"""
Fitmeezo Razorpay Automatic Refund & Reconciliation Service.
Handles:
- Safe calculation of eligible refund amounts accounting for previous refunds.
- Automatic creation of Razorpay refunds on eligible order cancellation.
- Prevention of duplicate refund requests via database locks and gateway checks.
- Synchronous and asynchronous status tracking (Pending, Processing, Successful, Failed).
- Safe reconciliation to recover from timeouts without double-refunding.
- Customer notification dispatch.
"""

from decimal import Decimal
import logging
import uuid
from django.conf import settings
from django.db import transaction
from django.utils import timezone

import razorpay

from core.models import Order, RefundRecord

logger = logging.getLogger(__name__)


class RefundProcessingError(Exception):
    """Custom exception raised when refund processing encounters a business rule violation."""
    pass


def calculate_order_refundable_amount(order: Order) -> Decimal:
    """
    Computes the remaining refundable amount for an order in INR.
    Formula: order.total_amount - sum of approved amounts of existing PROCESSED or PROCESSING refunds.
    """
    if not order or not order.total_amount:
        return Decimal("0.00")

    existing_refunded = (
        RefundRecord.objects.filter(
            order=order,
            status__in=[RefundRecord.STATUS_PROCESSED, RefundRecord.STATUS_PROCESSING],
        ).values_list("approved_amount", flat=True)
    )
    total_already_refunded = sum(existing_refunded, Decimal("0.00"))
    remaining = order.total_amount - total_already_refunded
    return max(remaining, Decimal("0.00")).quantize(Decimal("0.01"))


def is_order_eligible_for_automatic_refund(order: Order) -> tuple[bool, str]:
    """
    Validates whether an order is eligible for an automatic Razorpay refund:
    1. Order must be cancelled.
    2. Payment method must be online via Razorpay.
    3. Payment status must be PAID or verified.
    4. Must have a valid Razorpay payment ID.
    5. Order must not already be fully refunded.
    """
    if not order:
        return False, "Order does not exist."

    if order.status != Order.STATUS_CANCELLED:
        return False, f"Order #{order.order_number} is not cancelled (Current status: {order.get_status_display()})."

    if order.payment_method != Order.PAYMENT_METHOD_RAZORPAY:
        return False, f"Order #{order.order_number} was placed with payment method '{order.get_payment_method_display()}'. No online refund required."

    if order.payment_status not in (Order.PAYMENT_STATUS_PAID, Order.PAYMENT_STATUS_REFUNDED):
        return False, f"Order #{order.order_number} payment status is '{order.get_payment_status_display()}'. Unpaid/failed orders cannot be refunded."

    if not order.razorpay_payment_id or not order.razorpay_payment_id.strip():
        return False, f"Order #{order.order_number} does not have a verified Razorpay Payment ID."

    if order.refund_status == Order.REFUND_STATUS_PROCESSED:
        return False, f"Order #{order.order_number} has already been marked as fully refunded."

    refundable_amount = calculate_order_refundable_amount(order)
    if refundable_amount <= Decimal("0.00"):
        return False, f"Order #{order.order_number} has no remaining refundable balance."

    return True, "Order is eligible for automatic refund."


def initiate_order_cancellation_refund(
    order: Order,
    actor=None,
    reason: str = "",
    speed: str = "normal",
) -> tuple[bool, str, RefundRecord | None]:
    """
    Initiates an automatic refund for a cancelled, prepaid Razorpay order.
    1. Enforces eligibility rules and acquires row locks.
    2. Checks for in-flight or existing refunds to prevent duplicates.
    3. Calculates eligible amount accounting for any partial refunds.
    4. Calls official Razorpay API or safely executes simulated mock if in mock mode.
    5. Updates Order and RefundRecord statuses safely.
    6. Dispatches customer email notification.

    Returns:
        tuple (success: bool, message: str, refund_record: RefundRecord | None)
    """
    order_pk = order.pk if isinstance(order, Order) else order

    try:
        with transaction.atomic():
            locked_order = (
                Order.objects.select_for_update()
                .filter(pk=order_pk)
                .first()
            )
            if not locked_order:
                return False, "Order does not exist.", None

            eligible, msg = is_order_eligible_for_automatic_refund(locked_order)
            if not eligible:
                logger.info(f"Skipping automatic refund for #{locked_order.order_number}: {msg}")
                return False, msg, None

            # Concurrency guard: Check for recent in-flight processing refund record
            in_flight = RefundRecord.objects.filter(
                order=locked_order,
                status=RefundRecord.STATUS_PROCESSING,
            ).first()
            if in_flight:
                return (
                    False,
                    f"A refund request is already in-flight for Order #{locked_order.order_number} (Ref: {in_flight.refund_reference}).",
                    in_flight,
                )

            refundable_amount = calculate_order_refundable_amount(locked_order)
            if refundable_amount <= Decimal("0.00"):
                return False, "No refundable balance remaining.", None

            # Generate unique internal voucher reference
            date_str = timezone.now().strftime("%Y%m%d")
            rand_id = uuid.uuid4().hex[:6].upper()
            ref_voucher = f"REF-{date_str}-{rand_id}"

            clean_reason = (reason or f"Automatic cancellation refund for Order #{locked_order.order_number}").strip()[:255]

            # Determine whether live Razorpay credentials are configured
            rp_key_id = getattr(settings, "RAZORPAY_KEY_ID", "").strip()
            rp_key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "").strip()
            mock_mode = (
                getattr(settings, "FITMEEZO_MOCK_REFUNDS", False)
                or not rp_key_id
                or not rp_key_secret
                or rp_key_id.lower().startswith(("replace_", "your_", "todo"))
            )

            # Create initial RefundRecord in database
            refund_record = RefundRecord.objects.create(
                refund_reference=ref_voucher,
                external_reference="",
                order=locked_order,
                requested_amount=refundable_amount,
                approved_amount=refundable_amount,
                currency=locked_order.currency or "INR",
                payment_method=Order.PAYMENT_METHOD_RAZORPAY,
                status=RefundRecord.STATUS_PENDING,
                is_mock=mock_mode,
                processed_by=actor if getattr(actor, "is_authenticated", False) else None,
                reason=clean_reason,
                audit_note="Refund initiated following order cancellation.",
            )

            if mock_mode:
                # Simulated Mock Refund (Zero live external banking calls)
                mock_ext_id = f"MOCK-REF-{uuid.uuid4().hex[:12].upper()}"
                now_ts = timezone.now()

                refund_record.external_reference = mock_ext_id
                refund_record.status = RefundRecord.STATUS_PROCESSED
                refund_record.processed_at = now_ts
                refund_record.audit_note = (
                    f"Mock refund settled internally for ₹{refundable_amount}. Zero external gateway calls."
                )
                refund_record.save(update_fields=[
                    "external_reference", "status", "processed_at", "audit_note", "updated_at"
                ])

                locked_order.refund_status = Order.REFUND_STATUS_PROCESSED
                locked_order.payment_status = Order.PAYMENT_STATUS_REFUNDED
                locked_order.refund_amount = (locked_order.refund_amount or Decimal("0.00")) + refundable_amount
                locked_order.razorpay_refund_id = mock_ext_id
                locked_order.refunded_at = now_ts
                locked_order.refund_failure_reason = ""
                locked_order.save(update_fields=[
                    "refund_status",
                    "payment_status",
                    "refund_amount",
                    "razorpay_refund_id",
                    "refunded_at",
                    "refund_failure_reason",
                    "updated_at",
                ])

                logger.info(
                    f"Processed simulated mock refund {ref_voucher} for #{locked_order.order_number}: ₹{refundable_amount}"
                )
                # Dispatch notification email outside lock
                _dispatch_refund_email(locked_order, refund_record)
                return (
                    True,
                    f"Refund of ₹{refundable_amount} has been processed successfully (Simulated Ref: {mock_ext_id}).",
                    refund_record,
                )

            # Live Razorpay API Refund
            try:
                client = razorpay.Client(auth=(rp_key_id, rp_key_secret))
                amount_in_paise = int(refundable_amount * 100)

                payload = {
                    "amount": amount_in_paise,
                    "speed": speed or "normal",
                    "notes": {
                        "order_number": locked_order.order_number,
                        "refund_reference": ref_voucher,
                        "reason": clean_reason[:100],
                    },
                    "receipt": ref_voucher,
                }

                logger.info(
                    f"Calling Razorpay payment.refund for #{locked_order.order_number}: "
                    f"Payment ID {locked_order.razorpay_payment_id}, Amount {amount_in_paise} paise"
                )

                rp_resp = client.payment.refund(locked_order.razorpay_payment_id, payload)
                rp_refund_id = rp_resp.get("id", "")
                rp_refund_status = rp_resp.get("status", "processed")
                now_ts = timezone.now()

                refund_record.external_reference = rp_refund_id
                locked_order.razorpay_refund_id = rp_refund_id
                locked_order.refund_amount = (locked_order.refund_amount or Decimal("0.00")) + refundable_amount
                locked_order.refund_failure_reason = ""

                if rp_refund_status == "processed":
                    refund_record.status = RefundRecord.STATUS_PROCESSED
                    refund_record.processed_at = now_ts
                    refund_record.audit_note = (
                        f"Razorpay refund verified settled instantly: {rp_refund_id}."
                    )
                    locked_order.refund_status = Order.REFUND_STATUS_PROCESSED
                    locked_order.payment_status = Order.PAYMENT_STATUS_REFUNDED
                    locked_order.refunded_at = now_ts
                else:
                    # In-progress / Pending at bank network
                    refund_record.status = RefundRecord.STATUS_PROCESSING
                    refund_record.audit_note = (
                        f"Razorpay refund created ({rp_refund_status}): {rp_refund_id}. Awaiting banking network webhook."
                    )
                    locked_order.refund_status = Order.REFUND_STATUS_PROCESSING

                refund_record.save(update_fields=[
                    "external_reference", "status", "processed_at", "audit_note", "updated_at"
                ])
                locked_order.save(update_fields=[
                    "refund_status",
                    "payment_status",
                    "refund_amount",
                    "razorpay_refund_id",
                    "refunded_at",
                    "refund_failure_reason",
                    "updated_at",
                ])

                logger.info(
                    f"Razorpay refund initiated for #{locked_order.order_number}: "
                    f"{rp_refund_id} (Status: {rp_refund_status})"
                )

                _dispatch_refund_email(locked_order, refund_record)
                return (
                    True,
                    f"Refund of ₹{refundable_amount} has been initiated via Razorpay (Ref: {rp_refund_id}).",
                    refund_record,
                )

            except Exception as rp_err:
                err_msg = str(rp_err)
                logger.error(
                    f"Razorpay refund API call failed for #{locked_order.order_number}: {err_msg}",
                    exc_info=True,
                )

                refund_record.status = RefundRecord.STATUS_FAILED
                refund_record.audit_note = f"Razorpay refund API call failed: {err_msg}"
                refund_record.save(update_fields=["status", "audit_note", "updated_at"])

                locked_order.refund_status = Order.REFUND_STATUS_FAILED
                locked_order.refund_failure_reason = err_msg[:255]
                locked_order.save(update_fields=["refund_status", "refund_failure_reason", "updated_at"])

                return False, f"Payment gateway refund initiation failed: {err_msg}", refund_record

    except Exception as exc:
        logger.error(f"Unexpected error during refund processing for order #{order_pk}: {exc}", exc_info=True)
        return False, f"Unexpected refund error: {str(exc)}", None


def reconcile_order_refund(order: Order, actor=None) -> tuple[bool, str, RefundRecord | None]:
    """
    Safely reconciles order refund state with Razorpay before retrying:
    1. Queries Razorpay for all refunds on the order's payment ID.
    2. If a refund already exists at Razorpay (e.g. from a prior timed-out request):
       Synchronizes database state with the existing gateway refund without creating a duplicate.
    3. If no refund exists at Razorpay, initiates a fresh refund.
    4. Protected against duplicate refunds and race conditions.
    """
    if not order:
        return False, "Order does not exist.", None

    if order.status != Order.STATUS_CANCELLED:
        return False, "Only cancelled orders can be reconciled for refunds.", None

    if order.payment_method != Order.PAYMENT_METHOD_RAZORPAY:
        return False, "Only online Razorpay orders require gateway reconciliation.", None

    if not order.razorpay_payment_id:
        return False, "No verified Razorpay payment ID exists on this order.", None

    rp_key_id = getattr(settings, "RAZORPAY_KEY_ID", "").strip()
    rp_key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "").strip()
    mock_mode = (
        getattr(settings, "FITMEEZO_MOCK_REFUNDS", False)
        or not rp_key_id
        or not rp_key_secret
        or rp_key_id.lower().startswith(("replace_", "your_", "todo"))
    )

    if mock_mode:
        # In mock mode, if order was failed or pending, complete the mock settlement
        if order.refund_status != Order.REFUND_STATUS_PROCESSED:
            return initiate_order_cancellation_refund(
                order, actor=actor, reason="Admin mock refund reconciliation"
            )
        return True, "Mock refund is already processed.", order.refund_records.first()

    try:
        client = razorpay.Client(auth=(rp_key_id, rp_key_secret))
        logger.info(f"Reconciling refunds for order #{order.order_number} via Razorpay API...")

        # Fetch all refunds associated with the captured payment ID
        refunds_response = client.payment.all_refunds(order.razorpay_payment_id)
        refund_items = (
            refunds_response.get("items", [])
            if isinstance(refunds_response, dict)
            else (refunds_response if isinstance(refunds_response, list) else [])
        )

        with transaction.atomic():
            locked_order = Order.objects.select_for_update().get(pk=order.pk)

            if refund_items:
                # Prior refund exists on Razorpay! Reconcile with latest refund record
                latest_rp_refund = refund_items[0]
                rfnd_id = latest_rp_refund.get("id", "")
                rfnd_status = latest_rp_refund.get("status", "processed")
                rfnd_amount = Decimal(str(latest_rp_refund.get("amount", 0) / 100)).quantize(Decimal("0.01"))

                refund_record = RefundRecord.objects.filter(
                    order=locked_order,
                    external_reference=rfnd_id,
                ).first()

                if not refund_record:
                    # Find any pending or failed refund record on this order to link
                    refund_record = RefundRecord.objects.filter(
                        order=locked_order,
                    ).order_by("-created_at").first()

                if not refund_record:
                    date_str = timezone.now().strftime("%Y%m%d")
                    rand_id = uuid.uuid4().hex[:6].upper()
                    refund_record = RefundRecord(
                        refund_reference=f"REF-{date_str}-{rand_id}",
                        order=locked_order,
                        requested_amount=rfnd_amount,
                        approved_amount=rfnd_amount,
                        currency=locked_order.currency or "INR",
                        payment_method="razorpay",
                        is_mock=False,
                    )

                refund_record.external_reference = rfnd_id
                refund_record.approved_amount = rfnd_amount
                now_ts = timezone.now()

                if rfnd_status == "processed":
                    refund_record.status = RefundRecord.STATUS_PROCESSED
                    refund_record.processed_at = now_ts
                    refund_record.audit_note = (
                        f"Reconciled existing Razorpay refund {rfnd_id} confirmed as Processed."
                    )
                    locked_order.refund_status = Order.REFUND_STATUS_PROCESSED
                    locked_order.payment_status = Order.PAYMENT_STATUS_REFUNDED
                    locked_order.refunded_at = now_ts
                else:
                    refund_record.status = RefundRecord.STATUS_PROCESSING
                    refund_record.audit_note = (
                        f"Reconciled existing Razorpay refund {rfnd_id} currently in state '{rfnd_status}'."
                    )
                    locked_order.refund_status = Order.REFUND_STATUS_PROCESSING

                refund_record.save()

                locked_order.razorpay_refund_id = rfnd_id
                locked_order.refund_amount = rfnd_amount
                locked_order.refund_failure_reason = ""
                locked_order.save(update_fields=[
                    "refund_status",
                    "payment_status",
                    "refund_amount",
                    "razorpay_refund_id",
                    "refunded_at",
                    "refund_failure_reason",
                    "updated_at",
                ])

                logger.info(
                    f"Reconciled existing refund {rfnd_id} for #{locked_order.order_number}: Status {rfnd_status}"
                )
                return (
                    True,
                    f"Successfully reconciled existing gateway refund {rfnd_id} (Status: {rfnd_status.title()}). No duplicate refund was created.",
                    refund_record,
                )

            # No refund found on Razorpay -> Safe to trigger fresh refund
            logger.info(f"No existing refund on Razorpay for #{locked_order.order_number}. Initiating fresh refund...")
            return initiate_order_cancellation_refund(
                locked_order,
                actor=actor,
                reason="Admin retry and reconciliation",
            )

    except Exception as exc:
        logger.error(f"Reconciliation error for order #{order.order_number}: {exc}", exc_info=True)
        return False, f"Reconciliation failed: {str(exc)}", None


def _dispatch_refund_email(order: Order, refund_record: RefundRecord = None):
    """Safely dispatches refund notification email to customer."""
    try:
        from core.emails import send_order_refund_email
        send_order_refund_email(order, refund_record=refund_record)
    except Exception as err:
        logger.warning(f"Could not dispatch refund notification email for #{order.order_number}: {err}")
