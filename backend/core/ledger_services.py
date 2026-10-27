"""
Fitmeezo Seller Ledger, Commission & Payout Management Service (Phase 7).

Provides auditable multi-seller marketplace financial accounting:
- Double-entry immutable credit/debit seller ledger postings
- Commission rate & amount snapshot integrity preservation
- Strict idempotency and database constraint enforcement against duplicate postings
- Courier Cash-on-Delivery (COD) hold and explicit remittance reconciliation
- Integrated Phase 6 returns/refunds debit/credit adjustments
- Payout eligibility calculation with return-window holding
- Concurrency-safe payout batch preparation using row-level database locking
- Simulated / Mock payout execution and auditable reversal tracking (zero live banking calls)
"""

import calendar
import logging
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction, IntegrityError
from django.db.models import F, Sum, Q
from django.utils import timezone

from core.models import (
    CODReceiptRecord,
    Order,
    OrderItem,
    PayoutAuditLog,
    PayoutBatch,
    Product,
    RefundRecord,
    ReturnRequest,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)

logger = logging.getLogger(__name__)


class LedgerServiceError(Exception):
    """Base exception for ledger and payout accounting business failures."""
    pass


class DuplicatePostingError(LedgerServiceError):
    """Exception raised when a financial entry has already been posted."""
    pass


class PayoutEligibilityError(LedgerServiceError):
    """Exception raised when merchant does not meet payout criteria."""
    pass


class PayoutAuthorizationError(LedgerServiceError):
    """Exception raised when dual-authorization (Maker-Checker) or RBAC permissions fail."""
    pass


class ReconciliationError(LedgerServiceError):
    """Exception raised on invalid COD reconciliation attempt."""
    pass


def get_payout_hold_days() -> int:
    """Returns the holding period in days post-delivery before order funds are payout-eligible (default: 5 days / 120 hours)."""
    return getattr(settings, "FITMEEZO_PAYOUT_HOLD_DAYS_AFTER_DELIVERY", 5)


def get_min_payout_threshold() -> Decimal:
    """Returns the minimum net balance in INR required to generate a payout batch (regular minimum: ₹500.00)."""
    return getattr(settings, "FITMEEZO_MIN_PAYOUT_THRESHOLD", Decimal("500.00"))


def get_cycle_bounds(
    cycle_str: str | None = None,
    as_of_date=None,
) -> tuple[str, datetime, datetime, datetime]:
    """
    Computes timezone-aware payout cycle bounds.
    Supports:
    1. 15-Day Cycles (Approved Standard):
       - Cycle 1 (YYYY-MM-C1): Calendar days 1st–15th. Settlement calculation date is the 16th of that month at 00:00:00.
       - Cycle 2 (YYYY-MM-C2): Calendar days 16th–Month-End. Settlement calculation date is the 1st of next month at 00:00:00.
    2. Legacy Monthly Cycles (Backward Compatibility):
       - YYYY-MM: 5th of cycle month calculating previous calendar month.
    3. Default (cycle_str is None):
       - Resolves to the most recently completed 15-day cycle based on as_of_date.

    Returns:
    (canonical_cycle: str, cycle_payout_date: datetime, period_start: datetime, period_end: datetime)
    """
    tz = timezone.get_current_timezone()
    if as_of_date is None:
        as_of_date = timezone.now()

    cycle_clean = str(cycle_str).strip().upper() if cycle_str else ""

    # 1. Explicit 15-Day Cycle (e.g. "2026-10-C1" or "2026-10-C2")
    if "-C1" in cycle_clean or "-C2" in cycle_clean:
        parts = cycle_clean.split("-")
        cycle_year = int(parts[0])
        cycle_month = int(parts[1])
        cycle_suffix = parts[2]

        if cycle_suffix == "C1":
            canonical_cycle = f"{cycle_year:04d}-{cycle_month:02d}-C1"
            period_start = timezone.make_aware(datetime(cycle_year, cycle_month, 1, 0, 0, 0), tz)
            period_end = timezone.make_aware(datetime(cycle_year, cycle_month, 15, 23, 59, 59, 999999), tz)
            cycle_payout_date = timezone.make_aware(datetime(cycle_year, cycle_month, 16, 0, 0, 0), tz)
        else:  # C2
            canonical_cycle = f"{cycle_year:04d}-{cycle_month:02d}-C2"
            _, last_day = calendar.monthrange(cycle_year, cycle_month)
            period_start = timezone.make_aware(datetime(cycle_year, cycle_month, 16, 0, 0, 0), tz)
            period_end = timezone.make_aware(datetime(cycle_year, cycle_month, last_day, 23, 59, 59, 999999), tz)
            if cycle_month == 12:
                next_year = cycle_year + 1
                next_month = 1
            else:
                next_year = cycle_year
                next_month = cycle_month + 1
            cycle_payout_date = timezone.make_aware(datetime(next_year, next_month, 1, 0, 0, 0), tz)

        return canonical_cycle, cycle_payout_date, period_start, period_end

    # 2. Legacy Monthly Cycle (e.g. "2026-10")
    if cycle_clean and len(cycle_clean.split("-")) == 2:
        try:
            parts = cycle_clean.split("-")
            cycle_year = int(parts[0])
            cycle_month = int(parts[1])
        except (ValueError, IndexError):
            cycle_year = as_of_date.year
            cycle_month = as_of_date.month

        canonical_cycle = f"{cycle_year:04d}-{cycle_month:02d}"
        cycle_day = getattr(settings, "FITMEEZO_PAYOUT_CYCLE_DAY", 5)
        cycle_payout_date = timezone.make_aware(datetime(cycle_year, cycle_month, cycle_day, 0, 0, 0), tz)

        if cycle_month == 1:
            prev_year = cycle_year - 1
            prev_month = 12
        else:
            prev_year = cycle_year
            prev_month = cycle_month - 1

        _, last_day = calendar.monthrange(prev_year, prev_month)
        prev_month_start = timezone.make_aware(datetime(prev_year, prev_month, 1, 0, 0, 0), tz)
        prev_month_end = timezone.make_aware(datetime(prev_year, prev_month, last_day, 23, 59, 59, 999999), tz)

        return canonical_cycle, cycle_payout_date, prev_month_start, prev_month_end

    # 3. Default resolution based on as_of_date (15-Day Cycle Standard)
    if as_of_date.day >= 16:
        canonical_cycle = f"{as_of_date.year:04d}-{as_of_date.month:02d}-C1"
        period_start = timezone.make_aware(datetime(as_of_date.year, as_of_date.month, 1, 0, 0, 0), tz)
        period_end = timezone.make_aware(datetime(as_of_date.year, as_of_date.month, 15, 23, 59, 59, 999999), tz)
        cycle_payout_date = timezone.make_aware(datetime(as_of_date.year, as_of_date.month, 16, 0, 0, 0), tz)
    else:
        if as_of_date.month == 1:
            prev_year = as_of_date.year - 1
            prev_month = 12
        else:
            prev_year = as_of_date.year
            prev_month = as_of_date.month - 1
        _, last_day = calendar.monthrange(prev_year, prev_month)
        canonical_cycle = f"{prev_year:04d}-{prev_month:02d}-C2"
        period_start = timezone.make_aware(datetime(prev_year, prev_month, 16, 0, 0, 0), tz)
        period_end = timezone.make_aware(datetime(prev_year, prev_month, last_day, 23, 59, 59, 999999), tz)
        cycle_payout_date = timezone.make_aware(datetime(as_of_date.year, as_of_date.month, 1, 0, 0, 0), tz)

    return canonical_cycle, cycle_payout_date, period_start, period_end


# ==============================================================================
# 1. ORDER SALE & COMMISSION POSTING
# ==============================================================================

def post_order_sale_to_ledger(
    seller_order: SellerOrder,
    actor=None,
    force_posted: bool = False,
) -> list[SellerLedgerEntry]:
    """
    Idempotently posts credit and debit ledger entries for an eligible SellerOrder package:
    1. ORDER_SALE (Credit): Gross Merchandise Value (subtotal).
    2. COMMISSION (Debit): Platform commission amount snapshotted at checkout.
    3. SHIPPING_CREDIT (Credit, optional): Customer shipping collected allocated to seller.
    4. SHIPPING_DEBIT (Debit, optional): Courier shipping cost allocated to seller.

    COD Safety Rule:
    If the order payment method is COD and remittance is not yet reconciled,
    the entries are posted with status=PENDING_COD. They will NOT qualify for
    payout until explicitly reconciled via `reconcile_cod_remittance`.
    Prepaid orders (or already-reconciled COD) are posted with status=POSTED.
    """
    created_entries = []
    order = seller_order.order
    seller = seller_order.seller
    is_platform = seller_order.is_platform_fulfilled or seller is None

    # Determine posting status based on payment method and COD reconciliation
    is_cod = order.payment_method == Order.PAYMENT_METHOD_COD
    has_cod_reconciliation = hasattr(seller_order, "cod_reconciliation") and seller_order.cod_reconciliation is not None

    if is_cod and not has_cod_reconciliation and not force_posted:
        initial_status = SellerLedgerEntry.STATUS_PENDING_COD
        posted_at_ts = None
        status_note = "Pending courier COD cash remittance."
    else:
        initial_status = SellerLedgerEntry.STATUS_POSTED
        posted_at_ts = timezone.now()
        status_note = "Prepaid or reconciled."

    with transaction.atomic():
        # 1. Gross Merchandise Value (Credit)
        sale_key = f"ORD-{seller_order.id}-SALE"
        existing_sale = SellerLedgerEntry.objects.filter(idempotency_key=sale_key).first()
        if not existing_sale and seller_order.subtotal > Decimal("0.00"):
            entry_desc = (
                f"Gross sale for package #{seller_order.sub_order_number} "
                f"({order.get_payment_method_display()}) - {status_note}"
            )
            sale_entry = SellerLedgerEntry.objects.create(
                seller=seller,
                is_platform_fulfilled=is_platform,
                entry_type=SellerLedgerEntry.TYPE_CREDIT,
                category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
                amount=seller_order.subtotal,
                currency=order.currency or "INR",
                status=initial_status,
                source_type="seller_order",
                source_reference=seller_order.sub_order_number,
                order=order,
                seller_order=seller_order,
                idempotency_key=sale_key,
                description=entry_desc,
                posted_at=posted_at_ts,
                created_by=actor if hasattr(actor, "id") else None,
                audit_metadata={
                    "order_number": order.order_number,
                    "sub_order_number": seller_order.sub_order_number,
                    "payment_method": order.payment_method,
                    "payment_status": order.payment_status,
                    "seller_subtotal": str(seller_order.subtotal),
                },
            )
            created_entries.append(sale_entry)
        elif existing_sale:
            created_entries.append(existing_sale)

        # 2. Platform Commission (Debit) - applicable to third-party merchants
        if not is_platform and seller_order.total_commission > Decimal("0.00"):
            comm_key = f"ORD-{seller_order.id}-COMM"
            existing_comm = SellerLedgerEntry.objects.filter(idempotency_key=comm_key).first()
            if not existing_comm:
                comm_entry = SellerLedgerEntry.objects.create(
                    seller=seller,
                    is_platform_fulfilled=is_platform,
                    entry_type=SellerLedgerEntry.TYPE_DEBIT,
                    category=SellerLedgerEntry.CATEGORY_COMMISSION,
                    amount=seller_order.total_commission,
                    currency=order.currency or "INR",
                    status=initial_status,
                    source_type="seller_order",
                    source_reference=seller_order.sub_order_number,
                    order=order,
                    seller_order=seller_order,
                    idempotency_key=comm_key,
                    description=f"Platform commission for #{seller_order.sub_order_number}",
                    posted_at=posted_at_ts,
                    created_by=actor if hasattr(actor, "id") else None,
                    audit_metadata={
                        "sub_order_number": seller_order.sub_order_number,
                        "commission_rate_snapshot": str(seller.commission_rate if seller else "0.00"),
                        "total_commission": str(seller_order.total_commission),
                    },
                )
                created_entries.append(comm_entry)
            elif existing_comm:
                created_entries.append(existing_comm)

        # 3. Shipping Allocations (if configured and allocated)
        # If customer shipping collected is credited to the seller
        if (
            not is_platform
            and seller_order.customer_shipping_charge > Decimal("0.00")
            and getattr(settings, "FITMEEZO_PASS_CUSTOMER_SHIPPING_TO_SELLER", False)
        ):
            ship_cred_key = f"ORD-{seller_order.id}-SHIP-CREDIT"
            if not SellerLedgerEntry.objects.filter(idempotency_key=ship_cred_key).exists():
                ship_credit_entry = SellerLedgerEntry.objects.create(
                    seller=seller,
                    is_platform_fulfilled=is_platform,
                    entry_type=SellerLedgerEntry.TYPE_CREDIT,
                    category=SellerLedgerEntry.CATEGORY_SHIPPING_CREDIT,
                    amount=seller_order.customer_shipping_charge,
                    currency=order.currency or "INR",
                    status=initial_status,
                    source_type="seller_order",
                    source_reference=seller_order.sub_order_number,
                    order=order,
                    seller_order=seller_order,
                    idempotency_key=ship_cred_key,
                    description=f"Customer shipping charge share for #{seller_order.sub_order_number}",
                    posted_at=posted_at_ts,
                    created_by=actor if hasattr(actor, "id") else None,
                )
                created_entries.append(ship_credit_entry)

        # If actual shipping cost was allocated to be deducted from seller
        if (
            not is_platform
            and seller_order.shipping_allocated_to_seller > Decimal("0.00")
        ):
            ship_deb_key = f"ORD-{seller_order.id}-SHIP-DEBIT"
            if not SellerLedgerEntry.objects.filter(idempotency_key=ship_deb_key).exists():
                ship_debit_entry = SellerLedgerEntry.objects.create(
                    seller=seller,
                    is_platform_fulfilled=is_platform,
                    entry_type=SellerLedgerEntry.TYPE_DEBIT,
                    category=SellerLedgerEntry.CATEGORY_SHIPPING_DEBIT,
                    amount=seller_order.shipping_allocated_to_seller,
                    currency=order.currency or "INR",
                    status=initial_status,
                    source_type="seller_order",
                    source_reference=seller_order.sub_order_number,
                    order=order,
                    seller_order=seller_order,
                    idempotency_key=ship_deb_key,
                    description=f"Logistics shipping cost allocated for #{seller_order.sub_order_number}",
                    posted_at=posted_at_ts,
                    created_by=actor if hasattr(actor, "id") else None,
                )
                created_entries.append(ship_debit_entry)

    return created_entries


# ==============================================================================
# 2. COD CASH RECEIPT RECONCILIATION
# ==============================================================================

def reconcile_cod_remittance(
    seller_order: SellerOrder,
    remittance_reference: str,
    collected_amount: Decimal | None = None,
    courier_name: str = "",
    remitted_date=None,
    actor=None,
    notes: str = "",
) -> CODReceiptRecord:
    """
    Explicitly reconciles courier Cash-on-Delivery remittance for a delivered package:
    1. Validates that package is COD and in DELIVERED status.
    2. Prevents duplicate reconciliation via unique constraint.
    3. Creates durable, auditable CODReceiptRecord.
    4. Transitions associated PENDING_COD ledger entries to POSTED, unlocking payout eligibility.
    5. Updates order payment_status to PAID if all child packages are reconciled.
    """
    clean_ref = (remittance_reference or "").strip()
    if not clean_ref:
        raise ReconciliationError("Courier remittance reference is required.")

    if seller_order.order.payment_method != Order.PAYMENT_METHOD_COD:
        raise ReconciliationError(
            f"Package #{seller_order.sub_order_number} belongs to a prepaid order ({seller_order.order.payment_method}). COD reconciliation is not applicable."
        )

    if seller_order.status != SellerOrder.STATUS_DELIVERED:
        raise ReconciliationError(
            f"Package #{seller_order.sub_order_number} must be in DELIVERED status before reconciling cash remittance (current status: {seller_order.get_status_display()})."
        )

    with transaction.atomic():
        locked_pkg = (
            SellerOrder.objects.select_for_update()
            .select_related("order", "seller")
            .filter(pk=seller_order.pk)
            .first()
        )
        if not locked_pkg:
            raise ReconciliationError("Package does not exist.")

        if hasattr(locked_pkg, "cod_reconciliation") and locked_pkg.cod_reconciliation is not None:
            raise ReconciliationError(
                f"Package #{locked_pkg.sub_order_number} has already been reconciled (Ref: {locked_pkg.cod_reconciliation.remittance_reference})."
            )

        # Default collected amount to package subtotal if not provided
        if collected_amount is None:
            collected_amount = locked_pkg.subtotal
        else:
            collected_amount = Decimal(str(collected_amount)).quantize(Decimal("0.01"))

        if collected_amount <= Decimal("0.00"):
            raise ReconciliationError("Collected amount must be positive.")

        rec_record = CODReceiptRecord.objects.create(
            remittance_reference=clean_ref,
            seller_order=locked_pkg,
            order=locked_pkg.order,
            collected_amount=collected_amount,
            courier_name=courier_name or locked_pkg.courier_name or "Courier Partner",
            remitted_date=remitted_date or timezone.now().date(),
            reconciled_by=actor if hasattr(actor, "id") else None,
            reconciled_at=timezone.now(),
            notes=notes or f"Courier COD remittance reconciled for #{locked_pkg.sub_order_number}.",
            audit_metadata={
                "subtotal": str(locked_pkg.subtotal),
                "reconciled_by_username": getattr(actor, "username", "system"),
            },
        )

        # Transition pending ledger entries to POSTED
        now_ts = timezone.now()
        pending_entries = SellerLedgerEntry.objects.filter(
            seller_order=locked_pkg,
            status=SellerLedgerEntry.STATUS_PENDING_COD,
        )

        # If entries hadn't been posted yet, post them now
        if not pending_entries.exists():
            post_order_sale_to_ledger(locked_pkg, actor=actor, force_posted=True)
        else:
            pending_entries.update(
                status=SellerLedgerEntry.STATUS_POSTED,
                posted_at=now_ts,
            )

        # Check if parent order payment_status can now be marked PAID
        parent_order = locked_pkg.order
        all_pkgs = parent_order.seller_orders.all()
        all_reconciled = all(
            hasattr(pkg, "cod_reconciliation") and pkg.cod_reconciliation is not None
            for pkg in all_pkgs
        )
        if all_reconciled and parent_order.payment_status != Order.PAYMENT_STATUS_PAID:
            parent_order.payment_status = Order.PAYMENT_STATUS_PAID
            parent_order.paid_at = now_ts
            parent_order.save(update_fields=["payment_status", "paid_at", "updated_at"])

        return rec_record


# ==============================================================================
# 3. REFUNDS & RETURNS ADJUSTMENT POSTING (PHASE 6 INTEGRATION)
# ==============================================================================

def post_refund_to_ledger(
    refund_record: RefundRecord,
    actor=None,
) -> list[SellerLedgerEntry]:
    """
    Integrates Phase 6 RefundRecord with the seller ledger:
    1. Validates that refund status is PROCESSED.
    2. RETURN_REFUND (Debit): Customer refund amount deducted from merchant.
    3. COMMISSION_REVERSAL (Credit): Snapshotted commission returned to merchant.
    4. Enforces exactly-once posting via deterministic idempotency keys.
    """
    if refund_record.status != RefundRecord.STATUS_PROCESSED:
        raise LedgerServiceError(
            f"Cannot post refund #{refund_record.refund_reference} to ledger: status must be PROCESSED (current: {refund_record.status})."
        )

    created_entries = []
    seller_order = refund_record.seller_order
    seller = seller_order.seller if seller_order else None
    is_platform = seller_order.is_platform_fulfilled if seller_order else (seller is None)

    with transaction.atomic():
        # 1. Customer Return Refund Deduction (Debit)
        refund_key = f"REF-{refund_record.id}-RETURN"
        existing_ref = SellerLedgerEntry.objects.filter(idempotency_key=refund_key).first()
        if not existing_ref and refund_record.approved_amount > Decimal("0.00"):
            ref_entry = SellerLedgerEntry.objects.create(
                seller=seller,
                is_platform_fulfilled=is_platform,
                entry_type=SellerLedgerEntry.TYPE_DEBIT,
                category=SellerLedgerEntry.CATEGORY_RETURN_REFUND,
                amount=refund_record.approved_amount,
                currency=refund_record.currency or "INR",
                status=SellerLedgerEntry.STATUS_POSTED,
                source_type="refund_record",
                source_reference=refund_record.refund_reference,
                order=refund_record.order,
                seller_order=seller_order,
                refund_record=refund_record,
                idempotency_key=refund_key,
                description=f"Customer refund deduction for #{refund_record.refund_reference}",
                posted_at=timezone.now(),
                created_by=actor if hasattr(actor, "id") else None,
                audit_metadata={
                    "refund_reference": refund_record.refund_reference,
                    "external_reference": refund_record.external_reference,
                    "approved_amount": str(refund_record.approved_amount),
                    "return_request_id": refund_record.return_request_id,
                },
            )
            created_entries.append(ref_entry)
        elif existing_ref:
            created_entries.append(existing_ref)

        # 2. Commission Reversal on Return (Credit back to merchant)
        if not is_platform and refund_record.commission_reversal > Decimal("0.00"):
            comm_rev_key = f"REF-{refund_record.id}-COMM-REV"
            existing_comm_rev = SellerLedgerEntry.objects.filter(idempotency_key=comm_rev_key).first()
            if not existing_comm_rev:
                comm_rev_entry = SellerLedgerEntry.objects.create(
                    seller=seller,
                    is_platform_fulfilled=is_platform,
                    entry_type=SellerLedgerEntry.TYPE_CREDIT,
                    category=SellerLedgerEntry.CATEGORY_COMMISSION_REVERSAL,
                    amount=refund_record.commission_reversal,
                    currency=refund_record.currency or "INR",
                    status=SellerLedgerEntry.STATUS_POSTED,
                    source_type="refund_record",
                    source_reference=refund_record.refund_reference,
                    order=refund_record.order,
                    seller_order=seller_order,
                    refund_record=refund_record,
                    idempotency_key=comm_rev_key,
                    description=f"Platform commission reversal on return #{refund_record.refund_reference}",
                    posted_at=timezone.now(),
                    created_by=actor if hasattr(actor, "id") else None,
                    audit_metadata={
                        "refund_reference": refund_record.refund_reference,
                        "commission_reversal": str(refund_record.commission_reversal),
                        "seller_payable_deduction": str(refund_record.seller_payable_deduction),
                    },
                )
                created_entries.append(comm_rev_entry)
            elif existing_comm_rev:
                created_entries.append(existing_comm_rev)

    return created_entries


# ==============================================================================
# 4. ORDER CANCELLATION REVERSAL POSTING
# ==============================================================================

def post_cancellation_reversal_to_ledger(
    seller_order: SellerOrder,
    actor=None,
    reason: str = "",
) -> list[SellerLedgerEntry]:
    """
    Safely offsets previously posted unfulfilled orders upon cancellation:
    - If entries were in PENDING_COD: transitions them to REVERSED.
    - If entries were POSTED: creates offsetting debit (for sale) and credit (for commission).
    """
    created_entries = []
    seller = seller_order.seller
    is_platform = seller_order.is_platform_fulfilled or seller is None
    now_ts = timezone.now()

    with transaction.atomic():
        # Handle any PENDING_COD entries: directly mark them REVERSED
        pending_qs = SellerLedgerEntry.objects.filter(
            seller_order=seller_order,
            status=SellerLedgerEntry.STATUS_PENDING_COD,
        )
        if pending_qs.exists():
            pending_qs.update(status=SellerLedgerEntry.STATUS_REVERSED)

        # Handle POSTED entries that need explicit audit offset
        posted_sales = SellerLedgerEntry.objects.filter(
            seller_order=seller_order,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            status=SellerLedgerEntry.STATUS_POSTED,
            payout_batch__isnull=True,
        )
        for sale in posted_sales:
            cancel_sale_key = f"ORD-CANCEL-{seller_order.id}-SALE-REV"
            if not SellerLedgerEntry.objects.filter(idempotency_key=cancel_sale_key).exists():
                offset_sale = SellerLedgerEntry.objects.create(
                    seller=seller,
                    is_platform_fulfilled=is_platform,
                    entry_type=SellerLedgerEntry.TYPE_DEBIT,
                    category=SellerLedgerEntry.CATEGORY_ORDER_CANCELLATION,
                    amount=sale.amount,
                    currency=sale.currency,
                    status=SellerLedgerEntry.STATUS_POSTED,
                    source_type="seller_order",
                    source_reference=seller_order.sub_order_number,
                    order=seller_order.order,
                    seller_order=seller_order,
                    idempotency_key=cancel_sale_key,
                    description=f"Pre-dispatch cancellation reversal for #{seller_order.sub_order_number}",
                    posted_at=now_ts,
                    created_by=actor if hasattr(actor, "id") else None,
                    audit_metadata={"cancellation_reason": reason or "Cancelled order"},
                )
                created_entries.append(offset_sale)

        posted_comms = SellerLedgerEntry.objects.filter(
            seller_order=seller_order,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
            status=SellerLedgerEntry.STATUS_POSTED,
            payout_batch__isnull=True,
        )
        for comm in posted_comms:
            cancel_comm_key = f"ORD-CANCEL-{seller_order.id}-COMM-REV"
            if not SellerLedgerEntry.objects.filter(idempotency_key=cancel_comm_key).exists():
                offset_comm = SellerLedgerEntry.objects.create(
                    seller=seller,
                    is_platform_fulfilled=is_platform,
                    entry_type=SellerLedgerEntry.TYPE_CREDIT,
                    category=SellerLedgerEntry.CATEGORY_ORDER_CANCELLATION,
                    amount=comm.amount,
                    currency=comm.currency,
                    status=SellerLedgerEntry.STATUS_POSTED,
                    source_type="seller_order",
                    source_reference=seller_order.sub_order_number,
                    order=seller_order.order,
                    seller_order=seller_order,
                    idempotency_key=cancel_comm_key,
                    description=f"Commission offset on cancellation for #{seller_order.sub_order_number}",
                    posted_at=now_ts,
                    created_by=actor if hasattr(actor, "id") else None,
                )
                created_entries.append(offset_comm)

    return created_entries


# ==============================================================================
# 5. APPROVED MANUAL ADJUSTMENTS
# ==============================================================================

def create_manual_adjustment(
    entry_type: str,
    amount: Decimal,
    reason: str,
    seller: Seller | None = None,
    is_platform_fulfilled: bool = False,
    actor=None,
) -> SellerLedgerEntry:
    """
    Creates an approved administrative manual credit or debit ledger entry:
    - Requires non-empty audit reasoning.
    - Requires authenticated staff actor where available.
    - Immediately posted to the merchant's ledger.
    """
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise LedgerServiceError("Manual ledger adjustments require a mandatory explanation reason.")

    amt = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amt <= Decimal("0.00"):
        raise LedgerServiceError("Adjustment amount must be positive.")

    if entry_type not in (SellerLedgerEntry.TYPE_CREDIT, SellerLedgerEntry.TYPE_DEBIT):
        raise LedgerServiceError("Invalid entry type. Must be credit or debit.")

    if not seller and not is_platform_fulfilled:
        raise LedgerServiceError("Either an approved seller or platform fulfillment flag is required.")

    idempotency_key = f"MANUAL-{uuid.uuid4().hex.upper()}"
    entry = SellerLedgerEntry.objects.create(
        seller=seller,
        is_platform_fulfilled=is_platform_fulfilled,
        entry_type=entry_type,
        category=SellerLedgerEntry.CATEGORY_MANUAL_ADJUSTMENT,
        amount=amt,
        currency="INR",
        status=SellerLedgerEntry.STATUS_POSTED,
        source_type="manual",
        source_reference=f"ADJ-{timezone.now().strftime('%Y%m%d')}",
        idempotency_key=idempotency_key,
        description=clean_reason[:255],
        posted_at=timezone.now(),
        created_by=actor if hasattr(actor, "id") else None,
        audit_metadata={
            "authorized_by": getattr(actor, "username", "admin"),
            "full_reason": clean_reason,
        },
    )
    return entry


# ==============================================================================
# 6. PAYOUT ELIGIBILITY CALCULATIONS
# ==============================================================================

def calculate_seller_payout_eligibility(
    seller: Seller | None = None,
    is_platform_fulfilled: bool = False,
    as_of_date=None,
    cycle_month: str | None = None,
    payout_cycle: str | None = None,
) -> dict:
    """
    Evaluates net payout eligibility for a specific merchant or platform warehouse:
    15-Day Payout Rules (Prompt 4):
    1. Cycles: C1 (1st-15th, calc on 16th), C2 (16th-month-end, calc on 1st of next month).
    2. Requires 5 complete days (120 full hours) after delivery before an order package becomes eligible.
    3. Excludes orders with unresolved returns, refunds, disputes, or unreconciled COD remittance.
    4. Ineligible orders are kept on hold and carried forward into a future cycle.
    5. Manual hold release (is_hold_released=True) waives ONLY the 120h cooling hold.
    6. Double-entry accounting: Net balance = sum(eligible credits) - sum(eligible debits).
    7. Fully timezone-aware dates.
    """
    hold_days = get_payout_hold_days()
    cycle_to_use = payout_cycle or cycle_month

    if cycle_to_use:
        canonical_cycle, cycle_payout_date, prev_month_start, prev_month_end = get_cycle_bounds(
            cycle_str=cycle_to_use,
            as_of_date=as_of_date,
        )
        if as_of_date is None:
            as_of_date = cycle_payout_date
    else:
        canonical_cycle = None
        cycle_payout_date = None
        prev_month_start = None
        prev_month_end = None
        if as_of_date is None:
            as_of_date = timezone.now()

    hold_hours = hold_days * 24  # 5 days = 120 full hours
    cutoff_delivery_time = as_of_date - timedelta(hours=hold_hours)

    # Base query for unbatched entries for this seller
    base_qs = SellerLedgerEntry.objects.filter(
        payout_batch__isnull=True,
    )
    if is_platform_fulfilled or seller is None:
        base_qs = base_qs.filter(is_platform_fulfilled=True)
    else:
        base_qs = base_qs.filter(seller=seller, is_platform_fulfilled=False)

    eligible_entry_ids = []
    held_entry_ids = []
    held_entry_details = []

    for entry in base_qs.select_related("seller_order", "seller_order__order"):
        meta = entry.audit_metadata if isinstance(entry.audit_metadata, dict) else {}
        is_released = getattr(entry, "is_hold_released", False)

        # 1. COD pending entries are strictly excluded
        if entry.status == SellerLedgerEntry.STATUS_PENDING_COD:
            held_entry_ids.append(entry.id)
            held_entry_details.append({
                "entry_id": entry.id,
                "amount": entry.amount,
                "type": entry.entry_type,
                "category": entry.category,
                "reason": "Courier COD cash remittance not yet reconciled.",
                "is_cooling_hold": False,
                "is_hold_released": is_released,
                "hold_release_reason": getattr(entry, "hold_release_reason", ""),
                "hold_release_history": meta.get("hold_release_history", []),
            })
            continue

        if entry.status != SellerLedgerEntry.STATUS_POSTED:
            # Reversed or non-posted entries do not qualify
            continue

        # 2. Cycle cutoff: If cycle_month is specified, orders/entries from future cycles are carried forward
        if prev_month_end:
            order_date = None
            if entry.seller_order_id:
                order_date = entry.seller_order.created_at
            else:
                order_date = entry.created_at

            if order_date and order_date > prev_month_end:
                held_entry_ids.append(entry.id)
                held_entry_details.append({
                    "entry_id": entry.id,
                    "amount": entry.amount,
                    "type": entry.entry_type,
                    "category": entry.category,
                    "reason": f"Future cycle transaction (carried forward to next cycle after {prev_month_end.strftime('%d %b %Y')}).",
                    "is_cooling_hold": False,
                    "is_hold_released": is_released,
                    "hold_release_reason": getattr(entry, "hold_release_reason", ""),
                    "hold_release_history": meta.get("hold_release_history", []),
                })
                continue

        # 3. If linked to a package, verify delivery, cooling window, returns, refunds, and COD
        if entry.seller_order_id:
            pkg = entry.seller_order

            # Package delivery status
            if pkg.status != SellerOrder.STATUS_DELIVERED:
                held_entry_ids.append(entry.id)
                held_entry_details.append({
                    "entry_id": entry.id,
                    "amount": entry.amount,
                    "type": entry.entry_type,
                    "category": entry.category,
                    "reason": f"Delivery not confirmed (status: {pkg.get_status_display()}).",
                    "is_cooling_hold": False,
                    "is_hold_released": is_released,
                    "hold_release_reason": getattr(entry, "hold_release_reason", ""),
                    "hold_release_history": meta.get("hold_release_history", []),
                })
                continue

            # 5-day cooling window post-delivery (120 full hours)
            # If entry has is_hold_released=True, cooling hold is waived
            del_time = pkg.delivered_at or pkg.updated_at
            is_released = getattr(entry, "is_hold_released", False)
            meta = entry.audit_metadata if isinstance(entry.audit_metadata, dict) else {}
            if not is_released and del_time and (as_of_date - del_time) < timedelta(hours=hold_hours):
                held_entry_ids.append(entry.id)
                seconds_remaining = max(1, ((del_time + timedelta(hours=hold_hours)) - as_of_date).total_seconds())
                hours_remaining = max(1, int(seconds_remaining // 3600) + (1 if seconds_remaining % 3600 else 0))
                days_left = max(1, (hours_remaining + 23) // 24)
                held_entry_details.append({
                    "entry_id": entry.id,
                    "amount": entry.amount,
                    "type": entry.entry_type,
                    "category": entry.category,
                    "reason": f"Delivery cooling period active ({days_left}d / {hours_remaining}h remaining).",
                    "is_cooling_hold": True,
                    "is_hold_released": False,
                    "hold_release_reason": "",
                    "hold_release_history": meta.get("hold_release_history", []),
                })
                continue

            # Unresolved returns or exchanges
            unresolved_returns = ReturnRequest.objects.filter(
                seller_order=pkg,
            ).exclude(
                status__in=[
                    ReturnRequest.STATUS_COMPLETED,
                    ReturnRequest.STATUS_REJECTED,
                    ReturnRequest.STATUS_CANCELLED,
                ]
            ).exists()
            if unresolved_returns:
                held_entry_ids.append(entry.id)
                held_entry_details.append({
                    "entry_id": entry.id,
                    "amount": entry.amount,
                    "type": entry.entry_type,
                    "category": entry.category,
                    "reason": "Active return or exchange request unresolved.",
                    "is_cooling_hold": False,
                    "is_hold_released": is_released,
                    "hold_release_reason": getattr(entry, "hold_release_reason", ""),
                    "hold_release_history": meta.get("hold_release_history", []),
                })
                continue

            # Unresolved refunds (both sub-order package and parent order-level)
            unresolved_refunds = RefundRecord.objects.filter(
                Q(seller_order=pkg)
                | Q(order=pkg.order, seller_order__isnull=True),
                status__in=[
                    RefundRecord.STATUS_PENDING,
                    RefundRecord.STATUS_PROCESSING,
                    RefundRecord.STATUS_FAILED,
                ],
            ).exists()
            if unresolved_refunds:
                held_entry_ids.append(entry.id)
                held_entry_details.append({
                    "entry_id": entry.id,
                    "amount": entry.amount,
                    "type": entry.entry_type,
                    "category": entry.category,
                    "reason": "Customer refund record in pending/disputed state.",
                    "is_cooling_hold": False,
                    "is_hold_released": is_released,
                    "hold_release_reason": getattr(entry, "hold_release_reason", ""),
                    "hold_release_history": meta.get("hold_release_history", []),
                })
                continue

            # COD reconciliation check on the package itself
            if pkg.order.payment_method == Order.PAYMENT_METHOD_COD:
                if not hasattr(pkg, "cod_reconciliation") or pkg.cod_reconciliation is None:
                    held_entry_ids.append(entry.id)
                    held_entry_details.append({
                        "entry_id": entry.id,
                        "amount": entry.amount,
                        "type": entry.entry_type,
                        "category": entry.category,
                        "reason": "Courier COD remittance pending reconciliation.",
                        "is_cooling_hold": False,
                        "is_hold_released": is_released,
                        "hold_release_reason": getattr(entry, "hold_release_reason", ""),
                        "hold_release_history": meta.get("hold_release_history", []),
                    })
                    continue

        # Matured and eligible
        eligible_entry_ids.append(entry.id)

    eligible_qs = SellerLedgerEntry.objects.filter(id__in=eligible_entry_ids)
    held_qs = SellerLedgerEntry.objects.filter(id__in=held_entry_ids)

    # Financial aggregates for eligible entries
    credit_sum = (
        eligible_qs.filter(entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    debit_sum = (
        eligible_qs.filter(entry_type=SellerLedgerEntry.TYPE_DEBIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    net_eligible = (credit_sum - debit_sum).quantize(Decimal("0.01"))

    # Line item breakdown by category
    eligible_sales = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_ORDER_SALE).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    eligible_commission = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_COMMISSION).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    eligible_shipping_credit = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_SHIPPING_CREDIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    eligible_shipping_debit = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_SHIPPING_DEBIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    eligible_refunds = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_RETURN_REFUND).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    eligible_comm_reversals = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_COMMISSION_REVERSAL).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    eligible_cancellations = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_ORDER_CANCELLATION).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    adj_credits = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_MANUAL_ADJUSTMENT, entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    adj_debits = (
        eligible_qs.filter(category=SellerLedgerEntry.CATEGORY_MANUAL_ADJUSTMENT, entry_type=SellerLedgerEntry.TYPE_DEBIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    eligible_adjustments = adj_credits - adj_debits

    # Category totals
    category_totals = {}
    for cat_code, cat_label in SellerLedgerEntry.CATEGORY_CHOICES:
        c_sub = (
            eligible_qs.filter(category=cat_code).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        if c_sub > Decimal("0.00"):
            category_totals[cat_code] = {
                "label": cat_label,
                "amount": c_sub,
            }

    # Held entries amount
    held_credit = (
        held_qs.filter(entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    held_debit = (
        held_qs.filter(entry_type=SellerLedgerEntry.TYPE_DEBIT).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    held_net = (held_credit - held_debit).quantize(Decimal("0.01"))

    # Active existing batch for this seller & cycle, if any
    existing_batch = None
    if canonical_cycle:
        existing_batch = PayoutBatch.objects.filter(
            seller=seller,
            is_platform_fulfilled=is_platform_fulfilled,
            payout_cycle=canonical_cycle,
        ).exclude(status__in=[PayoutBatch.STATUS_FAILED, PayoutBatch.STATUS_REVERSED]).first()

    refunds_and_adjustments = (
        eligible_refunds + eligible_cancellations - eligible_comm_reversals - eligible_adjustments
    ).quantize(Decimal("0.01"))

    return {
        "seller": seller,
        "is_platform_fulfilled": is_platform_fulfilled,
        "eligible_entries_queryset": eligible_qs,
        "eligible_entry_ids": eligible_entry_ids,
        "eligible_count": len(eligible_entry_ids),
        "total_credits": credit_sum,
        "total_debits": debit_sum,
        "net_eligible_balance": net_eligible,
        "is_above_threshold": net_eligible >= get_min_payout_threshold(),
        "held_entry_ids": held_entry_ids,
        "held_count": len(held_entry_ids),
        "held_balance": held_net,
        "held_entry_details": held_entry_details,
        "category_breakdown": category_totals,
        "as_of_date": as_of_date,
        "cutoff_delivery_time": cutoff_delivery_time,
        "hold_days": hold_days,
        "bank_verification_status": seller.bank_verification_status if seller else "verified",
        "bank_verification_label": seller.get_bank_verification_status_display() if seller else "Verified (Central)",
        "is_bank_verified": seller.is_bank_payout_eligible if seller else True,
        "masked_bank_account": seller.get_masked_account_number() if seller else "PLATFORM-CENTRAL",
        "bank_name": seller.bank_name if seller else "Central Platform Treasury",
        "bank_ifsc": seller.bank_ifsc_code if seller else "N/A",
        # Category breakdown shortcuts
        "eligible_sales": eligible_sales,
        "eligible_commission": eligible_commission,
        "eligible_shipping_credit": eligible_shipping_credit,
        "eligible_shipping_debit": eligible_shipping_debit,
        "eligible_refunds": eligible_refunds + eligible_cancellations,
        "eligible_commission_reversals": eligible_comm_reversals,
        "eligible_adjustments": eligible_adjustments,
        "refunds_and_adjustments": refunds_and_adjustments,
        "canonical_cycle": canonical_cycle,
        "cycle_payout_date": cycle_payout_date,
        "period_start": prev_month_start,
        "period_end": prev_month_end,
        "prev_month_start": prev_month_start,
        "prev_month_end": prev_month_end,
        "existing_batch": existing_batch,
    }


# ==============================================================================
# 7. PAYOUT BATCH CREATION & EXECUTION (CONCURRENCY-SAFE & MOCK ONLY)
# ==============================================================================

def create_payout_batch(
    seller: Seller | None = None,
    is_platform_fulfilled: bool = False,
    actor=None,
    notes: str = "",
    enforce_bank_verification: bool = False,
    payout_cycle: str | None = None,
) -> PayoutBatch:
    """
    Prepares a new PayoutBatch bundling all currently eligible, unbatched ledger entries:
    1. Uses transaction.atomic() and select_for_update() to prevent double-consumption.
    2. Validates that net balance meets the minimum payout threshold.
    3. Prevents duplicate batches for the same seller and monthly cycle.
    4. Atomically links locked ledger entries to the new PayoutBatch (status=DRAFT).
    5. Snapshots masked bank account, IFSC, and beneficiary for immutable financial audit.
    6. Optionally enforces admin bank verification before batch preparation.
    7. Records an immutable PayoutAuditLog entry.
    """
    if enforce_bank_verification and seller and not is_platform_fulfilled:
        if not seller.is_bank_payout_eligible:
            raise PayoutEligibilityError(
                f"Seller '{seller.store_name}' does not have a verified bank account "
                f"(Status: {seller.get_bank_verification_status_display()}). "
                f"Administrative bank verification is required before payouts can be prepared."
            )

    with transaction.atomic():
        # Duplicate batch prevention for the same seller and monthly cycle
        if payout_cycle:
            existing_cycle_batch = (
                PayoutBatch.objects.select_for_update()
                .filter(
                    seller=seller,
                    is_platform_fulfilled=is_platform_fulfilled,
                    payout_cycle=payout_cycle,
                )
                .exclude(status__in=[PayoutBatch.STATUS_FAILED, PayoutBatch.STATUS_REVERSED])
                .first()
            )
            if existing_cycle_batch:
                raise DuplicatePostingError(
                    f"Payout batch #{existing_cycle_batch.batch_reference} already exists for "
                    f"'{seller.store_name if seller else 'Platform'}' in monthly cycle {payout_cycle} "
                    f"(Status: {existing_cycle_batch.get_status_display()})."
                )

        eligibility = calculate_seller_payout_eligibility(
            seller=seller,
            is_platform_fulfilled=is_platform_fulfilled,
            cycle_month=payout_cycle,
        )

        eligible_ids = eligibility["eligible_entry_ids"]
        net_amount = eligibility["net_eligible_balance"]
        min_threshold = get_min_payout_threshold()

        if not eligible_ids:
            raise PayoutEligibilityError("No eligible ledger entries are currently available for payout.")

        if net_amount < min_threshold:
            raise PayoutEligibilityError(
                f"Net eligible balance (₹{net_amount}) is below the minimum payout threshold of ₹{min_threshold}."
            )

        # Acquire pessimistic row-level lock on the eligible entries
        locked_entries = list(
            SellerLedgerEntry.objects.select_for_update().filter(id__in=eligible_ids)
        )

        # Double-check that none of the locked entries got consumed concurrently
        already_batched = [e for e in locked_entries if e.payout_batch_id is not None]
        if already_batched:
            raise PayoutEligibilityError(
                f"{len(already_batched)} ledger entries have already been included in another payout batch."
            )

        # Determine period bounds
        earliest_dt = min(e.created_at for e in locked_entries)
        latest_dt = max(e.created_at for e in locked_entries)

        # Generate unique batch reference
        date_str = timezone.now().strftime("%Y%m%d")
        rand_hex = uuid.uuid4().hex[:6].upper()
        batch_ref = f"PAY-{date_str}-{rand_hex}"
        while PayoutBatch.objects.filter(batch_reference=batch_ref).exists():
            rand_hex = uuid.uuid4().hex[:6].upper()
            batch_ref = f"PAY-{date_str}-{rand_hex}"

        # Create PayoutBatch record in DRAFT status with bank snapshots
        batch = PayoutBatch.objects.create(
            batch_reference=batch_ref,
            seller=seller,
            is_platform_fulfilled=is_platform_fulfilled,
            amount=net_amount,
            currency="INR",
            status=PayoutBatch.STATUS_DRAFT,
            payout_method="mock_bank_transfer",
            external_reference="",
            payout_cycle=payout_cycle or "",
            created_by=actor if hasattr(actor, "id") else None,
            bank_account_masked=seller.get_masked_account_number() if seller else "PLATFORM-CENTRAL",
            bank_ifsc_code=seller.bank_ifsc_code if seller else "N/A",
            bank_beneficiary_name=(seller.bank_account_holder_name or seller.store_name) if seller else "Fitmeezo Central",
            eligible_entries_count=len(locked_entries),
            period_start=earliest_dt,
            period_end=latest_dt,
            notes=notes or f"Simulated payout batch for {batch_ref} (Cycle: {payout_cycle or 'Unspecified'}).",
            is_mock=True,
            provider_status="not_configured",
            audit_metadata={
                "total_credits": str(eligibility["total_credits"]),
                "total_debits": str(eligibility["total_debits"]),
                "created_by_username": getattr(actor, "username", "admin"),
                "covered_entry_ids": [e.id for e in locked_entries],
                "bank_account_masked": seller.get_masked_account_number() if seller else "PLATFORM-CENTRAL",
                "bank_ifsc": seller.bank_ifsc_code if seller else "N/A",
                "payout_cycle": payout_cycle or "",
            },
        )

        # Atomically link all locked entries to this batch
        SellerLedgerEntry.objects.filter(id__in=eligible_ids).update(payout_batch=batch)

        # Record audit log
        PayoutAuditLog.objects.create(
            payout_batch=batch,
            action=PayoutAuditLog.ACTION_CREATED,
            actor=actor if hasattr(actor, "id") else None,
            from_status="",
            to_status=PayoutBatch.STATUS_DRAFT,
            reason=f"Batch prepared with {len(locked_entries)} entries (Amount: ₹{net_amount}, Cycle: {payout_cycle or 'General'}).",
            metadata={"eligible_entries_count": len(locked_entries), "net_amount": str(net_amount)},
        )

        return batch


def approve_payout_batch(
    payout_batch: PayoutBatch,
    actor=None,
) -> tuple[bool, str, PayoutBatch]:
    """
    Server-side authorized approval for a prepared payout batch:
    - Verifies batch is not on hold.
    - Requires verified seller bank account.
    - Revalidates eligibility.
    - Prevents repeated approval on already completed/approved batches.
    - Transitions to STATUS_PENDING (ready for execution) and logs audit entry.
    """
    with transaction.atomic():
        locked_batch = (
            PayoutBatch.objects.select_for_update()
            .filter(pk=payout_batch.pk)
            .first()
        )
        if not locked_batch:
            raise LedgerServiceError("Payout batch does not exist.")

        if locked_batch.status == PayoutBatch.STATUS_ON_HOLD:
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} is ON HOLD ({locked_batch.hold_reason or 'Admin hold'}). "
                "The hold must be explicitly released before this batch can be approved."
            )

        if locked_batch.status == PayoutBatch.STATUS_COMPLETED:
            return True, f"Payout batch #{locked_batch.batch_reference} has already been completed.", locked_batch

        if locked_batch.status == PayoutBatch.STATUS_FAILED:
            raise LedgerServiceError(
                f"Cannot approve rejected/failed payout batch #{locked_batch.batch_reference}."
            )

        # Maker-Checker restriction: Creator cannot approve own batch
        if locked_batch.created_by_id and actor and hasattr(actor, "id") and locked_batch.created_by_id == actor.id:
            raise PayoutAuthorizationError(
                f"Dual authorization (Maker-Checker) restriction: Payout batch #{locked_batch.batch_reference} "
                f"was prepared by you ({actor.username}). A different authorized administrator must audit and approve it."
            )

        # Revalidate seller bank eligibility
        if locked_batch.seller and not locked_batch.is_platform_fulfilled:
            if not locked_batch.seller.is_bank_payout_eligible:
                raise PayoutEligibilityError(
                    f"Cannot approve payout: seller '{locked_batch.seller.store_name}' bank details are not verified "
                    f"({locked_batch.seller.get_bank_verification_status_display()})."
                )

        if locked_batch.approved_at and locked_batch.status == PayoutBatch.STATUS_PENDING:
            return True, f"Payout batch #{locked_batch.batch_reference} is already approved and pending execution.", locked_batch

        old_status = locked_batch.status
        now_ts = timezone.now()
        locked_batch.status = PayoutBatch.STATUS_PENDING
        locked_batch.approved_at = now_ts
        locked_batch.approved_by = actor if hasattr(actor, "id") else None
        locked_batch.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])

        PayoutAuditLog.objects.create(
            payout_batch=locked_batch,
            action=PayoutAuditLog.ACTION_APPROVED,
            actor=actor if hasattr(actor, "id") else None,
            from_status=old_status,
            to_status=locked_batch.status,
            reason=f"Approved by {getattr(actor, 'username', 'admin')}.",
        )

        return True, f"Payout batch #{locked_batch.batch_reference} approved successfully (Amount: ₹{locked_batch.amount}).", locked_batch


def hold_payout_batch(
    payout_batch: PayoutBatch,
    actor=None,
    reason: str = "",
) -> tuple[bool, str, PayoutBatch]:
    """
    Places an individual seller payout batch ON HOLD without blocking other sellers:
    - Requires a mandatory explanation reason.
    - Transitions status to STATUS_ON_HOLD.
    - Prevents disbursement execution while held.
    - Records hold reason, admin identity, and timestamp.
    """
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise LedgerServiceError("A mandatory hold reason is required to place a payout batch on hold.")

    with transaction.atomic():
        locked_batch = (
            PayoutBatch.objects.select_for_update()
            .filter(pk=payout_batch.pk)
            .first()
        )
        if not locked_batch:
            raise LedgerServiceError("Payout batch does not exist.")

        if locked_batch.status == PayoutBatch.STATUS_COMPLETED:
            raise LedgerServiceError("Cannot hold an already completed payout batch. Use reversal instead.")

        if locked_batch.status == PayoutBatch.STATUS_REVERSED:
            raise LedgerServiceError("Cannot hold a reversed payout batch.")

        if locked_batch.status == PayoutBatch.STATUS_ON_HOLD:
            return True, f"Payout batch #{locked_batch.batch_reference} is already on hold.", locked_batch

        old_status = locked_batch.status
        now_ts = timezone.now()
        locked_batch.status = PayoutBatch.STATUS_ON_HOLD
        locked_batch.hold_reason = clean_reason
        locked_batch.held_at = now_ts
        locked_batch.held_by = actor if hasattr(actor, "id") else None
        locked_batch.save(update_fields=["status", "hold_reason", "held_at", "held_by", "updated_at"])

        PayoutAuditLog.objects.create(
            payout_batch=locked_batch,
            action=PayoutAuditLog.ACTION_HELD,
            actor=actor if hasattr(actor, "id") else None,
            from_status=old_status,
            to_status=PayoutBatch.STATUS_ON_HOLD,
            reason=clean_reason,
            metadata={"held_by": getattr(actor, "username", "admin"), "timestamp": now_ts.isoformat()},
        )

        return True, f"Payout batch #{locked_batch.batch_reference} placed on HOLD: {clean_reason}", locked_batch


def release_payout_batch(
    payout_batch: PayoutBatch,
    actor=None,
    notes: str = "",
) -> tuple[bool, str, PayoutBatch]:
    """
    Releases a previously held payout batch:
    - Validates batch is currently in STATUS_ON_HOLD.
    - Revalidates bank verification status.
    - Transitions to STATUS_PENDING (or STATUS_DRAFT) and records release decision.
    """
    with transaction.atomic():
        locked_batch = (
            PayoutBatch.objects.select_for_update()
            .filter(pk=payout_batch.pk)
            .first()
        )
        if not locked_batch:
            raise LedgerServiceError("Payout batch does not exist.")

        if locked_batch.status != PayoutBatch.STATUS_ON_HOLD:
            raise LedgerServiceError(
                f"Batch #{locked_batch.batch_reference} is not on hold (current status: {locked_batch.get_status_display()})."
            )

        if locked_batch.seller and not locked_batch.is_platform_fulfilled:
            if not locked_batch.seller.is_bank_payout_eligible:
                raise PayoutEligibilityError(
                    f"Cannot release hold: seller '{locked_batch.seller.store_name}' bank details are not verified."
                )

        old_status = locked_batch.status
        now_ts = timezone.now()
        target_status = PayoutBatch.STATUS_PENDING if locked_batch.approved_at else PayoutBatch.STATUS_DRAFT
        locked_batch.status = target_status
        locked_batch.released_at = now_ts
        locked_batch.released_by = actor if hasattr(actor, "id") else None
        locked_batch.save(update_fields=["status", "released_at", "released_by", "updated_at"])

        clean_notes = (notes or "").strip() or f"Hold released by {getattr(actor, 'username', 'admin')}."
        PayoutAuditLog.objects.create(
            payout_batch=locked_batch,
            action=PayoutAuditLog.ACTION_RELEASED,
            actor=actor if hasattr(actor, "id") else None,
            from_status=old_status,
            to_status=target_status,
            reason=clean_notes,
            metadata={"released_by": getattr(actor, "username", "admin"), "timestamp": now_ts.isoformat()},
        )

        return True, f"Hold released for payout batch #{locked_batch.batch_reference}. Status updated to {locked_batch.get_status_display()}.", locked_batch


def release_hold_balance_entries(
    seller: Seller | None,
    entry_ids: list[int],
    reason: str,
    actor=None,
) -> tuple[int, list[SellerLedgerEntry]]:
    """
    Independently releases cooling-period holds on specific posted ledger entries for a seller.

    Guarantees:
    1. Authorization: Requires staff user with finance management permissions (Super Admin or Finance Manager).
    2. Atomic & Locked: Uses select_for_update() inside transaction.atomic() to prevent concurrency anomalies.
    3. Mandatory Audit Reason: Requires a non-empty explanation reason.
    4. Strict Isolation: Verifies every entry strictly belongs to the specified seller.
    5. Rejection of Ineligible: Rejects already batched, reversed, cancelled, or unposted entries.
    6. Non-Disbursing: Releasing an entry's hold only makes it eligible for future cycle payout
       batches; it strictly DOES NOT create a PayoutBatch, mark any batch completed, post a payout debit,
       or call any external banking or payment gateway rails.
    7. Safeguard Preservation: Overrides ONLY the delivery cooling hold; orders with unresolved returns,
       refund disputes, or unreconciled COD remain held during payout eligibility calculation.
    8. Idempotent: If an entry is already released, it safely updates audit metadata without duplicate effect.

    Returns:
        tuple[int, list[SellerLedgerEntry]]: (count_released, updated_entries)
    """
    from core.admin_permissions import ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, get_user_role

    is_authorized = False
    if actor and actor.is_authenticated and actor.is_active:
        if actor.is_superuser:
            is_authorized = True
        elif actor.is_staff:
            role = get_user_role(actor)
            if role in (ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER):
                is_authorized = True

    if not is_authorized:
        raise LedgerServiceError(
            "Permission denied: Staff user with Finance Manager or Super Admin role is required to release holds."
        )

    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise LedgerServiceError("A mandatory explanation reason is required to release held balances.")

    if not entry_ids:
        raise LedgerServiceError("At least one ledger entry ID must be specified.")

    with transaction.atomic():
        locked_entries = list(
            SellerLedgerEntry.objects.select_for_update().filter(id__in=entry_ids)
        )
        if len(locked_entries) != len(set(entry_ids)):
            raise LedgerServiceError("One or more specified ledger entries were not found.")

        updated_entries = []
        now_ts = timezone.now()

        for entry in locked_entries:
            # 1. Seller isolation check
            if seller is not None:
                if entry.seller_id != seller.id:
                    raise LedgerServiceError(
                        f"Entry #{entry.id} belongs to a different seller ({entry.seller.store_name if entry.seller else 'Platform'}) "
                        f"and cannot be released for '{seller.store_name}'."
                    )
            else:
                if not entry.is_platform_fulfilled:
                    raise LedgerServiceError(
                        f"Entry #{entry.id} belongs to third-party merchant and cannot be released for Platform Store."
                    )

            # 2. Check posting status
            if entry.status != SellerLedgerEntry.STATUS_POSTED:
                raise LedgerServiceError(
                    f"Entry #{entry.id} has status '{entry.get_status_display()}'. Only POSTED entries can be hold-released."
                )

            # 3. Check if already included in a payout batch
            if entry.payout_batch_id is not None:
                raise LedgerServiceError(
                    f"Entry #{entry.id} is already bundled into PayoutBatch #{entry.payout_batch.batch_reference}."
                )

            # 4. Check if package has been cancelled
            if entry.seller_order and entry.seller_order.status == SellerOrder.STATUS_CANCELLED:
                raise LedgerServiceError(
                    f"Entry #{entry.id} belongs to a cancelled sub-order (#{entry.seller_order.sub_order_number}) and cannot be released."
                )

            # 5. Record hold release on the entry
            entry.is_hold_released = True
            entry.hold_released_at = now_ts
            entry.hold_released_by = actor
            entry.hold_release_reason = clean_reason

            meta = dict(entry.audit_metadata) if isinstance(entry.audit_metadata, dict) else {}
            release_event = {
                "released_by_username": getattr(actor, "username", "admin"),
                "released_at": now_ts.isoformat(),
                "reason": clean_reason,
            }
            raw_history = meta.get("hold_release_history")
            if isinstance(raw_history, list):
                history = list(raw_history)
            else:
                history = []
                if "hold_release" in meta and isinstance(meta["hold_release"], dict):
                    history.append(meta["hold_release"])
            history.append(release_event)
            meta["hold_release_history"] = history
            meta["hold_release"] = release_event
            entry.audit_metadata = meta
            entry.save(update_fields=[
                "is_hold_released",
                "hold_released_at",
                "hold_released_by",
                "hold_release_reason",
                "audit_metadata",
            ])
            updated_entries.append(entry)

        return len(updated_entries), updated_entries


def reject_payout_batch(
    payout_batch: PayoutBatch,
    actor=None,
    reason: str = "",
) -> tuple[bool, str, PayoutBatch]:
    """
    Rejects a payout batch with a mandatory explanation reason:
    - Transitions status to STATUS_FAILED.
    - Releases covered ledger entries for future evaluation.
    - Records admin identity, reason, and timestamp.
    """
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise LedgerServiceError("A mandatory rejection reason is required to reject a payout batch.")

    with transaction.atomic():
        locked_batch = (
            PayoutBatch.objects.select_for_update()
            .filter(pk=payout_batch.pk)
            .first()
        )
        if not locked_batch:
            raise LedgerServiceError("Payout batch does not exist.")

        if locked_batch.status == PayoutBatch.STATUS_COMPLETED:
            raise LedgerServiceError("Cannot reject an already completed payout batch. Use reversal instead.")

        old_status = locked_batch.status
        now_ts = timezone.now()
        locked_batch.status = PayoutBatch.STATUS_FAILED
        locked_batch.rejection_reason = clean_reason
        locked_batch.failure_reason = f"Rejected by admin: {clean_reason}"
        locked_batch.rejected_at = now_ts
        locked_batch.rejected_by = actor if hasattr(actor, "id") else None
        locked_batch.save(
            update_fields=[
                "status",
                "rejection_reason",
                "failure_reason",
                "rejected_at",
                "rejected_by",
                "updated_at",
            ]
        )

        # Release covered entries for future cycles
        SellerLedgerEntry.objects.filter(payout_batch=locked_batch).update(payout_batch=None)

        PayoutAuditLog.objects.create(
            payout_batch=locked_batch,
            action=PayoutAuditLog.ACTION_REJECTED,
            actor=actor if hasattr(actor, "id") else None,
            from_status=old_status,
            to_status=PayoutBatch.STATUS_FAILED,
            reason=clean_reason,
            metadata={"rejected_by": getattr(actor, "username", "admin"), "timestamp": now_ts.isoformat()},
        )

        return True, f"Payout batch #{locked_batch.batch_reference} rejected: {clean_reason}. Covered entries released.", locked_batch


def process_mock_payout_batch(
    payout_batch: PayoutBatch,
    actor=None,
    simulate_failure: bool = False,
    failure_reason: str = "",
    enforce_bank_verification: bool = False,
) -> tuple[bool, str, PayoutBatch]:
    """
    Executes a simulated internal payout batch settlement:
    - Never calls external banking or Razorpay APIs (FITMEEZO_MOCK_PAYOUTS is strictly enforced).
    - If batch is ON HOLD: strictly prevents disbursement.
    - If simulate_failure=True: marks batch as FAILED, unlinks covered ledger entries for retry.
    - If successful: marks batch as COMPLETED, generates MOCK-PAY-XXXX external ref, and posts
      an auditable PAYOUT debit entry into the ledger to close the balance.
    """
    if not getattr(settings, "FITMEEZO_MOCK_PAYOUTS", True):
        raise LedgerServiceError("Live payouts are strictly disabled. FITMEEZO_MOCK_PAYOUTS must be True.")

    with transaction.atomic():
        locked_batch = (
            PayoutBatch.objects.select_for_update()
            .filter(pk=payout_batch.pk)
            .first()
        )
        if not locked_batch:
            raise LedgerServiceError("Payout batch does not exist.")

        # Maker-Checker restriction: Creator cannot disburse own batch
        if locked_batch.created_by_id and actor and hasattr(actor, "id") and locked_batch.created_by_id == actor.id:
            raise PayoutAuthorizationError(
                f"Dual authorization (Maker-Checker) restriction: Batch #{locked_batch.batch_reference} "
                f"creator ({actor.username}) cannot execute disbursement. A different authorized administrator must disburse it."
            )

        # Held payment must not be sent
        if locked_batch.status == PayoutBatch.STATUS_ON_HOLD:
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} is ON HOLD ({locked_batch.hold_reason or 'Admin hold'}). "
                "Held payments cannot be disbursed."
            )

        # Final settlement batches must NOT be processed via mock payout
        if locked_batch.batch_type == PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT:
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} is a final settlement batch. "
                "Final settlement batches cannot be settled via mock payout; they must be completed through the verified manual bank transfer workflow."
            )

        # Duplicate transfer protection
        if locked_batch.status == PayoutBatch.STATUS_COMPLETED:
            return True, f"Duplicate disbursement prevented: batch #{locked_batch.batch_reference} is already completed.", locked_batch

        if locked_batch.status not in (
            PayoutBatch.STATUS_DRAFT,
            PayoutBatch.STATUS_PROCESSING,
            PayoutBatch.STATUS_PENDING,
        ):
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} cannot be processed from status: {locked_batch.get_status_display()}."
            )

        # Non-platform sellers must have verified bank accounts when enforced or when cycle is set
        must_enforce_bank = enforce_bank_verification or bool(locked_batch.payout_cycle)
        if must_enforce_bank and locked_batch.seller and not locked_batch.is_platform_fulfilled:
            if not locked_batch.seller.is_bank_payout_eligible:
                clean_reason = "Seller bank account is not verified. Platform admin verification required."
                locked_batch.status = PayoutBatch.STATUS_FAILED
                locked_batch.failure_reason = clean_reason
                locked_batch.provider_status = "simulated_failed"
                locked_batch.retry_count = locked_batch.retry_count + 1
                locked_batch.processed_at = timezone.now()
                locked_batch.processed_by = actor if hasattr(actor, "id") else None
                locked_batch.save(update_fields=["status", "failure_reason", "provider_status", "retry_count", "processed_at", "processed_by", "updated_at"])

                SellerLedgerEntry.objects.filter(payout_batch=locked_batch).update(payout_batch=None)

                PayoutAuditLog.objects.create(
                    payout_batch=locked_batch,
                    action=PayoutAuditLog.ACTION_FAILED,
                    actor=actor if hasattr(actor, "id") else None,
                    from_status=locked_batch.status,
                    to_status=PayoutBatch.STATUS_FAILED,
                    reason=clean_reason,
                    metadata={"simulated": True},
                )
                return False, f"TEST/SIMULATED payout rejected: {clean_reason}", locked_batch

        old_status = locked_batch.status

        if simulate_failure:
            clean_reason = failure_reason or "Simulated automated banking network transfer rejection."
            locked_batch.status = PayoutBatch.STATUS_FAILED
            locked_batch.failure_reason = clean_reason
            locked_batch.provider_status = "simulated_failed"
            locked_batch.retry_count = locked_batch.retry_count + 1
            locked_batch.processed_at = timezone.now()
            locked_batch.processed_by = actor if hasattr(actor, "id") else None
            locked_batch.save(update_fields=["status", "failure_reason", "provider_status", "retry_count", "processed_at", "processed_by", "updated_at"])

            # Release covered ledger entries so they can be re-evaluated
            SellerLedgerEntry.objects.filter(payout_batch=locked_batch).update(payout_batch=None)

            PayoutAuditLog.objects.create(
                payout_batch=locked_batch,
                action=PayoutAuditLog.ACTION_FAILED,
                actor=actor if hasattr(actor, "id") else None,
                from_status=old_status,
                to_status=PayoutBatch.STATUS_FAILED,
                reason=clean_reason,
                metadata={"simulated": True},
            )

            return False, f"[TEST/SIMULATED] Payout batch #{locked_batch.batch_reference} marked as FAILED: {clean_reason}", locked_batch

        # Successful mock disbursement execution
        now_ts = timezone.now()
        ext_ref = f"MOCK-PAY-{uuid.uuid4().hex[:12].upper()}"

        locked_batch.status = PayoutBatch.STATUS_COMPLETED
        locked_batch.external_reference = ext_ref
        locked_batch.provider_status = "simulated_sandbox"
        locked_batch.retry_count = locked_batch.retry_count + 1
        locked_batch.processed_at = now_ts
        locked_batch.processed_by = actor if hasattr(actor, "id") else None

        if locked_batch.seller and not locked_batch.bank_account_masked:
            locked_batch.bank_account_masked = locked_batch.seller.get_masked_account_number()
            locked_batch.bank_ifsc_code = locked_batch.seller.bank_ifsc_code
            locked_batch.bank_beneficiary_name = (
                locked_batch.seller.bank_account_holder_name or locked_batch.seller.store_name
            )

        locked_batch.save(
            update_fields=[
                "status",
                "external_reference",
                "provider_status",
                "retry_count",
                "processed_at",
                "processed_by",
                "bank_account_masked",
                "bank_ifsc_code",
                "bank_beneficiary_name",
                "updated_at",
            ]
        )

        # Post an immutable PAYOUT (Debit) settlement entry in the ledger
        payout_debit_key = f"PAYOUT-DISBURSE-{locked_batch.id}"
        if not SellerLedgerEntry.objects.filter(idempotency_key=payout_debit_key).exists():
            SellerLedgerEntry.objects.create(
                seller=locked_batch.seller,
                is_platform_fulfilled=locked_batch.is_platform_fulfilled,
                entry_type=SellerLedgerEntry.TYPE_DEBIT,
                category=SellerLedgerEntry.CATEGORY_PAYOUT,
                amount=locked_batch.amount,
                currency=locked_batch.currency,
                status=SellerLedgerEntry.STATUS_POSTED,
                source_type="payout_batch",
                source_reference=locked_batch.batch_reference,
                payout_batch=locked_batch,
                idempotency_key=payout_debit_key,
                description=f"[TEST/SIMULATED] Mock disbursement settlement for batch #{locked_batch.batch_reference} ({ext_ref})",
                posted_at=now_ts,
                created_by=actor if hasattr(actor, "id") else None,
                audit_metadata={
                    "batch_reference": locked_batch.batch_reference,
                    "external_reference": ext_ref,
                    "disbursement_amount": str(locked_batch.amount),
                    "simulated": True,
                },
            )

        PayoutAuditLog.objects.create(
            payout_batch=locked_batch,
            action=PayoutAuditLog.ACTION_DISBURSED,
            actor=actor if hasattr(actor, "id") else None,
            from_status=old_status,
            to_status=PayoutBatch.STATUS_COMPLETED,
            metadata={"external_reference": ext_ref, "simulated": True},
        )

        return True, f"[TEST/SIMULATED] Mock payout batch #{locked_batch.batch_reference} completed successfully (Ref: {ext_ref}). Zero real funds transferred (Live payouts not configured).", locked_batch


def record_manual_bank_transfer(
    payout_batch: PayoutBatch,
    actor=None,
    utr_reference: str = "",
    transfer_date=None,
    confirmation: bool = False,
    notes: str = "",
) -> tuple[bool, str, PayoutBatch]:
    """
    Records an already completed external offline bank transfer (NEFT/RTGS/IMPS/UPI) for an approved PayoutBatch.

    Security & Financial Integrity Rules:
    - Never initiates an external money transfer, calls bank rails, or touches live payment gateways.
    - Operates strictly as an audit and settlement recording service for funds already transferred externally.
    - Requires authenticated staff with Super Admin or Finance Manager role.
    - Enforces row-level pessimistic locking (select_for_update) inside an atomic transaction.
    - Requires the batch to be approved (approved_at is not null) and in an eligible state.
    - Requires non-empty, unique UTR reference, valid transfer date (not in future), and explicit confirmation.
    - Enforces is_mock=False and payout_channel=PAYOUT_CHANNEL_MANUAL_BANK so offline transfers
      are never conflated with simulated mock disbursements.
    - Posts an immutable PAYOUT (Debit) settlement entry in SellerLedgerEntry exactly once (idempotent).
    - Logs an immutable PayoutAuditLog entry and accumulates audit metadata.
    """
    # 1. Staff Authorization Check (if actor provided)
    if actor is not None and hasattr(actor, "is_authenticated"):
        if not actor.is_authenticated or not actor.is_active:
            raise LedgerServiceError("Authentication required to record bank transfers.")
        if not (getattr(actor, "is_superuser", False) or getattr(actor, "is_staff", False)):
            raise LedgerServiceError("Staff privileges required to record bank transfers.")

        # Verify finance or super admin role
        try:
            from core.admin_permissions import ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, has_role
            if not (actor.is_superuser or has_role(actor, ROLE_SUPER_ADMIN) or has_role(actor, ROLE_FINANCE_MANAGER)):
                raise LedgerServiceError("Permission denied: Super Admin or Finance Manager role required.")
        except ImportError:
            if not (actor.is_superuser or actor.is_staff):
                raise LedgerServiceError("Permission denied: Unauthorized administrative role.")

    # 2. Confirmation Check
    if not confirmation:
        raise LedgerServiceError(
            "Explicit confirmation required: You must confirm that the external bank transfer has already been executed."
        )

    # 3. UTR Reference Validation
    clean_utr = (utr_reference or "").strip()
    if not clean_utr:
        raise LedgerServiceError("A valid, non-empty Bank UTR / Transaction Reference number is required.")

    # 4. Transfer Date Validation
    if not transfer_date:
        raise LedgerServiceError("Actual bank transfer execution date is required.")

    if isinstance(transfer_date, str):
        try:
            val_date = datetime.strptime(transfer_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise LedgerServiceError(f"Invalid transfer date format: '{transfer_date}'. Expected YYYY-MM-DD.")
    elif isinstance(transfer_date, datetime):
        val_date = transfer_date.date()
    elif isinstance(transfer_date, date):
        val_date = transfer_date
    else:
        raise LedgerServiceError("Invalid transfer date provided.")

    if val_date > timezone.now().date():
        raise LedgerServiceError(f"Transfer date cannot be in the future (provided: {val_date}).")

    clean_notes = (notes or "").strip()

    # 5. Atomic Transaction & Row Locking
    with transaction.atomic():
        locked_batch = (
            PayoutBatch.objects.select_for_update()
            .filter(pk=payout_batch.pk)
            .first()
        )
        if not locked_batch:
            raise LedgerServiceError("Payout batch does not exist.")

        # Maker-Checker restriction: Creator cannot record manual transfer for own batch
        if locked_batch.created_by_id and actor and hasattr(actor, "id") and locked_batch.created_by_id == actor.id:
            raise PayoutAuthorizationError(
                f"Dual authorization (Maker-Checker) restriction: Batch #{locked_batch.batch_reference} "
                f"creator ({actor.username}) cannot record manual transfer. A different authorized administrator must record the offline settlement."
            )

        # Batch Status Safeguards
        if locked_batch.status == PayoutBatch.STATUS_ON_HOLD:
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} is ON HOLD ({locked_batch.hold_reason or 'Admin hold'}). "
                "Hold must be released before recording a transfer."
            )

        if locked_batch.status == PayoutBatch.STATUS_FAILED:
            raise LedgerServiceError(
                f"Cannot record transfer for failed/rejected payout batch #{locked_batch.batch_reference}."
            )

        if locked_batch.status == PayoutBatch.STATUS_REVERSED:
            raise LedgerServiceError(
                f"Cannot record transfer for reversed payout batch #{locked_batch.batch_reference}."
            )

        # Idempotent Check: If already completed with the SAME manual UTR
        if locked_batch.status == PayoutBatch.STATUS_COMPLETED:
            if (
                locked_batch.payout_channel == PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK
                and (locked_batch.manual_utr_reference or "").lower() == clean_utr.lower()
            ):
                return True, f"Manual bank transfer already recorded for batch #{locked_batch.batch_reference} (UTR: {clean_utr}).", locked_batch
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} has already been completed under a different reference or channel."
            )

        # Approval Check: Must be approved according to actual workflow
        if not locked_batch.approved_at:
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} must be approved before recording a manual bank transfer."
            )

        if locked_batch.status not in (
            PayoutBatch.STATUS_PENDING,
            PayoutBatch.STATUS_DRAFT,
            PayoutBatch.STATUS_PROCESSING,
        ):
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} cannot record transfer from status: {locked_batch.get_status_display()}."
            )

        # Amount check
        if locked_batch.amount <= Decimal("0.00"):
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} amount must be greater than zero (current: ₹{locked_batch.amount})."
            )

        # Non-platform seller bank account eligibility check
        if locked_batch.seller and not locked_batch.is_platform_fulfilled:
            if not locked_batch.seller.is_bank_payout_eligible:
                raise PayoutEligibilityError(
                    f"Cannot record transfer: seller '{locked_batch.seller.store_name}' bank account is not verified."
                )

        # Global UTR uniqueness check across all other batches
        if PayoutBatch.objects.exclude(pk=locked_batch.pk).filter(manual_utr_reference__iexact=clean_utr).exists():
            raise LedgerServiceError(
                f"UTR reference '{clean_utr}' has already been recorded for another payout batch."
            )

        old_status = locked_batch.status
        now_ts = timezone.now()

        # Update batch fields
        locked_batch.status = PayoutBatch.STATUS_COMPLETED
        locked_batch.payout_channel = PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK
        locked_batch.payout_method = "manual_bank_transfer"
        locked_batch.is_mock = False
        locked_batch.manual_utr_reference = clean_utr
        locked_batch.manual_transfer_date = val_date
        locked_batch.external_reference = clean_utr
        locked_batch.provider_status = "manual_bank_transfer_recorded"
        locked_batch.processed_at = now_ts
        locked_batch.processed_by = actor if hasattr(actor, "id") else None

        if locked_batch.seller and not locked_batch.bank_account_masked:
            locked_batch.bank_account_masked = locked_batch.seller.get_masked_account_number()
            locked_batch.bank_ifsc_code = locked_batch.seller.bank_ifsc_code
            locked_batch.bank_beneficiary_name = (
                locked_batch.seller.bank_account_holder_name or locked_batch.seller.store_name
            )

        if clean_notes:
            locked_batch.notes = (
                f"{locked_batch.notes}\n[Manual Transfer: {clean_notes}]" if locked_batch.notes else clean_notes
            ).strip()

        # Audit metadata
        meta = locked_batch.audit_metadata if isinstance(locked_batch.audit_metadata, dict) else {}
        transfer_audit = {
            "recorded_by_username": getattr(actor, "username", "admin"),
            "recorded_at": now_ts.isoformat(),
            "manual_transfer_date": str(val_date),
            "manual_utr_reference": clean_utr,
            "disbursement_amount": str(locked_batch.amount),
            "channel": PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
            "notes": clean_notes,
            "offline_manual_transfer": True,
        }
        history = meta.get("manual_transfer_history")
        if not isinstance(history, list):
            history = []
        history.append(transfer_audit)
        meta["manual_transfer_history"] = history
        meta["manual_transfer"] = transfer_audit
        locked_batch.audit_metadata = meta

        locked_batch.save(
            update_fields=[
                "status",
                "payout_channel",
                "payout_method",
                "is_mock",
                "manual_utr_reference",
                "manual_transfer_date",
                "external_reference",
                "provider_status",
                "processed_at",
                "processed_by",
                "bank_account_masked",
                "bank_ifsc_code",
                "bank_beneficiary_name",
                "notes",
                "audit_metadata",
                "updated_at",
            ]
        )

        # Post immutable PAYOUT (Debit) settlement ledger entry exactly once
        payout_debit_key = f"PAYOUT-DISBURSE-{locked_batch.id}"
        if not SellerLedgerEntry.objects.filter(idempotency_key=payout_debit_key).exists():
            SellerLedgerEntry.objects.create(
                seller=locked_batch.seller,
                is_platform_fulfilled=locked_batch.is_platform_fulfilled,
                entry_type=SellerLedgerEntry.TYPE_DEBIT,
                category=SellerLedgerEntry.CATEGORY_PAYOUT,
                amount=locked_batch.amount,
                currency=locked_batch.currency,
                status=SellerLedgerEntry.STATUS_POSTED,
                source_type="payout_batch",
                source_reference=locked_batch.batch_reference,
                payout_batch=locked_batch,
                idempotency_key=payout_debit_key,
                description=f"Manual bank transfer disbursement settlement for batch #{locked_batch.batch_reference} (UTR: {clean_utr})",
                posted_at=now_ts,
                created_by=actor if hasattr(actor, "id") else None,
                audit_metadata={
                    "batch_reference": locked_batch.batch_reference,
                    "channel": PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
                    "manual_utr_reference": clean_utr,
                    "manual_transfer_date": str(val_date),
                    "disbursement_amount": str(locked_batch.amount),
                    "offline_manual_transfer": True,
                },
            )

        # Log immutable PayoutAuditLog
        PayoutAuditLog.objects.create(
            payout_batch=locked_batch,
            action=PayoutAuditLog.ACTION_DISBURSED,
            actor=actor if hasattr(actor, "id") else None,
            from_status=old_status,
            to_status=PayoutBatch.STATUS_COMPLETED,
            reason=f"Manual external bank transfer recorded (UTR: {clean_utr}, Value Date: {val_date}).",
            metadata={
                "channel": PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
                "manual_utr_reference": clean_utr,
                "manual_transfer_date": str(val_date),
                "amount": str(locked_batch.amount),
                "recorded_by": getattr(actor, "username", "admin"),
                "timestamp": now_ts.isoformat(),
                "offline_manual_transfer": True,
            },
        )

        return True, f"Manual bank transfer recorded successfully for batch #{locked_batch.batch_reference} (UTR: {clean_utr}). Payout marked COMPLETED.", locked_batch


def reverse_payout_batch(
    payout_batch: PayoutBatch,
    actor=None,
    reason: str = "",
) -> tuple[bool, str, PayoutBatch]:
    """
    Auditable reversal of a COMPLETED payout batch:
    - Never deletes records.
    - Transitions PayoutBatch status to REVERSED.
    - Posts an auditable PAYOUT_REVERSAL (Credit) entry into the ledger to restore merchant balance.
    """
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise LedgerServiceError("Mandatory reason is required to reverse a payout batch.")

    if actor is not None and hasattr(actor, "is_authenticated"):
        if not actor.is_authenticated or not actor.is_active:
            raise PayoutAuthorizationError("Authentication required to reverse payout batches.")
        if not (getattr(actor, "is_superuser", False) or getattr(actor, "is_staff", False)):
            raise PayoutAuthorizationError("Staff privileges required to reverse payout batches.")
        from core.admin_permissions import ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, get_user_role
        user_role = get_user_role(actor)
        if not (actor.is_superuser or user_role in (ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER)):
            raise PayoutAuthorizationError("Permission denied: Super Admin or Finance Manager role required to reverse payout batches.")

    with transaction.atomic():
        locked_batch = (
            PayoutBatch.objects.select_for_update()
            .filter(pk=payout_batch.pk)
            .first()
        )
        if not locked_batch:
            raise LedgerServiceError("Payout batch does not exist.")

        if locked_batch.status != PayoutBatch.STATUS_COMPLETED:
            raise LedgerServiceError(
                f"Only COMPLETED payout batches can be reversed (current status: {locked_batch.get_status_display()})."
            )

        now_ts = timezone.now()
        locked_batch.status = PayoutBatch.STATUS_REVERSED
        locked_batch.notes = (
            f"{locked_batch.notes}\n[REVERSED at {now_ts.strftime('%Y-%m-%d %H:%M')} by {getattr(actor, 'username', 'admin')}: {clean_reason}]"
        ).strip()
        locked_batch.save(update_fields=["status", "notes", "updated_at"])

        # Post PAYOUT_REVERSAL (Credit) entry to restore merchant balance
        reversal_key = f"PAYOUT-REV-{locked_batch.id}"
        if not SellerLedgerEntry.objects.filter(idempotency_key=reversal_key).exists():
            SellerLedgerEntry.objects.create(
                seller=locked_batch.seller,
                is_platform_fulfilled=locked_batch.is_platform_fulfilled,
                entry_type=SellerLedgerEntry.TYPE_CREDIT,
                category=SellerLedgerEntry.CATEGORY_PAYOUT_REVERSAL,
                amount=locked_batch.amount,
                currency=locked_batch.currency,
                status=SellerLedgerEntry.STATUS_POSTED,
                source_type="payout_batch",
                source_reference=locked_batch.batch_reference,
                payout_batch=locked_batch,
                idempotency_key=reversal_key,
                description=f"Reversal credit for payout batch #{locked_batch.batch_reference}: {clean_reason}",
                posted_at=now_ts,
                created_by=actor if hasattr(actor, "id") else None,
                audit_metadata={
                    "reversed_batch": locked_batch.batch_reference,
                    "reason": clean_reason,
                },
            )

        PayoutAuditLog.objects.create(
            payout_batch=locked_batch,
            action=PayoutAuditLog.ACTION_REVERSED,
            actor=actor if hasattr(actor, "id") else None,
            from_status=PayoutBatch.STATUS_COMPLETED,
            to_status=PayoutBatch.STATUS_REVERSED,
            reason=clean_reason,
            metadata={"reversed_by": getattr(actor, "username", "admin"), "timestamp": now_ts.isoformat()},
        )

        return True, f"Payout batch #{locked_batch.batch_reference} was reversed and credited back to seller ledger.", locked_batch


# ==============================================================================
# 8. SELLER FINANCIAL DASHBOARD METRICS (TENANT ISOLATED)
# ==============================================================================

def get_seller_ledger_summary(seller: Seller | None = None, is_platform_fulfilled: bool = False) -> dict:
    """
    Computes real-time financial ledger metrics for a single tenant merchant:
    - Total Gross Merchandise Value (GMV)
    - Total Platform Commission Deductions
    - Total Refund Deductions
    - Total Payouts Disbursed
    - Current Outstanding Payable Balance
    - Pending COD Amount (locked until courier remittance)
    """
    qs = SellerLedgerEntry.objects.all()
    if is_platform_fulfilled or seller is None:
        qs = qs.filter(is_platform_fulfilled=True)
    else:
        qs = qs.filter(seller=seller, is_platform_fulfilled=False)

    posted_qs = qs.filter(status=SellerLedgerEntry.STATUS_POSTED)
    pending_cod_qs = qs.filter(status=SellerLedgerEntry.STATUS_PENDING_COD)

    # All posted credits and debits
    total_posted_credits = (
        posted_qs.filter(entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    total_posted_debits = (
        posted_qs.filter(entry_type=SellerLedgerEntry.TYPE_DEBIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    current_balance = (total_posted_credits - total_posted_debits).quantize(Decimal("0.01"))

    # Category breakdowns
    gmv = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_ORDER_SALE).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    commission = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_COMMISSION).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    refunds = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_RETURN_REFUND).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    commission_reversals = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_COMMISSION_REVERSAL).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    payouts = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_PAYOUT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )

    pending_cod_credits = (
        pending_cod_qs.filter(entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    pending_cod_debits = (
        pending_cod_qs.filter(entry_type=SellerLedgerEntry.TYPE_DEBIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    pending_cod_net = (pending_cod_credits - pending_cod_debits).quantize(Decimal("0.01"))

    return {
        "seller": seller,
        "is_platform_fulfilled": is_platform_fulfilled,
        "current_balance": current_balance,
        "gmv": gmv,
        "commission": commission,
        "refunds": refunds,
        "commission_reversals": commission_reversals,
        "payouts_disbursed": payouts,
        "pending_cod_amount": pending_cod_net,
    }


# ==============================================================================
# 11. SELLER CLOSURE CLEARANCE & FINAL SETTLEMENT (PHASE 2)
# ==============================================================================

def audit_seller_closure_clearance(
    seller: Seller,
    as_of_date=None,
) -> dict:
    """
    Read-only audit evaluating whether a seller is cleared for final settlement and account closure (Phase 2).

    Non-mutating: Strictly inspects database state; never modifies models, releases holds,
    creates batches, posts ledger entries, or invokes payment rails.

    Returns a comprehensive structured clearance report:
    - seller metadata and current closure status
    - blockers preventing final settlement or full closure
    - open/unfulfilled sub-orders (anything not delivered or cancelled)
    - active/unresolved return/exchange requests
    - pending/failed refund records
    - unreconciled courier COD remittances
    - active 120-hour delivery cooling holds with time remaining
    - complete ledger balance breakdown (credits, debits, net balance, unbatched funds)
    - active and completed final settlement batch details
    - can_create_final_settlement_batch: boolean indicating final batch readiness
    - can_fully_close: boolean indicating whether seller can be safely marked closed
    """
    if as_of_date is None:
        as_of_date = timezone.now()

    blockers = []
    closure_blockers = []

    # 1. Status Check
    if seller.status != Seller.STATUS_CLOSING:
        status_msg = f"Seller is in '{seller.get_status_display()}' status. Must be in 'Closing / Settlement Run-off' to proceed."
        blockers.append(status_msg)
        closure_blockers.append(status_msg)

    # 2. Open / Unfulfilled Orders Check
    open_orders_qs = SellerOrder.objects.filter(seller=seller).exclude(
        status__in=[SellerOrder.STATUS_DELIVERED, SellerOrder.STATUS_CANCELLED]
    ).select_related("order")
    open_orders_count = open_orders_qs.count()
    open_orders_details = [
        {
            "sub_order_number": o.sub_order_number,
            "status": o.status,
            "status_display": o.get_status_display(),
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o in open_orders_qs[:20]
    ]
    if open_orders_count > 0:
        msg = f"{open_orders_count} open/unfulfilled sub-order(s) must be delivered or cancelled."
        blockers.append(msg)
        closure_blockers.append(msg)

    # 3. Unresolved Return / Exchange Requests Check
    unresolved_returns_qs = ReturnRequest.objects.filter(
        seller_order__seller=seller
    ).exclude(
        status__in=[
            ReturnRequest.STATUS_COMPLETED,
            ReturnRequest.STATUS_REJECTED,
            ReturnRequest.STATUS_CANCELLED,
        ]
    ).select_related("seller_order")
    unresolved_returns_count = unresolved_returns_qs.count()
    unresolved_returns_details = [
        {
            "request_number": r.request_number,
            "status": r.status,
            "status_display": r.get_status_display(),
            "sub_order_number": r.seller_order.sub_order_number if r.seller_order else "N/A",
        }
        for r in unresolved_returns_qs[:20]
    ]
    if unresolved_returns_count > 0:
        msg = f"{unresolved_returns_count} active return/exchange request(s) remain unresolved."
        blockers.append(msg)
        closure_blockers.append(msg)

    # 4. Pending / Failed Refund Records Check (Sub-order linked and Order-level)
    unresolved_refunds_qs = (
        RefundRecord.objects.filter(
            Q(seller_order__seller=seller)
            | Q(order__seller_orders__seller=seller, seller_order__isnull=True),
            status__in=[
                RefundRecord.STATUS_PENDING,
                RefundRecord.STATUS_PROCESSING,
                RefundRecord.STATUS_FAILED,
            ],
        )
        .select_related("seller_order", "order")
        .distinct()
    )
    unresolved_refunds_count = unresolved_refunds_qs.count()
    unresolved_refunds_details = [
        {
            "refund_reference": ref.refund_reference,
            "status": ref.status,
            "status_display": ref.get_status_display(),
            "amount": str(ref.requested_amount),
            "order_number": ref.order.order_number if ref.order else "N/A",
            "sub_order_number": ref.seller_order.sub_order_number if ref.seller_order else "Order-Level",
        }
        for ref in unresolved_refunds_qs[:20]
    ]
    if unresolved_refunds_count > 0:
        msg = f"{unresolved_refunds_count} refund record(s) pending processing or in failed state."
        blockers.append(msg)
        closure_blockers.append(msg)

    # 5. Unreconciled COD Check
    unreconciled_cod_orders = SellerOrder.objects.filter(
        seller=seller,
        order__payment_method=Order.PAYMENT_METHOD_COD,
        cod_reconciliation__isnull=True,
    ).exclude(status=SellerOrder.STATUS_CANCELLED)
    pending_cod_entries = SellerLedgerEntry.objects.filter(
        seller=seller,
        status=SellerLedgerEntry.STATUS_PENDING_COD,
    )
    unreconciled_cod_count = unreconciled_cod_orders.count() + pending_cod_entries.count()
    if unreconciled_cod_count > 0:
        msg = f"{unreconciled_cod_count} courier COD remittance record(s) pending reconciliation."
        blockers.append(msg)
        closure_blockers.append(msg)

    # 6. Eligibility & Delivery Cooling Hold Check
    eligibility = calculate_seller_payout_eligibility(
        seller=seller,
        as_of_date=as_of_date,
        cycle_month=None,
        payout_cycle=None,
    )
    cooling_holds = [
        item for item in eligibility.get("held_entry_details", [])
        if item.get("is_cooling_hold")
    ]
    if cooling_holds:
        msg = f"{len(cooling_holds)} delivery cooling period hold(s) active (120-hour cooling period pending)."
        blockers.append(msg)
        closure_blockers.append(msg)

    # 7. Complete Ledger Balance Evaluation
    posted_entries = SellerLedgerEntry.objects.filter(
        seller=seller,
        status=SellerLedgerEntry.STATUS_POSTED,
    )
    total_posted_credits = (
        posted_entries.filter(entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    total_posted_debits = (
        posted_entries.filter(entry_type=SellerLedgerEntry.TYPE_DEBIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    net_lifetime_balance = (total_posted_credits - total_posted_debits).quantize(Decimal("0.01"))
    is_negative_balance = net_lifetime_balance < Decimal("0.00")

    if is_negative_balance:
        msg = f"Negative ledger balance (-₹{abs(net_lifetime_balance)}). Outstanding merchant liability must be recovered."
        blockers.append(msg)
        closure_blockers.append(msg)

    unbatched_eligible_amount = eligibility.get("net_eligible_balance", Decimal("0.00"))

    # 8. Final Settlement Batch Inspection
    final_batches = PayoutBatch.objects.filter(
        seller=seller,
        batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
    ).order_by("-created_at")
    active_final_batch = final_batches.exclude(
        status__in=[PayoutBatch.STATUS_FAILED, PayoutBatch.STATUS_REVERSED]
    ).first()
    completed_final_batch = final_batches.filter(status=PayoutBatch.STATUS_COMPLETED).first()

    existing_final_info = None
    if active_final_batch:
        has_utr = bool(active_final_batch.manual_utr_reference and active_final_batch.manual_utr_reference.strip())
        has_date = bool(active_final_batch.manual_transfer_date)
        is_verified_transfer = has_utr and has_date

        existing_final_info = {
            "batch_reference": active_final_batch.batch_reference,
            "status": active_final_batch.status,
            "status_display": active_final_batch.get_status_display(),
            "amount": str(active_final_batch.amount),
            "is_completed": active_final_batch.status == PayoutBatch.STATUS_COMPLETED,
            "is_on_hold": active_final_batch.status == PayoutBatch.STATUS_ON_HOLD,
            "manual_utr_reference": active_final_batch.manual_utr_reference or "",
            "manual_transfer_date": active_final_batch.manual_transfer_date.isoformat() if active_final_batch.manual_transfer_date else None,
            "is_verified_transfer": is_verified_transfer,
        }
        if active_final_batch.status != PayoutBatch.STATUS_COMPLETED:
            closure_blockers.append(
                f"Active final settlement batch #{active_final_batch.batch_reference} is in '{active_final_batch.get_status_display()}' status and must be completed."
            )
        elif not is_verified_transfer:
            missing = []
            if not has_utr:
                missing.append("verified bank UTR reference")
            if not has_date:
                missing.append("transfer execution date")
            closure_blockers.append(
                f"Completed final settlement batch #{active_final_batch.batch_reference} is missing required transfer details: {', '.join(missing)}."
            )

    # 9. Can create final settlement batch?
    # Must be in closing, zero settlement blockers, unbatched net eligible > 0, and no active final batch already running
    can_create_final_settlement_batch = (
        seller.status == Seller.STATUS_CLOSING
        and len(blockers) == 0
        and unbatched_eligible_amount > Decimal("0.00")
        and (active_final_batch is None or active_final_batch.status in [PayoutBatch.STATUS_FAILED, PayoutBatch.STATUS_REVERSED])
    )

    # 10. Can fully close?
    # Must be in closing, zero closure blockers, no active uncompleted batch, and unbatched net residual balance is 0.00
    unbatched_entries_remain = SellerLedgerEntry.objects.filter(
        seller=seller,
        payout_batch__isnull=True,
        status=SellerLedgerEntry.STATUS_POSTED,
    ).exists()

    unbatched_net = Decimal("0.00")
    if unbatched_entries_remain:
        unb_credits = SellerLedgerEntry.objects.filter(
            seller=seller, payout_batch__isnull=True, status=SellerLedgerEntry.STATUS_POSTED, entry_type=SellerLedgerEntry.TYPE_CREDIT
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        unb_debits = SellerLedgerEntry.objects.filter(
            seller=seller, payout_batch__isnull=True, status=SellerLedgerEntry.STATUS_POSTED, entry_type=SellerLedgerEntry.TYPE_DEBIT
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        unbatched_net = (unb_credits - unb_debits).quantize(Decimal("0.01"))

    if unbatched_net > Decimal("0.00"):
        closure_blockers.append(
            f"Residual unbatched balance of ₹{unbatched_net} remains payable. Final settlement batch must be created and disbursed."
        )
    elif unbatched_net < Decimal("0.00"):
        closure_blockers.append(
            f"Negative unbatched balance (-₹{abs(unbatched_net)}). Outstanding merchant liability must be recovered."
        )

    # Reconcile lifetime balance versus unbatched residual:
    # If unbatched_net is 0 but net_lifetime_balance > 0 and final batch is completed (or absent),
    # undisbursed net funds remain on the ledger.
    if net_lifetime_balance > Decimal("0.00") and (active_final_batch is None or active_final_batch.status == PayoutBatch.STATUS_COMPLETED):
        closure_blockers.append(
            f"Undisbursed net lifetime balance of ₹{net_lifetime_balance} remains. Total merchant ledger must balance to ₹0.00 before closure."
        )

    # Active non-final payout batches check: any cycle batch in draft, pending, on hold, or processing
    active_other_batches = PayoutBatch.objects.filter(
        seller=seller
    ).exclude(
        batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT
    ).exclude(
        status__in=[PayoutBatch.STATUS_COMPLETED, PayoutBatch.STATUS_FAILED, PayoutBatch.STATUS_REVERSED]
    )
    for ob in active_other_batches:
        closure_blockers.append(
            f"Active payout batch #{ob.batch_reference} is in '{ob.get_status_display()}' status and must be completed or resolved before closure."
        )

    final_batch_verified = (
        active_final_batch is None
        or (
            active_final_batch.status == PayoutBatch.STATUS_COMPLETED
            and bool(active_final_batch.manual_utr_reference and active_final_batch.manual_utr_reference.strip())
            and bool(active_final_batch.manual_transfer_date)
        )
    )

    can_fully_close = (
        seller.status == Seller.STATUS_CLOSING
        and len(closure_blockers) == 0
        and unbatched_net == Decimal("0.00")
        and net_lifetime_balance == Decimal("0.00")
        and final_batch_verified
        and not active_other_batches.exists()
    )

    return {
        "seller_id": seller.id,
        "seller_store_name": seller.store_name,
        "status": seller.status,
        "status_display": seller.get_status_display(),
        "is_closing": seller.is_closing,
        "is_closed": seller.is_closed,
        "is_cleared_for_settlement": len(blockers) == 0,
        "settlement_blockers": blockers,
        "closure_blockers": closure_blockers,
        "open_orders": {
            "count": open_orders_count,
            "details": open_orders_details,
        },
        "unresolved_returns": {
            "count": unresolved_returns_count,
            "details": unresolved_returns_details,
        },
        "unresolved_refunds": {
            "count": unresolved_refunds_count,
            "details": unresolved_refunds_details,
        },
        "unreconciled_cod": {
            "count": unreconciled_cod_count,
            "unreconciled_orders_count": unreconciled_cod_orders.count(),
            "pending_cod_entries_count": pending_cod_entries.count(),
        },
        "cooling_holds": {
            "count": len(cooling_holds),
            "details": cooling_holds,
        },
        "ledger_balance": {
            "total_posted_credits": total_posted_credits,
            "total_posted_debits": total_posted_debits,
            "net_lifetime_balance": net_lifetime_balance,
            "is_negative_balance": is_negative_balance,
            "unbatched_eligible_amount": unbatched_eligible_amount,
            "unbatched_held_amount": eligibility.get("held_net_amount", Decimal("0.00")),
            "unbatched_residual_net": unbatched_net,
        },
        "existing_final_batch": existing_final_info,
        "can_create_final_settlement_batch": can_create_final_settlement_batch,
        "can_fully_close": can_fully_close,
    }


def create_final_settlement_batch(
    seller: Seller,
    actor=None,
    notes: str = "",
) -> PayoutBatch:
    """
    Creates an immutable PayoutBatch for seller account closure final settlement (Phase 2).

    Financial & Security Invariants:
    1. Seller Status: Seller MUST be in STATUS_CLOSING ('closing'). Rejects approved, requested,
       suspended, or closed sellers.
    2. Role Authorization: Requires Super Admin or Finance Manager privileges if actor is supplied.
    3. Re-checks Clearance Atomically: Does not rely on stale cached evaluations; re-verifies
       zero open orders, zero open returns, zero open refunds, zero unreconciled COD, and zero
       active cooling holds inside a pessimistic row-locking database transaction.
    4. Threshold Bypass: Does NOT enforce the regular ₹500 minimum payout threshold. Any positive
       residual balance (e.g. ₹120.00) is eligible for final settlement.
    5. Zero/Negative Balance Rejection: Rejects zero or negative balances with PayoutEligibilityError.
    6. Double-Batching Protection: Respects the database unique active final settlement batch constraint.
       If an active final batch already exists, returns it safely without duplicate batch creation.
    7. Safeguards & Provider Isolation: Does NOT disburse funds, does NOT call external banking APIs,
       does NOT mark seller closed, and bundles entries with batch_type='final_settlement'.
    """
    if actor is not None and hasattr(actor, "is_authenticated"):
        if not actor.is_authenticated or not actor.is_active:
            raise LedgerServiceError("Authentication required to create final settlement batches.")
        if not (getattr(actor, "is_superuser", False) or getattr(actor, "is_staff", False)):
            raise LedgerServiceError("Staff privileges required to create final settlement batches.")
        from core.admin_permissions import ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, has_admin_permission
        if not (actor.is_superuser or has_admin_permission(actor, [ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])):
            raise LedgerServiceError("Permission denied: Super Admin or Finance Manager role required.")

    if seller.status != Seller.STATUS_CLOSING:
        raise PayoutEligibilityError(
            f"Seller '{seller.store_name}' must be in CLOSING status to create a final settlement batch (current: {seller.get_status_display()})."
        )

    with transaction.atomic():
        locked_seller = Seller.objects.select_for_update().get(pk=seller.pk)
        if locked_seller.status != Seller.STATUS_CLOSING:
            raise PayoutEligibilityError(
                f"Seller '{locked_seller.store_name}' must be in CLOSING status to create a final settlement batch (current: {locked_seller.get_status_display()})."
            )

        # 1. Idempotency Check: Existing active final settlement batch
        existing_final = (
            PayoutBatch.objects.select_for_update()
            .filter(
                seller=locked_seller,
                batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
            )
            .exclude(
                status__in=[
                    PayoutBatch.STATUS_COMPLETED,
                    PayoutBatch.STATUS_FAILED,
                    PayoutBatch.STATUS_REVERSED,
                ]
            )
            .first()
        )
        if existing_final:
            return existing_final

        # 2. Re-verify clearance blockers atomically
        open_orders = SellerOrder.objects.filter(seller=locked_seller).exclude(
            status__in=[SellerOrder.STATUS_DELIVERED, SellerOrder.STATUS_CANCELLED]
        )
        if open_orders.exists():
            raise PayoutEligibilityError(
                f"Cannot create final settlement batch: {open_orders.count()} open/unfulfilled sub-order(s) remain."
            )

        open_returns = ReturnRequest.objects.filter(seller_order__seller=locked_seller).exclude(
            status__in=[
                ReturnRequest.STATUS_COMPLETED,
                ReturnRequest.STATUS_REJECTED,
                ReturnRequest.STATUS_CANCELLED,
            ]
        )
        if open_returns.exists():
            raise PayoutEligibilityError(
                f"Cannot create final settlement batch: {open_returns.count()} active return/exchange request(s) remain unresolved."
            )

        open_refunds = RefundRecord.objects.filter(
            Q(seller_order__seller=locked_seller)
            | Q(order__seller_orders__seller=locked_seller, seller_order__isnull=True),
            status__in=[
                RefundRecord.STATUS_PENDING,
                RefundRecord.STATUS_PROCESSING,
                RefundRecord.STATUS_FAILED,
            ],
        ).distinct()
        if open_refunds.exists():
            raise PayoutEligibilityError(
                f"Cannot create final settlement batch: {open_refunds.count()} pending or failed refund record(s) remain."
            )

        unreconciled_cod_orders = SellerOrder.objects.filter(
            seller=locked_seller,
            order__payment_method=Order.PAYMENT_METHOD_COD,
            cod_reconciliation__isnull=True,
        ).exclude(status=SellerOrder.STATUS_CANCELLED)
        pending_cod_entries = SellerLedgerEntry.objects.filter(
            seller=locked_seller,
            status=SellerLedgerEntry.STATUS_PENDING_COD,
        )
        if unreconciled_cod_orders.exists() or pending_cod_entries.exists():
            raise PayoutEligibilityError(
                "Cannot create final settlement batch: Courier COD cash remittances pending reconciliation."
            )

        # 3. Calculate unbatched eligibility
        eligibility = calculate_seller_payout_eligibility(
            seller=locked_seller,
            cycle_month=None,
            payout_cycle=None,
        )
        cooling_holds = [
            item for item in eligibility.get("held_entry_details", [])
            if item.get("is_cooling_hold")
        ]
        if cooling_holds:
            raise PayoutEligibilityError(
                f"Cannot create final settlement batch: 120-hour delivery cooling period active for {len(cooling_holds)} entries."
            )

        net_amount = eligibility["net_eligible_balance"]
        if net_amount <= Decimal("0.00"):
            raise PayoutEligibilityError(
                f"Cannot create final settlement batch: Net payable balance must be greater than zero (current: ₹{net_amount})."
            )

        # 4. Lock eligible entries pessimistically
        eligible_ids = eligibility["eligible_entry_ids"]
        if not eligible_ids:
            raise PayoutEligibilityError("No eligible ledger entries available for final settlement.")

        locked_entries = list(
            SellerLedgerEntry.objects.select_for_update().filter(id__in=eligible_ids)
        )
        already_batched = [e for e in locked_entries if e.payout_batch_id is not None]
        if already_batched:
            raise PayoutEligibilityError(
                f"{len(already_batched)} ledger entries have already been included in another payout batch."
            )

        # 5. Create PayoutBatch record in DRAFT status
        date_str = timezone.now().strftime("%Y%m%d")
        rand_hex = uuid.uuid4().hex[:6].upper()
        batch_ref = f"PAY-FINAL-{date_str}-{rand_hex}"
        while PayoutBatch.objects.filter(batch_reference=batch_ref).exists():
            rand_hex = uuid.uuid4().hex[:6].upper()
            batch_ref = f"PAY-FINAL-{date_str}-{rand_hex}"

        clean_notes = (notes or "").strip()
        batch = PayoutBatch.objects.create(
            batch_reference=batch_ref,
            seller=locked_seller,
            is_platform_fulfilled=False,
            amount=net_amount,
            currency="INR",
            status=PayoutBatch.STATUS_DRAFT,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
            payout_cycle=f"FINAL-{locked_seller.id}",
            payout_method="manual_bank_transfer",
            payout_channel=PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
            bank_account_masked=locked_seller.get_masked_account_number(),
            bank_ifsc_code=locked_seller.bank_ifsc_code or "N/A",
            bank_beneficiary_name=locked_seller.bank_account_holder_name or locked_seller.store_name,
            eligible_entries_count=len(locked_entries),
            period_start=min(e.created_at for e in locked_entries),
            period_end=max(e.created_at for e in locked_entries),
            notes=clean_notes if clean_notes else "Seller closure final settlement batch.",
            audit_metadata={
                "batch_type": PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
                "created_by": getattr(actor, "username", "system"),
                "seller_id": locked_seller.id,
                "store_name": locked_seller.store_name,
                "net_payable": str(net_amount),
                "eligible_entries_count": len(locked_entries),
            },
        )

        # Atomically link entries to the batch
        for entry in locked_entries:
            entry.payout_batch = batch
            entry.save(update_fields=["payout_batch"])

        # Create immutable PayoutAuditLog
        PayoutAuditLog.objects.create(
            payout_batch=batch,
            action=PayoutAuditLog.ACTION_CREATED,
            actor=actor if hasattr(actor, "id") else None,
            from_status="",
            to_status=PayoutBatch.STATUS_DRAFT,
            reason=f"Final settlement batch created for closing seller '{locked_seller.store_name}'. Amount: ₹{net_amount}",
            metadata={
                "eligible_entries_count": len(locked_entries),
                "net_amount": str(net_amount),
                "notes": notes,
                "actor_username": getattr(actor, "username", "system"),
            },
        )

        return batch


def is_seller_eligible_for_final_closure(
    seller: Seller,
    as_of_date=None,
) -> tuple[bool, list[str]]:
    """
    Evaluates whether a seller in CLOSING status is cleared to be finalized as CLOSED (Phase 2).

    Returns:
        tuple[bool, list[str]]: (can_fully_close, list_of_blockers)
    """
    clearance = audit_seller_closure_clearance(seller, as_of_date=as_of_date)
    return clearance["can_fully_close"], clearance["closure_blockers"]


def finalize_seller_account_closure(
    seller: Seller,
    actor=None,
    notes: str = "",
    confirm: bool = False,
) -> Seller:
    """
    Executes formal, permanent seller account closure (Phase 5).

    Financial, Operational & Security Invariants:
    1. Authentication & Authorization:
       - Requires authenticated staff user with ROLE_SUPER_ADMIN or ROLE_FINANCE_MANAGER role (or superuser).
    2. Prohibit Self-Finalization:
       - A seller user is strictly prohibited from finalizing their own account closure,
         preventing privilege-escalation / self-offboarding bypass.
    3. Mandatory Confirmation & Audit Notes:
       - Requires explicit confirmation flag (confirm=True).
       - Requires non-empty internal audit notes (minimum 5 characters).
    4. Pessimistic Row Locking & Atomic Transaction:
       - Executes inside transaction.atomic() with select_for_update() row lock on Seller.
    5. Prior Status Validation:
       - Seller MUST be currently in STATUS_CLOSING ('closing').
       - Rejects requests from STATUS_APPROVED, STATUS_CLOSURE_REQUESTED, STATUS_SUSPENDED, or STATUS_PENDING.
       - Rejects already finalized STATUS_CLOSED requests safely (idempotency / duplicate rejection).
    6. Re-evaluates All Clearance Checks Atomically:
       - Calls audit_seller_closure_clearance(locked_seller) inside the transaction.
       - Rejects closure if any open order, unresolved return/refund, unreconciled COD,
         active 120-hour cooling hold, negative balance, or unbatched payable residual remains.
       - If any payable residual was due, requires a final-settlement batch to exist and be
         recorded as STATUS_COMPLETED via the manual-transfer-recording workflow.
       - If no amount was due, verifies zero outstanding merchant liability and zero unpaid residual.
       - Reconciles lifetime ledger balance against unbatched residual (both must be ₹0.00).
       - Never waives the 120-hour cooling hold.
       - Never calls an external payment provider or auto-disburses funds.
    7. Atomic State Mutation:
       - Sets status = Seller.STATUS_CLOSED
       - Sets is_active = False
       - Sets closed_at = timezone.now()
       - Sets closed_by = actor (if authenticated)
       - Appends durable timestamped audit note to closure_notes.
    8. Product Delisting:
       - Updates all active seller products to is_active=False, preserving historical catalog
         and order references.
    9. Durable Administrative Audit:
       - Calls log_admin_action() recording actor, seller, timestamp, reason, and final batch reference.
    10. Safe Transactional Seller Notification:
       - Calls send_seller_closure_finalized_email() safely wrapped so email issues never roll back
         a valid financial closure.
    11. Read-Only Retention:
       - The seller retains read-only portal access governed by Phase 4 permissions.
    """
    # 1. Prevent Self-Finalization (pre-lock check)
    if actor is not None and hasattr(actor, "id") and getattr(seller, "user_id", None) == actor.id:
        raise LedgerServiceError("Sellers are strictly prohibited from finalizing their own account closure.")

    # 2. Staff Authentication and Role Authorization
    if actor is not None and hasattr(actor, "is_authenticated"):
        if not actor.is_authenticated or not actor.is_active:
            raise LedgerServiceError("Authentication required to finalize seller account closure.")
        if not (getattr(actor, "is_superuser", False) or getattr(actor, "is_staff", False)):
            raise LedgerServiceError("Staff privileges required to finalize seller account closure.")

        try:
            from core.admin_permissions import ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, has_admin_permission
            if not (actor.is_superuser or has_admin_permission(actor, [ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])):
                raise LedgerServiceError("Permission denied: Super Admin or Finance Manager role required.")
        except ImportError:
            if not (actor.is_superuser or actor.is_staff):
                raise LedgerServiceError("Permission denied: Unauthorized administrative role.")

    # 3. Explicit Confirmation
    if not confirm:
        raise LedgerServiceError(
            "Explicit confirmation required: You must confirm that all clearance checks have been verified before closing the account."
        )

    # 4. Mandatory Internal Reason / Notes
    clean_notes = (notes or "").strip()
    if not clean_notes or len(clean_notes) < 5:
        raise LedgerServiceError(
            "Mandatory internal audit notes (minimum 5 characters) are required to finalize seller account closure."
        )

    # 5. Atomic Transaction & Pessimistic Row Locking
    with transaction.atomic():
        locked_seller = (
            Seller.objects.select_for_update()
            .filter(pk=seller.pk)
            .first()
        )
        if not locked_seller:
            raise LedgerServiceError("Seller profile does not exist.")

        # Self-finalization check on locked seller instance
        if actor is not None and hasattr(actor, "id") and locked_seller.user_id == actor.id:
            raise LedgerServiceError("Sellers are strictly prohibited from finalizing their own account closure.")

        # Duplicate / Idempotency check: Already closed?
        if locked_seller.status == Seller.STATUS_CLOSED:
            raise LedgerServiceError(
                f"Seller '{locked_seller.store_name}' is already closed (finalized on {locked_seller.closed_at}). "
                "Duplicate closure finalization rejected."
            )

        # Status validation: Must be currently in STATUS_CLOSING
        if locked_seller.status != Seller.STATUS_CLOSING:
            raise PayoutEligibilityError(
                f"Cannot finalize closure: Seller '{locked_seller.store_name}' must be in 'Closing / Settlement Run-off' "
                f"status to be finalized (currently '{locked_seller.get_status_display()}'). "
                f"Direct finalization from status '{locked_seller.status}' is strictly prohibited."
            )

        # 6. Re-evaluate full clearance inside the transaction
        clearance = audit_seller_closure_clearance(locked_seller)

        if not clearance["can_fully_close"] or clearance["closure_blockers"]:
            blocker_summary = "; ".join(clearance["closure_blockers"]) or "Clearance requirements not met."
            raise PayoutEligibilityError(
                f"Cannot finalize closure for '{locked_seller.store_name}': {blocker_summary}"
            )

        # 7. Additional Financial & Accounting Invariants
        ledger_bal = clearance["ledger_balance"]
        net_lifetime_bal = ledger_bal["net_lifetime_balance"]
        unbatched_residual = ledger_bal["unbatched_residual_net"]

        if net_lifetime_bal < Decimal("0.00"):
            raise PayoutEligibilityError(
                f"Cannot finalize closure: Outstanding merchant liability of ₹{abs(net_lifetime_bal)} must be recovered."
            )
        if net_lifetime_bal > Decimal("0.00"):
            raise PayoutEligibilityError(
                f"Cannot finalize closure: Undisbursed lifetime balance of ₹{net_lifetime_bal} remains payable."
            )
        if unbatched_residual != Decimal("0.00"):
            raise PayoutEligibilityError(
                f"Cannot finalize closure: Unbatched residual balance of ₹{unbatched_residual} must be settled."
            )

        # 8. Mutate Seller state atomically
        now_ts = timezone.now()
        locked_seller.status = Seller.STATUS_CLOSED
        locked_seller.is_active = False
        locked_seller.closed_at = now_ts
        locked_seller.closed_by = actor if (actor and hasattr(actor, "id") and actor.id) else None

        actor_name = getattr(actor, "username", "staff_admin")
        audit_stamp = f"[{now_ts.strftime('%Y-%m-%d %H:%M')}] ACCOUNT CLOSED by {actor_name}. Notes: {clean_notes}"
        locked_seller.closure_notes = f"{audit_stamp}\n{locked_seller.closure_notes}".strip()

        locked_seller.save(
            update_fields=[
                "status",
                "is_active",
                "closed_at",
                "closed_by",
                "closure_notes",
                "updated_at",
            ]
        )

        # 9. Disable / Delist Seller Products (Preserving historical catalog & order references)
        Product.objects.filter(seller=locked_seller, is_active=True).update(is_active=False)

        # 10. Record durable LogEntry audit event
        try:
            from django.contrib.admin.models import CHANGE
            from core.admin_permissions import log_admin_action

            final_batch_ref = (
                clearance.get("existing_final_batch", {}).get("batch_reference")
                if clearance.get("existing_final_batch")
                else "None (zero liability)"
            )
            audit_msg = (
                f"Finalized seller account closure for '{locked_seller.store_name}'. "
                f"Status: CLOSED. Final settlement batch: {final_batch_ref}. Notes: {clean_notes}"
            )
            log_admin_action(actor, locked_seller, CHANGE, audit_msg)
        except Exception as audit_err:
            logger.warning(f"Could not log LogEntry for closed seller #{locked_seller.id}: {audit_err}")

        # 11. Transactional Seller Notification (Deferred post-commit failsafe)
        seller_pk = locked_seller.pk
        store_name = locked_seller.store_name

        def _send_closure_notification():
            try:
                from core.emails import send_seller_closure_finalized_email
                target_seller = Seller.objects.select_related("user").filter(pk=seller_pk).first()
                if target_seller:
                    send_seller_closure_finalized_email(target_seller, notes=clean_notes)
            except Exception as email_err:
                logger.warning(
                    f"Non-blocking notification email failure for closed seller '{store_name}' (#{seller_pk}): {email_err}"
                )

        transaction.on_commit(_send_closure_notification)

        return locked_seller
