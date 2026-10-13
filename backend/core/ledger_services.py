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
from datetime import datetime, time, timedelta
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


class ReconciliationError(LedgerServiceError):
    """Exception raised on invalid COD reconciliation attempt."""
    pass


def get_payout_hold_days() -> int:
    """Returns the holding period in days post-delivery before order funds are payout-eligible."""
    return getattr(settings, "FITMEEZO_PAYOUT_HOLD_DAYS_AFTER_DELIVERY", 7)


def get_min_payout_threshold() -> Decimal:
    """Returns the minimum net balance in INR required to generate a payout batch."""
    return getattr(settings, "FITMEEZO_MIN_PAYOUT_THRESHOLD", Decimal("1.00"))


def get_cycle_bounds(
    cycle_str: str | None = None,
    as_of_date=None,
) -> tuple[str, datetime, datetime, datetime]:
    """
    Computes timezone-aware monthly payout cycle bounds.
    - Payout cycle is on the 5th of each month.
    - Eligible earnings are calculated from the previous calendar month.
    Returns:
    (canonical_cycle: str, cycle_payout_date: datetime, prev_month_start: datetime, prev_month_end: datetime)
    """
    tz = timezone.get_current_timezone()
    if as_of_date is None:
        as_of_date = timezone.now()

    if cycle_str:
        try:
            parts = str(cycle_str).strip().split("-")
            cycle_year = int(parts[0])
            cycle_month = int(parts[1])
        except (ValueError, IndexError):
            cycle_year = as_of_date.year
            cycle_month = as_of_date.month
    else:
        cycle_year = as_of_date.year
        cycle_month = as_of_date.month

    # Canonical cycle format YYYY-MM
    canonical_cycle = f"{cycle_year:04d}-{cycle_month:02d}"

    # Cycle payout date (5th of the cycle month)
    cycle_day = getattr(settings, "FITMEEZO_PAYOUT_CYCLE_DAY", 5)
    cycle_payout_date = timezone.make_aware(
        datetime(cycle_year, cycle_month, cycle_day, 0, 0, 0),
        tz,
    )

    # Previous calendar month
    if cycle_month == 1:
        prev_year = cycle_year - 1
        prev_month = 12
    else:
        prev_year = cycle_year
        prev_month = cycle_month - 1

    _, last_day = calendar.monthrange(prev_year, prev_month)
    prev_month_start = timezone.make_aware(
        datetime(prev_year, prev_month, 1, 0, 0, 0),
        tz,
    )
    prev_month_end = timezone.make_aware(
        datetime(prev_year, prev_month, last_day, 23, 59, 59, 999999),
        tz,
    )

    return canonical_cycle, cycle_payout_date, prev_month_start, prev_month_end


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
) -> dict:
    """
    Evaluates net payout eligibility for a specific merchant or platform warehouse:
    Monthly Payout Rules:
    1. Payout cycle is on the 5th of each month.
    2. Calculates eligible earnings from the previous calendar month.
    3. Requires 7 complete days after confirmed delivery before an order package becomes eligible.
    4. Excludes orders with unresolved returns, refunds, disputes, or unreconciled COD remittance.
    5. Ineligible orders are kept on hold and carried forward into a future cycle.
    6. Double-entry accounting: Net balance = sum(eligible credits) - sum(eligible debits).
    7. Fully timezone-aware dates.
    """
    hold_days = get_payout_hold_days()

    if cycle_month:
        canonical_cycle, cycle_payout_date, prev_month_start, prev_month_end = get_cycle_bounds(
            cycle_str=cycle_month,
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

    cutoff_delivery_time = as_of_date - timedelta(days=hold_days)

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
        # 1. COD pending entries are strictly excluded
        if entry.status == SellerLedgerEntry.STATUS_PENDING_COD:
            held_entry_ids.append(entry.id)
            held_entry_details.append({
                "entry_id": entry.id,
                "amount": entry.amount,
                "type": entry.entry_type,
                "category": entry.category,
                "reason": "Courier COD cash remittance not yet reconciled.",
            })
            continue

        if entry.status != SellerLedgerEntry.STATUS_POSTED:
            # Reversed or non-posted entries do not qualify
            continue

        # 2. Monthly cutoff: If cycle_month is specified, orders/entries from future cycles are carried forward
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
                    "reason": f"Current month transaction (carried forward to next cycle after {prev_month_end.strftime('%b %Y')}).",
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
                })
                continue

            # 7-day cooling window post-delivery
            del_time = pkg.delivered_at or pkg.updated_at
            if del_time and del_time > cutoff_delivery_time:
                held_entry_ids.append(entry.id)
                days_left = max(1, ((del_time + timedelta(days=hold_days)) - as_of_date).days + 1)
                held_entry_details.append({
                    "entry_id": entry.id,
                    "amount": entry.amount,
                    "type": entry.entry_type,
                    "category": entry.category,
                    "reason": f"7-day delivery cooling period active ({days_left}d remaining).",
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
                })
                continue

            # Unresolved refunds
            unresolved_refunds = RefundRecord.objects.filter(
                seller_order=pkg,
                status__in=[RefundRecord.STATUS_PENDING, RefundRecord.STATUS_FAILED],
            ).exists()
            if unresolved_refunds:
                held_entry_ids.append(entry.id)
                held_entry_details.append({
                    "entry_id": entry.id,
                    "amount": entry.amount,
                    "type": entry.entry_type,
                    "category": entry.category,
                    "reason": "Customer refund record in pending/disputed state.",
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

        # Held payment must not be sent
        if locked_batch.status == PayoutBatch.STATUS_ON_HOLD:
            raise LedgerServiceError(
                f"Payout batch #{locked_batch.batch_reference} is ON HOLD ({locked_batch.hold_reason or 'Admin hold'}). "
                "Held payments cannot be disbursed."
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
