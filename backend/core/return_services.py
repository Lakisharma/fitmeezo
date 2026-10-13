"""
Fitmeezo Returns, Exchanges & Refund Management Service (Phase 6).

Provides auditable, multi-seller isolated business workflows:
- Item-level return and exchange request validation
- Return lifecycle state transitions with strict permission gates
- Quality inspection recording and restock eligibility
- Exactly-once catalogue inventory restoration
- Safe internal/mock refund execution (zero live external gateway calls)
- Non-destructive commission and seller payable adjustment tracking
"""

import logging
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from core.models import (
    Order,
    OrderItem,
    Product,
    ProductVariant,
    RefundRecord,
    ReturnAuditLog,
    ReturnRequest,
    ReturnRequestItem,
    Seller,
    SellerOrder,
)

logger = logging.getLogger(__name__)


class ReturnServiceError(Exception):
    """Base exception for return and refund business errors."""
    pass


class ReturnValidationError(ReturnServiceError):
    """Exception for invalid customer, quantity, or eligibility requests."""
    pass


class ReturnPermissionError(ReturnServiceError):
    """Exception for unauthorized seller or customer access."""
    pass


class RefundError(ReturnServiceError):
    """Exception for invalid or failed refund settlement operations."""
    pass


def get_return_window_days() -> int:
    """Returns the configurable return window in days."""
    return getattr(settings, "FITMEEZO_RETURN_WINDOW_DAYS", 7)


def validate_item_eligibility(order_item: OrderItem, quantity: int = 1, user=None) -> tuple[bool, str]:
    """
    Validates server-side if an OrderItem is eligible for return/exchange:
    1. Quantity must be positive integer.
    2. User must own the parent order (if user provided).
    3. Associated SellerOrder package must be in DELIVERED status.
    4. Product must be returnable under policy.
    5. Delivery timestamp must be within product-specific or global return window.
    6. Requested quantity must not exceed remaining returnable quantity.
    """
    if quantity <= 0:
        return False, "Requested return quantity must be at least 1."

    if user and not (user.is_staff or user.is_superuser):
        if order_item.order.user_id != user.id:
            return False, "You do not have permission to return items from this order."

    seller_order = order_item.seller_order
    if not seller_order:
        return False, "This item is not linked to a valid package."

    if seller_order.status != SellerOrder.STATUS_DELIVERED:
        return False, f"Package #{seller_order.sub_order_number} must be delivered before initiating a return."

    # Product policy check
    if order_item.product and not getattr(order_item.product, "is_returnable", True):
        return False, f"'{order_item.product_name}' is marked as non-returnable under its product policy."

    # Return window validation
    delivered_date = seller_order.delivered_at or seller_order.updated_at
    if not delivered_date:
        return False, "Delivery date is missing for this package."

    window_days = order_item.return_window_days
    cutoff_date = delivered_date + timedelta(days=window_days)
    if timezone.now() > cutoff_date:
        return False, f"The return window of {window_days} days for this item expired on {cutoff_date.strftime('%d %b %Y')}."

    # Check remaining returnable quantity
    remaining = order_item.remaining_returnable_quantity
    if remaining <= 0:
        return False, f"All units of '{order_item.product_name}' have already been returned or requested for return."

    if quantity > remaining:
        return False, f"Cannot return {quantity} units. Only {remaining} units of '{order_item.product_name}' are eligible."

    return True, ""


def create_return_request(
    customer,
    seller_order: SellerOrder,
    items_data: list[dict],
    request_type: str = ReturnRequest.TYPE_RETURN,
    reason: str = ReturnRequest.REASON_OTHER,
    customer_notes: str = "",
    photo_1=None,
    photo_2=None,
    photo_3=None,
) -> ReturnRequest:
    """
    Atomically creates a new ReturnRequest for items in a specific SellerOrder package.

    Args:
        customer: The authenticated customer user.
        seller_order: The delivered SellerOrder package.
        items_data: list of dicts: [
            {"order_item": OrderItem or id, "quantity": int, "exchange_variant_id": int or None}
        ]
        request_type: "return" or "exchange".
        reason: Stated reason category.
        customer_notes: Explanation remarks.
        photo_1, photo_2, photo_3: Optional customer uploaded photos.

    Returns:
        The newly created ReturnRequest instance.
    """
    if not items_data:
        raise ReturnValidationError("Please select at least one item to return or exchange.")

    if request_type == ReturnRequest.TYPE_EXCHANGE and not getattr(settings, "FITMEEZO_EXCHANGE_ENABLED", True):
        raise ReturnValidationError("Exchanges are currently disabled.")

    with transaction.atomic():
        # Lock seller order to prevent concurrent race conditions
        locked_seller_order = (
            SellerOrder.objects.select_for_update()
            .select_related("order", "seller")
            .filter(pk=seller_order.pk)
            .first()
        )
        if not locked_seller_order:
            raise ReturnValidationError("Package not found.")

        # Customer ownership validation
        if locked_seller_order.order.user_id != customer.id:
            raise ReturnPermissionError("You do not own this order package.")

        if locked_seller_order.status != SellerOrder.STATUS_DELIVERED:
            raise ReturnValidationError("Package must be delivered before requesting a return or exchange.")

        # Return window check
        delivered_date = locked_seller_order.delivered_at or locked_seller_order.updated_at
        window_days = get_return_window_days()
        if timezone.now() > (delivered_date + timedelta(days=window_days)):
            raise ReturnValidationError(f"The return window of {window_days} days has expired for this package.")

        # Generate unique return request number
        date_str = timezone.now().strftime("%Y%m%d")
        rand_id = uuid.uuid4().hex[:6].upper()
        req_number = f"RET-{date_str}-{rand_id}"
        while ReturnRequest.objects.filter(request_number=req_number).exists():
            rand_id = uuid.uuid4().hex[:6].upper()
            req_number = f"RET-{date_str}-{rand_id}"

        return_request = ReturnRequest.objects.create(
            request_number=req_number,
            customer=customer,
            order=locked_seller_order.order,
            seller_order=locked_seller_order,
            seller=locked_seller_order.seller,
            is_platform_fulfilled=locked_seller_order.is_platform_fulfilled,
            request_type=request_type,
            status=ReturnRequest.STATUS_REQUESTED,
            reason=reason,
            customer_notes=customer_notes.strip() if customer_notes else "",
            photo_1=photo_1,
            photo_2=photo_2,
            photo_3=photo_3,
            is_mock=True,
        )

        for entry in items_data:
            item_obj = entry.get("order_item")
            item_id = item_obj.id if hasattr(item_obj, "id") else item_obj
            qty = int(entry.get("quantity", 1))

            locked_item = (
                OrderItem.objects.select_for_update()
                .filter(pk=item_id, seller_order=locked_seller_order)
                .first()
            )
            if not locked_item:
                raise ReturnValidationError(f"Invalid item #{item_id} for this package.")

            is_eligible, err_msg = validate_item_eligibility(locked_item, quantity=qty, user=customer)
            if not is_eligible:
                raise ReturnValidationError(err_msg)

            # Exchange variant validation if applicable
            exchange_variant = None
            if request_type == ReturnRequest.TYPE_EXCHANGE:
                v_id = entry.get("exchange_variant_id")
                if v_id:
                    exchange_variant = ProductVariant.objects.filter(
                        pk=v_id,
                        product=locked_item.product,
                        is_active=True,
                    ).first()
                    if not exchange_variant:
                        raise ReturnValidationError("Selected exchange variant is invalid or inactive.")
                    if exchange_variant.stock_quantity < qty:
                        raise ReturnValidationError(
                            f"Insufficient inventory for exchange option '{exchange_variant.option_value}'. Only {exchange_variant.stock_quantity} available."
                        )

            refund_amt = (locked_item.unit_price * Decimal(str(qty))).quantize(Decimal("0.01"))

            ReturnRequestItem.objects.create(
                return_request=return_request,
                order_item=locked_item,
                quantity=qty,
                exchange_variant=exchange_variant,
                item_unit_price=locked_item.unit_price,
                refund_eligible_amount=refund_amt,
                restock_inventory=False,
                inspection_status=ReturnRequestItem.INSPECTION_PENDING,
            )

        # Record timeline audit entry
        ReturnAuditLog.objects.create(
            return_request=return_request,
            actor=customer,
            actor_role="customer",
            from_status="",
            to_status=ReturnRequest.STATUS_REQUESTED,
            action="request_created",
            notes=f"Customer submitted {return_request.get_request_type_display().lower()} request.",
            metadata={
                "items_count": len(items_data),
                "total_quantity": return_request.total_quantity,
                "total_refund_amount": str(return_request.total_refund_amount),
            },
        )

        return return_request


VALID_TRANSITIONS = {
    ReturnRequest.STATUS_REQUESTED: [
        ReturnRequest.STATUS_APPROVED,
        ReturnRequest.STATUS_REJECTED,
        ReturnRequest.STATUS_CANCELLED,
    ],
    ReturnRequest.STATUS_APPROVED: [
        ReturnRequest.STATUS_PICKUP_PENDING,
        ReturnRequest.STATUS_CANCELLED,
    ],
    ReturnRequest.STATUS_PICKUP_PENDING: [
        ReturnRequest.STATUS_ITEM_RECEIVED,
        ReturnRequest.STATUS_CANCELLED,
    ],
    ReturnRequest.STATUS_ITEM_RECEIVED: [
        ReturnRequest.STATUS_INSPECTION,
    ],
    ReturnRequest.STATUS_INSPECTION: [
        ReturnRequest.STATUS_PROCESSING,
        ReturnRequest.STATUS_REJECTED,
    ],
    ReturnRequest.STATUS_PROCESSING: [
        ReturnRequest.STATUS_COMPLETED,
    ],
    ReturnRequest.STATUS_COMPLETED: [],
    ReturnRequest.STATUS_REJECTED: [],
    ReturnRequest.STATUS_CANCELLED: [],
}


def transition_return_status(
    return_request: ReturnRequest,
    new_status: str,
    actor,
    actor_role: str,
    notes: str = "",
    rejection_reason: str = "",
    inspection_decisions: dict | None = None,
) -> ReturnRequest:
    """
    Safely transitions the lifecycle status of a ReturnRequest with:
    - Row-level locking to prevent race conditions
    - Role-based authorization (customer vs seller vs platform staff)
    - State machine validity enforcement
    - Inspection recording and exactly-once inventory restoration
    - Auditable append-only event logging
    """
    with transaction.atomic():
        locked_req = (
            ReturnRequest.objects.select_for_update()
            .select_related("seller_order", "seller", "order")
            .filter(pk=return_request.pk)
            .first()
        )
        if not locked_req:
            raise ReturnServiceError("Return request not found.")

        current_status = locked_req.status

        # Authorization check
        if actor_role == "customer":
            if locked_req.customer_id != actor.id:
                raise ReturnPermissionError("You cannot modify return requests of other customers.")
            if new_status != ReturnRequest.STATUS_CANCELLED:
                raise ReturnPermissionError("Customers can only cancel their own return requests.")
            if not locked_req.can_customer_cancel:
                raise ReturnValidationError(
                    f"Cannot cancel return request in '{locked_req.get_status_display()}' state."
                )

        elif actor_role == "seller":
            if locked_req.is_platform_fulfilled:
                raise ReturnPermissionError("Sellers cannot manage platform-fulfilled returns.")
            seller_profile = getattr(actor, "seller_profile", None)
            if not seller_profile or locked_req.seller_id != seller_profile.id:
                raise ReturnPermissionError("You do not have permission to manage this seller's return request.")

        elif actor_role in ["platform_admin", "staff"]:
            if not (actor.is_staff or actor.is_superuser):
                raise ReturnPermissionError("Only platform administrators can perform this action.")
        else:
            raise ReturnPermissionError(f"Unknown actor role: {actor_role}")

        # Transition validation
        allowed_targets = VALID_TRANSITIONS.get(current_status, [])
        if new_status not in allowed_targets:
            raise ReturnValidationError(
                f"Invalid transition from '{locked_req.get_status_display()}' to '{dict(ReturnRequest.STATUS_CHOICES).get(new_status, new_status)}'."
            )

        # Inspection update if provided
        if inspection_decisions and new_status in [ReturnRequest.STATUS_INSPECTION, ReturnRequest.STATUS_PROCESSING, ReturnRequest.STATUS_COMPLETED]:
            for item in locked_req.items.select_related("order_item__product", "order_item__variant").all():
                dec = inspection_decisions.get(str(item.id)) or inspection_decisions.get(item.id)
                if dec:
                    if "status" in dec:
                        item.inspection_status = dec["status"]
                    if "restock" in dec:
                        item.restock_inventory = bool(dec["restock"])
                    if "notes" in dec:
                        item.inspection_notes = dec["notes"]
                    item.save(update_fields=["inspection_status", "restock_inventory", "inspection_notes"])

        # Exactly-once inventory restoration
        if new_status == ReturnRequest.STATUS_COMPLETED and not locked_req.inventory_restored:
            for item in locked_req.items.select_related("order_item__product", "order_item__variant").all():
                if item.restock_inventory:
                    order_item = item.order_item
                    qty = item.quantity
                    if order_item.variant_id:
                        ProductVariant.objects.filter(pk=order_item.variant_id).update(
                            stock_quantity=F("stock_quantity") + qty
                        )
                    elif order_item.product_id:
                        Product.objects.filter(pk=order_item.product_id).update(
                            stock_quantity=F("stock_quantity") + qty
                        )
            locked_req.inventory_restored = True

        # Field updates
        locked_req.status = new_status
        if rejection_reason and new_status == ReturnRequest.STATUS_REJECTED:
            locked_req.rejection_reason = rejection_reason.strip()
        if notes and actor_role in ["seller", "platform_admin", "staff"]:
            locked_req.seller_notes = f"{locked_req.seller_notes}\n[{timezone.now().strftime('%Y-%m-%d %H:%M')}] {notes}".strip()

        if new_status in [ReturnRequest.STATUS_COMPLETED, ReturnRequest.STATUS_REJECTED, ReturnRequest.STATUS_CANCELLED]:
            locked_req.resolved_at = timezone.now()

        locked_req.save()

        # Audit log creation
        action_name = f"status_{new_status}"
        ReturnAuditLog.objects.create(
            return_request=locked_req,
            actor=actor if hasattr(actor, "id") else None,
            actor_role=actor_role,
            from_status=current_status,
            to_status=new_status,
            action=action_name,
            notes=notes or rejection_reason or f"Status changed to {locked_req.get_status_display()}.",
            metadata={"new_status": new_status, "previous_status": current_status},
        )

        return locked_req


def process_simulated_refund(
    return_request: ReturnRequest,
    actor,
    approved_amount: Decimal | None = None,
    reason: str = "",
    payment_method: str = "",
) -> RefundRecord:
    """
    Safely records a simulated mock refund without contacting external payment gateways:
    1. Validates that FITMEEZO_MOCK_REFUNDS is True (safe mock execution).
    2. Atomically locks ReturnRequest to prevent duplicate refund records.
    3. Validates approved amount against return item eligible amounts.
    4. Calculates non-destructive commission reversal and seller payable deduction.
    5. Creates durable RefundRecord with MOCK-REF external reference.
    6. Automatically transitions ReturnRequest to COMPLETED if currently in PROCESSING.
    """
    if not getattr(settings, "FITMEEZO_MOCK_REFUNDS", True):
        raise RefundError("Live refunds are disabled. Set FITMEEZO_MOCK_REFUNDS=True.")

    with transaction.atomic():
        locked_req = (
            ReturnRequest.objects.select_for_update()
            .select_related("order", "seller_order", "seller")
            .filter(pk=return_request.pk)
            .first()
        )
        if not locked_req:
            raise RefundError("Return request does not exist.")

        # Guard against duplicate refund processing
        existing_processed = RefundRecord.objects.filter(
            return_request=locked_req,
            status=RefundRecord.STATUS_PROCESSED,
        ).first()
        if existing_processed:
            raise RefundError(
                f"Refund already processed for this request (Ref: {existing_processed.refund_reference})."
            )

        max_eligible = locked_req.total_refund_amount
        if approved_amount is None:
            approved_amount = max_eligible
        else:
            approved_amount = Decimal(str(approved_amount)).quantize(Decimal("0.01"))

        if approved_amount < Decimal("0.00"):
            raise RefundError("Refund amount cannot be negative.")
        if approved_amount > max_eligible:
            raise RefundError(
                f"Approved refund of ₹{approved_amount} exceeds maximum eligible amount of ₹{max_eligible}."
            )

        # Calculate commission reversal & seller payable deduction (audit adjustments)
        comm_reversal = Decimal("0.00")
        seller_deduction = Decimal("0.00")

        if not locked_req.is_platform_fulfilled and locked_req.seller:
            for item in locked_req.items.select_related("order_item").all():
                rate = item.order_item.commission_rate
                item_comm = (item.refund_eligible_amount * rate / Decimal("100.00")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                comm_reversal += item_comm
            seller_deduction = (approved_amount - comm_reversal).quantize(Decimal("0.01"))

        # Generate unique references
        date_str = timezone.now().strftime("%Y%m%d")
        rand_id = uuid.uuid4().hex[:6].upper()
        ref_voucher = f"REF-{date_str}-{rand_id}"
        ext_ref = f"MOCK-REF-{uuid.uuid4().hex[:12].upper()}"

        refund_record = RefundRecord.objects.create(
            refund_reference=ref_voucher,
            external_reference=ext_ref,
            order=locked_req.order,
            return_request=locked_req,
            seller_order=locked_req.seller_order,
            requested_amount=max_eligible,
            approved_amount=approved_amount,
            commission_reversal=comm_reversal,
            seller_payable_deduction=seller_deduction,
            currency="INR",
            payment_method=payment_method or locked_req.order.payment_method or "mock",
            status=RefundRecord.STATUS_PROCESSED,
            is_mock=True,
            processed_by=actor if hasattr(actor, "id") else None,
            processed_at=timezone.now(),
            reason=reason or f"Simulated mock refund for #{locked_req.request_number}",
            audit_note=(
                f"Simulated refund processed internally. Zero external gateway calls. "
                f"Commission reversal: ₹{comm_reversal}. Net seller payable deduction: ₹{seller_deduction}."
            ),
        )

        # Complete the return request if in processing
        if locked_req.status == ReturnRequest.STATUS_PROCESSING:
            transition_return_status(
                return_request=locked_req,
                new_status=ReturnRequest.STATUS_COMPLETED,
                actor=actor,
                actor_role="platform_admin" if (actor.is_staff or actor.is_superuser) else "seller",
                notes=f"Refund #{ref_voucher} processed.",
            )

        # Post exactly-once financial debit/credit adjustments to seller ledger (Phase 7)
        try:
            from core.ledger_services import post_refund_to_ledger
            post_refund_to_ledger(refund_record, actor=actor)
        except Exception as ledger_err:
            logger.warning(f"Failed to post refund {refund_record.refund_reference} to ledger: {ledger_err}")

        return refund_record

