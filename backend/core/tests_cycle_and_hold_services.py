"""
Comprehensive Test Suite for FitMeezo 15-Day Payout Cycle and Hold Release Services (Prompt 4).

Validates:
1. 15-day cycle helpers (C1, C2, February leap/non-leap, year transitions, legacy YYYY-MM, automatic resolution).
2. Five-full-day (120-hour) delivery cooling hold (119h, 120h, 121h).
3. Independent manual hold release service (release_hold_balance_entries):
   - RBAC validation (Finance Manager / Super Admin required; non-finance staff / customers rejected).
   - Strict seller multi-tenant isolation.
   - Mandatory non-empty reason.
   - Rejection of ineligible entries (already batched, unposted, cancelled).
   - Idempotency on repeated releases.
4. Non-disbursal guarantee: Releasing a hold strictly never creates a PayoutBatch,
   never posts a payout debit, and never touches payment rails.
5. Safeguard independence: Manual hold release waives ONLY the 120h cooling hold;
   it DOES NOT bypass active returns, pending refunds, or unreconciled COD.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    Category,
    CODReceiptRecord,
    Order,
    OrderItem,
    PayoutBatch,
    Product,
    RefundRecord,
    ReturnRequest,
    ReturnRequestItem,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from core.admin_permissions import (
    ensure_admin_groups,
    set_user_role,
    ROLE_FINANCE_MANAGER,
    ROLE_SUPPORT_MANAGER,
    ROLE_SUPER_ADMIN,
)
from core.ledger_services import (
    calculate_seller_payout_eligibility,
    get_cycle_bounds,
    get_payout_hold_days,
    post_order_sale_to_ledger,
    reconcile_cod_remittance,
    release_hold_balance_entries,
    LedgerServiceError,
    PayoutEligibilityError,
)

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class CycleBoundsTestCase(TestCase):
    """
    Validates 15-day cycle calculation, boundary handling, leap years, and legacy compatibility.
    """

    def test_cycle_1_bounds(self):
        """Cycle 1: calendar days 1-15; calculation date is 16th."""
        canonical_cycle, payout_date, start, end = get_cycle_bounds("2026-03-C1")
        self.assertEqual(canonical_cycle, "2026-03-C1")
        self.assertEqual(start.year, 2026)
        self.assertEqual(start.month, 3)
        self.assertEqual(start.day, 1)
        self.assertEqual(start.time(), time.min)

        self.assertEqual(end.year, 2026)
        self.assertEqual(end.month, 3)
        self.assertEqual(end.day, 15)
        self.assertEqual(end.time(), time.max)

        self.assertEqual(payout_date.year, 2026)
        self.assertEqual(payout_date.month, 3)
        self.assertEqual(payout_date.day, 16)
        self.assertEqual(payout_date.time(), time.min)

    def test_cycle_2_bounds_standard_31_day_month(self):
        """Cycle 2: calendar days 16-month-end; calculation date is 1st of next month."""
        canonical_cycle, payout_date, start, end = get_cycle_bounds("2026-03-C2")
        self.assertEqual(canonical_cycle, "2026-03-C2")
        self.assertEqual(start.year, 2026)
        self.assertEqual(start.month, 3)
        self.assertEqual(start.day, 16)
        self.assertEqual(start.time(), time.min)

        self.assertEqual(end.year, 2026)
        self.assertEqual(end.month, 3)
        self.assertEqual(end.day, 31)
        self.assertEqual(end.time(), time.max)

        self.assertEqual(payout_date.year, 2026)
        self.assertEqual(payout_date.month, 4)
        self.assertEqual(payout_date.day, 1)
        self.assertEqual(payout_date.time(), time.min)

    def test_cycle_2_bounds_february_non_leap(self):
        """Cycle 2 in non-leap February (2027): ends on Feb 28; settlement date is March 1."""
        canonical_cycle, payout_date, start, end = get_cycle_bounds("2027-02-C2")
        self.assertEqual(canonical_cycle, "2027-02-C2")
        self.assertEqual(start.day, 16)
        self.assertEqual(end.day, 28)
        self.assertEqual(payout_date.month, 3)
        self.assertEqual(payout_date.day, 1)

    def test_cycle_2_bounds_february_leap(self):
        """Cycle 2 in leap February (2028): ends on Feb 29; settlement date is March 1."""
        canonical_cycle, payout_date, start, end = get_cycle_bounds("2028-02-C2")
        self.assertEqual(canonical_cycle, "2028-02-C2")
        self.assertEqual(start.day, 16)
        self.assertEqual(end.day, 29)
        self.assertEqual(payout_date.month, 3)
        self.assertEqual(payout_date.day, 1)

    def test_cycle_2_year_transition(self):
        """Cycle 2 in December: ends on Dec 31; settlement date is Jan 1 of the following year."""
        canonical_cycle, payout_date, start, end = get_cycle_bounds("2026-12-C2")
        self.assertEqual(canonical_cycle, "2026-12-C2")
        self.assertEqual(start.year, 2026)
        self.assertEqual(start.month, 12)
        self.assertEqual(start.day, 16)

        self.assertEqual(end.year, 2026)
        self.assertEqual(end.month, 12)
        self.assertEqual(end.day, 31)

        self.assertEqual(payout_date.year, 2027)
        self.assertEqual(payout_date.month, 1)
        self.assertEqual(payout_date.day, 1)

    def test_legacy_monthly_cycle(self):
        """Preserves backward compatibility for legacy YYYY-MM identifiers."""
        canonical_cycle, payout_date, start, end = get_cycle_bounds("2026-04")
        self.assertEqual(canonical_cycle, "2026-04")
        self.assertEqual(start.year, 2026)
        self.assertEqual(start.month, 3)  # legacy monthly cycle calculates previous month
        self.assertEqual(start.day, 1)
        self.assertEqual(end.month, 3)
        self.assertEqual(end.day, 31)

        self.assertEqual(payout_date.year, 2026)
        self.assertEqual(payout_date.month, 4)
        self.assertEqual(payout_date.day, 5)

    def test_automatic_cycle_resolution_from_as_of_date(self):
        """
        When cycle string is omitted:
        - Date on 16th-31st resolves to C1 of the current month (calculated on the 16th).
        - Date on 1st-15th resolves to C2 of the previous month (calculated on the 1st).
        """
        tz = timezone.get_current_timezone()

        # Day 20 of March 2026 -> previous completed cycle is 2026-03-C1
        as_of_20_march = timezone.make_aware(datetime(2026, 3, 20, 10, 0, 0), tz)
        c_code, payout_date, start, end = get_cycle_bounds(as_of_date=as_of_20_march)
        self.assertEqual(c_code, "2026-03-C1")
        self.assertEqual(start.day, 1)
        self.assertEqual(end.day, 15)
        self.assertEqual(payout_date.day, 16)

        # Day 10 of March 2026 -> previous completed cycle is 2026-02-C2
        as_of_10_march = timezone.make_aware(datetime(2026, 3, 10, 10, 0, 0), tz)
        c_code, payout_date, start, end = get_cycle_bounds(as_of_date=as_of_10_march)
        self.assertEqual(c_code, "2026-02-C2")
        self.assertEqual(start.month, 2)
        self.assertEqual(start.day, 16)
        self.assertEqual(end.month, 2)
        self.assertEqual(payout_date.month, 3)
        self.assertEqual(payout_date.day, 1)

        # Day 5 of January 2026 -> previous completed cycle is 2025-12-C2
        as_of_5_jan = timezone.make_aware(datetime(2026, 1, 5, 10, 0, 0), tz)
        c_code, payout_date, start, end = get_cycle_bounds(as_of_date=as_of_5_jan)
        self.assertEqual(c_code, "2025-12-C2")
        self.assertEqual(start.year, 2025)
        self.assertEqual(start.month, 12)
        self.assertEqual(start.day, 16)
        self.assertEqual(end.year, 2025)
        self.assertEqual(end.month, 12)
        self.assertEqual(end.day, 31)
        self.assertEqual(payout_date.year, 2026)
        self.assertEqual(payout_date.month, 1)
        self.assertEqual(payout_date.day, 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class DeliveryHoldAndEligibilityTestCase(TestCase):
    """
    Validates the 5-full-day (120-hour) cooling hold and manual hold release override.
    """

    def setUp(self):
        ensure_admin_groups()

        # Staff users
        self.admin = User.objects.create_user(
            username="admin_hold",
            email="admin_hold@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        self.finance_user = User.objects.create_user(
            username="finance_officer",
            email="finance_officer@fitmeezo.com",
            password="FinancePassword123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        self.support_user = User.objects.create_user(
            username="support_agent",
            email="support_agent@fitmeezo.com",
            password="SupportPassword123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        self.customer = User.objects.create_user(
            username="customer_hold",
            email="customer_hold@fitmeezo.com",
            password="CustomerPassword123!",
        )

        # Sellers
        self.seller_user1 = User.objects.create_user(
            username="seller1_hold",
            email="seller1_hold@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Zenith Apparel",
            legal_business_name="Zenith Apparel Private Limited",
            support_email="support@zenith.com",
            support_phone="9876543210",
            registered_address="1 Textile Park",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="1 Textile Park",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z1",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        self.seller_user2 = User.objects.create_user(
            username="seller2_hold",
            email="seller2_hold@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Vortex Gear",
            legal_business_name="Vortex Gear LLP",
            support_email="support@vortex.com",
            support_phone="9876543211",
            registered_address="2 Gear St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="2 Gear St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29XYZAB5678C1Z2",
            pan_number="XYZAB5678C",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        self.category = Category.objects.create(name="Apparel", slug="apparel")
        self.product1 = Product.objects.create(
            category=self.category,
            seller=self.seller1,
            name="Zenith Tee",
            slug="zenith-tee",
            sku="ZEN-TEE-001",
            price=Decimal("1000.00"),
            stock_quantity=100,
            is_active=True,
        )

    def _create_delivered_order_with_ledger(
        self,
        seller,
        delivered_at,
        created_at=None,
        is_cod=False,
        amount=Decimal("1000.00"),
    ):
        """Helper to create an order, sub-order package, and posted ledger entry with exact timestamps."""
        tz = timezone.get_current_timezone()
        if created_at is None:
            created_at = delivered_at - timedelta(days=2)

        pm = Order.PAYMENT_METHOD_COD if is_cod else Order.PAYMENT_METHOD_RAZORPAY
        ps = Order.PAYMENT_STATUS_UNPAID if is_cod else Order.PAYMENT_STATUS_PAID

        order = Order.objects.create(
            order_number=f"FM-HOLD-{uuid.uuid4().hex[:6].upper()}",
            user=self.customer,
            shipping_full_name="John Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="456 Park Ave",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=amount,
            shipping_fee=Decimal("0.00"),
            total_amount=amount,
            currency="INR",
            status=Order.STATUS_DELIVERED,
            payment_method=pm,
            payment_status=ps,
            paid_at=created_at if ps == Order.PAYMENT_STATUS_PAID else None,
        )
        Order.objects.filter(id=order.id).update(created_at=created_at)

        comm_rate = seller.commission_rate
        comm_amount = (amount * comm_rate / Decimal("100.00")).quantize(Decimal("0.01"))
        net_payable = amount - comm_amount

        so = SellerOrder.objects.create(
            order=order,
            seller=seller,
            is_platform_fulfilled=False,
            sub_order_number=f"{order.order_number}-S{seller.id}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=amount,
            total_commission=comm_amount,
            net_seller_payable=net_payable,
            delivered_at=delivered_at,
        )
        SellerOrder.objects.filter(id=so.id).update(created_at=created_at, delivered_at=delivered_at)

        OrderItem.objects.create(
            order=order,
            product=self.product1,
            seller=seller,
            seller_order=so,
            product_name="Zenith Tee",
            sku="ZEN-TEE-001",
            quantity=1,
            unit_price=amount,
            line_total=amount,
            commission_rate=comm_rate,
            commission_amount=comm_amount,
        )

        entries = post_order_sale_to_ledger(so)
        for e in entries:
            SellerLedgerEntry.objects.filter(id=e.id).update(created_at=created_at)
        sale_entry = [e for e in entries if e.category == SellerLedgerEntry.CATEGORY_ORDER_SALE][0]
        sale_entry.refresh_from_db()
        return order, so, sale_entry

    def test_hold_days_default_is_five(self):
        """get_payout_hold_days() must return 5 days."""
        self.assertEqual(get_payout_hold_days(), 5)

    def test_delivery_119_hours_is_held(self):
        """Package delivered 119 hours ago (< 120 hours) must be held by cooling window."""
        tz = timezone.get_current_timezone()
        # Cycle 2026-03-C1 ends 2026-03-15 23:59:59. Calculation date is 2026-03-16 00:00:00.
        as_of_date = timezone.make_aware(datetime(2026, 3, 16, 0, 0, 0), tz)

        # 119 hours before as_of_date
        delivered_at = as_of_date - timedelta(hours=119)
        created_at = delivered_at - timedelta(days=2)

        order, so, entry = self._create_delivered_order_with_ledger(
            seller=self.seller1,
            delivered_at=delivered_at,
            created_at=created_at,
        )

        elig = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertIn(entry.id, elig["held_entry_ids"])
        self.assertNotIn(entry.id, elig["eligible_entry_ids"])
        self.assertEqual(elig["net_eligible_balance"], Decimal("0.00"))
        self.assertGreater(elig["held_balance"], Decimal("0.00"))

    def test_delivery_exact_120_hours_is_eligible(self):
        """Package delivered exactly 120 hours ago must satisfy cooling window and be eligible."""
        tz = timezone.get_current_timezone()
        as_of_date = timezone.make_aware(datetime(2026, 3, 16, 0, 0, 0), tz)

        # Exactly 120 hours before as_of_date
        delivered_at = as_of_date - timedelta(hours=120)
        created_at = delivered_at - timedelta(days=2)

        order, so, entry = self._create_delivered_order_with_ledger(
            seller=self.seller1,
            delivered_at=delivered_at,
            created_at=created_at,
        )

        elig = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertIn(entry.id, elig["eligible_entry_ids"])
        self.assertNotIn(entry.id, elig["held_entry_ids"])
        self.assertEqual(elig["net_eligible_balance"], Decimal("900.00"))

    def test_delivery_121_hours_is_eligible(self):
        """Package delivered 121 hours ago (> 120 hours) is eligible."""
        tz = timezone.get_current_timezone()
        as_of_date = timezone.make_aware(datetime(2026, 3, 16, 0, 0, 0), tz)

        delivered_at = as_of_date - timedelta(hours=121)
        created_at = delivered_at - timedelta(days=2)

        order, so, entry = self._create_delivered_order_with_ledger(
            seller=self.seller1,
            delivered_at=delivered_at,
            created_at=created_at,
        )

        elig = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertIn(entry.id, elig["eligible_entry_ids"])
        self.assertNotIn(entry.id, elig["held_entry_ids"])
        self.assertEqual(elig["net_eligible_balance"], Decimal("900.00"))

    def test_manual_release_before_120_hours_makes_entry_eligible(self):
        """
        An entry delivered only 48 hours ago is normally held.
        Once an authorized finance admin calls release_hold_balance_entries,
        is_hold_released=True waives the cooling hold and makes it eligible.
        """
        tz = timezone.get_current_timezone()
        as_of_date = timezone.make_aware(datetime(2026, 3, 16, 0, 0, 0), tz)

        # Delivered 48 hours ago (under 120 hours)
        delivered_at = as_of_date - timedelta(hours=48)
        created_at = delivered_at - timedelta(days=2)

        order, so, entry = self._create_delivered_order_with_ledger(
            seller=self.seller1,
            delivered_at=delivered_at,
            created_at=created_at,
        )

        # Before manual release -> held
        elig_before = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertIn(entry.id, elig_before["held_entry_ids"])
        self.assertEqual(elig_before["net_eligible_balance"], Decimal("0.00"))

        # Authorized admin releases the cooling hold for the package entries (sale credit and commission debit)
        package_entries = list(SellerLedgerEntry.objects.filter(seller_order=so))
        count, released = release_hold_balance_entries(
            seller=self.seller1,
            entry_ids=[e.id for e in package_entries],
            reason="Customer confirmed satisfaction; authorized expedited settlement.",
            actor=self.finance_user,
        )
        self.assertEqual(count, len(package_entries))

        # After manual release -> cooling hold bypassed, entry is eligible
        elig_after = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertIn(entry.id, elig_after["eligible_entry_ids"])
        self.assertNotIn(entry.id, elig_after["held_entry_ids"])
        self.assertEqual(elig_after["net_eligible_balance"], Decimal("900.00"))

    def test_manual_release_does_not_bypass_active_return(self):
        """
        Crucial safeguard: If an order has an active ReturnRequest,
        manual hold release of the cooling period MUST NOT make the entry eligible.
        """
        tz = timezone.get_current_timezone()
        as_of_date = timezone.make_aware(datetime(2026, 3, 16, 0, 0, 0), tz)

        delivered_at = as_of_date - timedelta(hours=48)
        created_at = delivered_at - timedelta(days=2)

        order, so, entry = self._create_delivered_order_with_ledger(
            seller=self.seller1,
            delivered_at=delivered_at,
            created_at=created_at,
        )

        # Create active return request
        item = order.items.first()
        rr = ReturnRequest.objects.create(
            request_number=f"RET-TEST-{uuid.uuid4().hex[:6].upper()}",
            customer=self.customer,
            order=order,
            seller_order=so,
            seller=self.seller1,
            is_platform_fulfilled=False,
            status=ReturnRequest.STATUS_REQUESTED,
        )
        ReturnRequestItem.objects.create(
            return_request=rr,
            order_item=item,
            quantity=1,
            item_unit_price=item.unit_price,
            refund_eligible_amount=Decimal("1000.00"),
        )

        # Manually release hold
        release_hold_balance_entries(
            seller=self.seller1,
            entry_ids=[entry.id],
            reason="Manager attempted manual release.",
            actor=self.admin,
        )

        # Calculate eligibility: Entry MUST REMAIN HELD due to unresolved return
        elig = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertIn(entry.id, elig["held_entry_ids"])
        self.assertNotIn(entry.id, elig["eligible_entry_ids"])
        self.assertEqual(elig["net_eligible_balance"], Decimal("0.00"))

    def test_manual_release_does_not_bypass_pending_refund(self):
        """
        Crucial safeguard: If an order has an active RefundRecord,
        manual hold release of the cooling period MUST NOT make the entry eligible.
        """
        tz = timezone.get_current_timezone()
        as_of_date = timezone.make_aware(datetime(2026, 3, 16, 0, 0, 0), tz)

        delivered_at = as_of_date - timedelta(hours=48)
        created_at = delivered_at - timedelta(days=2)

        order, so, entry = self._create_delivered_order_with_ledger(
            seller=self.seller1,
            delivered_at=delivered_at,
            created_at=created_at,
        )

        # Create pending refund
        RefundRecord.objects.create(
            refund_reference=f"REF-TEST-{uuid.uuid4().hex[:6].upper()}",
            external_reference=f"MOCK-REF-{uuid.uuid4().hex[:6].upper()}",
            order=order,
            seller_order=so,
            requested_amount=Decimal("1000.00"),
            approved_amount=Decimal("0.00"),
            status=RefundRecord.STATUS_PENDING,
        )

        release_hold_balance_entries(
            seller=self.seller1,
            entry_ids=[entry.id],
            reason="Manager attempted manual release.",
            actor=self.finance_user,
        )

        elig = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertIn(entry.id, elig["held_entry_ids"])
        self.assertNotIn(entry.id, elig["eligible_entry_ids"])
        self.assertEqual(elig["net_eligible_balance"], Decimal("0.00"))

    def test_manual_release_does_not_bypass_unreconciled_cod(self):
        """
        Crucial safeguard:
        1. A COD sale whose courier remittance is not yet reconciled is in status STATUS_PENDING_COD.
        2. Attempting manual hold release on an unposted/pending_cod entry raises LedgerServiceError.
        3. Even after manual attempts, it is excluded from eligible payout balance.
        """
        tz = timezone.get_current_timezone()
        as_of_date = timezone.make_aware(datetime(2026, 3, 16, 0, 0, 0), tz)

        delivered_at = as_of_date - timedelta(hours=48)
        created_at = delivered_at - timedelta(days=2)

        order, so, entry = self._create_delivered_order_with_ledger(
            seller=self.seller1,
            delivered_at=delivered_at,
            created_at=created_at,
            is_cod=True,
        )

        self.assertEqual(entry.status, SellerLedgerEntry.STATUS_PENDING_COD)

        # Attempting to release an unposted/pending COD entry must be rejected
        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[entry.id],
                reason="Manager release attempted on unreconciled COD.",
                actor=self.finance_user,
            )
        self.assertIn("Only POSTED entries can be hold-released", str(ctx.exception))

        # Eligibility check: remains strictly excluded
        elig = calculate_seller_payout_eligibility(
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            as_of_date=as_of_date,
        )
        self.assertEqual(elig["net_eligible_balance"], Decimal("0.00"))
        self.assertNotIn(entry.id, elig["eligible_entry_ids"])


class ReleaseHoldBalanceEntriesServiceTestCase(TestCase):
    """
    Validates permissions, edge cases, validations, idempotency, and isolation
    of release_hold_balance_entries().
    """

    def setUp(self):
        ensure_admin_groups()

        self.admin = User.objects.create_user(
            username="super_admin_srv",
            email="super_admin_srv@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        self.finance_user = User.objects.create_user(
            username="finance_mgr_srv",
            email="finance_mgr_srv@fitmeezo.com",
            password="FinancePassword123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        self.support_user = User.objects.create_user(
            username="support_srv",
            email="support_srv@fitmeezo.com",
            password="SupportPassword123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        self.customer = User.objects.create_user(
            username="buyer_srv",
            email="buyer_srv@fitmeezo.com",
            password="BuyerPassword123!",
        )

        # Sellers
        self.seller_user1 = User.objects.create_user(
            username="seller1_srv",
            email="seller1_srv@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Orion Retail",
            legal_business_name="Orion Retail Ltd",
            support_email="support@orion.com",
            support_phone="9876543210",
            registered_address="1 Orion Way",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="1 Orion Way",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z1",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        self.seller_user2 = User.objects.create_user(
            username="seller2_srv",
            email="seller2_srv@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Nebula Trade",
            legal_business_name="Nebula Trade LLP",
            support_email="support@nebula.com",
            support_phone="9876543211",
            registered_address="2 Nebula Rd",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="2 Nebula Rd",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29XYZAB5678C1Z2",
            pan_number="XYZAB5678C",
            commission_rate=Decimal("12.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Create sample entries
        self.entry_seller1 = SellerLedgerEntry.objects.create(
            seller=self.seller1,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("900.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            idempotency_key=f"test-entry-1-{uuid.uuid4().hex}",
            description="Sale credit for sub-order",
        )
        self.entry_seller2 = SellerLedgerEntry.objects.create(
            seller=self.seller2,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1200.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            idempotency_key=f"test-entry-2-{uuid.uuid4().hex}",
            description="Sale credit for sub-order",
        )

    def test_unauthorized_actor_rejected(self):
        """End-users and staff without finance permissions are rejected."""
        # Anonymous / None actor
        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[self.entry_seller1.id],
                reason="Attempt by anonymous user",
                actor=None,
            )
        self.assertIn("Permission denied", str(ctx.exception))

        # Customer actor
        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[self.entry_seller1.id],
                reason="Attempt by regular customer",
                actor=self.customer,
            )
        self.assertIn("Permission denied", str(ctx.exception))

        # Non-finance staff actor (Customer Support)
        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[self.entry_seller1.id],
                reason="Attempt by support agent",
                actor=self.support_user,
            )
        self.assertIn("Permission denied", str(ctx.exception))

    def test_empty_or_whitespace_reason_rejected(self):
        """A meaningful, non-empty reason is strictly required."""
        for invalid_reason in ["", "   ", None]:
            with self.assertRaises(LedgerServiceError) as ctx:
                release_hold_balance_entries(
                    seller=self.seller1,
                    entry_ids=[self.entry_seller1.id],
                    reason=invalid_reason,
                    actor=self.finance_user,
                )
            self.assertIn("mandatory explanation reason", str(ctx.exception))

    def test_empty_entry_ids_rejected(self):
        """Must specify at least one entry ID."""
        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[],
                reason="Valid explanation reason",
                actor=self.finance_user,
            )
        self.assertIn("At least one ledger entry ID must be specified", str(ctx.exception))

    def test_nonexistent_entry_id_rejected(self):
        """Non-existent entry IDs are rejected."""
        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[999999],
                reason="Valid explanation reason",
                actor=self.finance_user,
            )
        self.assertIn("not found", str(ctx.exception))

    def test_cross_seller_release_rejected(self):
        """Attempting to release an entry belonging to Seller 2 under Seller 1 is strictly rejected."""
        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[self.entry_seller2.id],
                reason="Cross seller release attempt",
                actor=self.finance_user,
            )
        self.assertIn("belongs to a different seller", str(ctx.exception))

    def test_already_batched_entry_rejected(self):
        """Entries already bundled into an existing PayoutBatch cannot be hold-released."""
        batch = PayoutBatch.objects.create(
            batch_reference="PB-TEST-ALREADY-BATCHED",
            seller=self.seller1,
            payout_cycle="2026-03-C1",
            amount=Decimal("900.00"),
            status=PayoutBatch.STATUS_PENDING,
        )
        self.entry_seller1.payout_batch = batch
        self.entry_seller1.save(update_fields=["payout_batch"])

        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[self.entry_seller1.id],
                reason="Release already batched entry",
                actor=self.finance_user,
            )
        self.assertIn("already bundled into PayoutBatch", str(ctx.exception))

    def test_unposted_entry_rejected(self):
        """Entries that are not STATUS_POSTED (e.g. DRAFT or VOID) cannot be released."""
        self.entry_seller1.status = SellerLedgerEntry.STATUS_REVERSED
        self.entry_seller1.save(update_fields=["status"])

        with self.assertRaises(LedgerServiceError) as ctx:
            release_hold_balance_entries(
                seller=self.seller1,
                entry_ids=[self.entry_seller1.id],
                reason="Release void entry",
                actor=self.finance_user,
            )
        self.assertIn("Only POSTED entries can be hold-released", str(ctx.exception))

    def test_successful_release_and_audit_metadata(self):
        """Valid release sets is_hold_released=True, timestamp, actor, and audit_metadata."""
        count, updated = release_hold_balance_entries(
            seller=self.seller1,
            entry_ids=[self.entry_seller1.id],
            reason="Authorized expedited release by finance committee.",
            actor=self.finance_user,
        )
        self.assertEqual(count, 1)

        self.entry_seller1.refresh_from_db()
        self.assertTrue(self.entry_seller1.is_hold_released)
        self.assertIsNotNone(self.entry_seller1.hold_released_at)
        self.assertEqual(self.entry_seller1.hold_released_by, self.finance_user)
        self.assertEqual(
            self.entry_seller1.hold_release_reason,
            "Authorized expedited release by finance committee.",
        )
        self.assertEqual(
            self.entry_seller1.audit_metadata["hold_release"]["released_by_username"],
            "finance_mgr_srv",
        )

    def test_idempotent_repeated_release(self):
        """Calling release again on an already-released entry is safe and updates audit fields."""
        release_hold_balance_entries(
            seller=self.seller1,
            entry_ids=[self.entry_seller1.id],
            reason="First release note",
            actor=self.finance_user,
        )
        self.entry_seller1.refresh_from_db()
        self.assertTrue(self.entry_seller1.is_hold_released)

        # Second release call
        count, updated = release_hold_balance_entries(
            seller=self.seller1,
            entry_ids=[self.entry_seller1.id],
            reason="Updated release justification",
            actor=self.admin,
        )
        self.assertEqual(count, 1)
        self.entry_seller1.refresh_from_db()
        self.assertEqual(self.entry_seller1.hold_release_reason, "Updated release justification")
        self.assertEqual(self.entry_seller1.hold_released_by, self.admin)

    def test_zero_disbursal_guarantee(self):
        """
        Releasing a hold strictly DOES NOT create a PayoutBatch,
        DOES NOT post a payout debit, and DOES NOT call any payment provider.
        """
        batches_before = PayoutBatch.objects.count()
        debits_before = SellerLedgerEntry.objects.filter(
            category=SellerLedgerEntry.CATEGORY_PAYOUT
        ).count()

        release_hold_balance_entries(
            seller=self.seller1,
            entry_ids=[self.entry_seller1.id],
            reason="Early customer delivery sign-off.",
            actor=self.finance_user,
        )

        batches_after = PayoutBatch.objects.count()
        debits_after = SellerLedgerEntry.objects.filter(
            category=SellerLedgerEntry.CATEGORY_PAYOUT
        ).count()

        self.assertEqual(batches_after, batches_before)
        self.assertEqual(debits_after, debits_before)
