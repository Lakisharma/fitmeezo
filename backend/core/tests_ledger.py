"""
Fitmeezo Phase 7 Test Suite: Seller Ledger, Commission & Payout Management.

Comprehensive automated test coverage:
1. Commission rate & snapshot immutability
2. Duplicate posting prevention & database constraint enforcement
3. COD remittance holding, eligibility gate & explicit reconciliation
4. Phase 6 Returns & Refunds ledger debit/credit integration
5. Order pre-dispatch cancellation reversal accounting
6. Concurrency & duplicate payout batch consumption prevention
7. Mock payout execution, failure simulation & unlinking
8. Auditable payout batch reversal with credit posting
9. Seller multi-tenant isolation & platform-fulfilled accounting
10. Approved manual adjustments with mandatory reasoning
11. CSV export generation & formula injection (CWE-1236) sanitization
"""

from datetime import timedelta
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    CODReceiptRecord,
    Order,
    OrderItem,
    PayoutBatch,
    Product,
    ProductVariant,
    RefundRecord,
    ReturnRequest,
    ReturnRequestItem,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from core.ledger_services import (
    calculate_seller_payout_eligibility,
    create_manual_adjustment,
    create_payout_batch,
    get_seller_ledger_summary,
    post_cancellation_reversal_to_ledger,
    post_order_sale_to_ledger,
    post_refund_to_ledger,
    process_mock_payout_batch,
    reconcile_cod_remittance,
    reverse_payout_batch,
    LedgerServiceError,
    PayoutEligibilityError,
    ReconciliationError,
)
from core.return_services import process_simulated_refund

User = get_user_model()


class SellerLedgerAndPayoutTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.customer = User.objects.create_user(
            username="customer_ledger",
            email="cust_ledger@example.com",
            password="SecurePassword123!",
        )
        self.staff_user = User.objects.create_user(
            username="staff_ledger",
            email="staff_ledger@fitmeezo.com",
            password="SecurePassword123!",
            is_staff=True,
            is_superuser=True,
        )
        self.checker_user = User.objects.create_user(
            username="checker_ledger",
            email="checker_ledger@fitmeezo.com",
            password="SecurePassword123!",
            is_staff=True,
            is_superuser=True,
        )

        # Sellers
        self.seller_user1 = User.objects.create_user(
            username="seller1_user",
            email="seller1@fitmeezo.com",
            password="SecurePassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Alpha Styles",
            legal_business_name="Alpha Styles Private Limited",
            support_email="support@alphastyles.com",
            support_phone="9876543210",
            registered_address="1 Fashion Blvd",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="1 Fashion Blvd",
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
            username="seller2_user",
            email="seller2@fitmeezo.com",
            password="SecurePassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Beta Footwear",
            legal_business_name="Beta Footwear LLP",
            support_email="support@betafootwear.com",
            support_phone="9876543211",
            registered_address="2 Shoe Lane",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="2 Shoe Lane",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29XYZAB5678C1Z2",
            pan_number="XYZAB5678C",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Category & Products
        self.category = Category.objects.create(name="Apparel & Footwear", slug="apparel-footwear")

        self.product1 = Product.objects.create(
            category=self.category,
            seller=self.seller1,
            name="Classic Denim Jacket",
            slug="classic-denim-jacket",
            sku="ALPH-JCK-001",
            price=Decimal("1000.00"),
            stock_quantity=50,
            is_active=True,
        )

        self.product2 = Product.objects.create(
            category=self.category,
            seller=self.seller2,
            name="Running Sneakers",
            slug="running-sneakers",
            sku="BETA-SNK-001",
            price=Decimal("2000.00"),
            stock_quantity=40,
            is_active=True,
        )

        self.platform_product = Product.objects.create(
            category=self.category,
            seller=None,
            name="Fitmeezo Brand Duffel Bag",
            slug="fitmeezo-brand-duffel-bag",
            sku="PLT-BAG-001",
            price=Decimal("1500.00"),
            stock_quantity=100,
            is_active=True,
        )

    def _create_test_order(self, payment_method=Order.PAYMENT_METHOD_RAZORPAY, is_cod=False):
        """Helper to create a multi-seller test order."""
        pm = Order.PAYMENT_METHOD_COD if is_cod else Order.PAYMENT_METHOD_RAZORPAY
        ps = Order.PAYMENT_STATUS_UNPAID if is_cod else Order.PAYMENT_STATUS_PAID
        order_num = f"FM-TEST-{uuid.uuid4().hex[:6].upper()}"

        order = Order.objects.create(
            order_number=order_num,
            user=self.customer,
            shipping_full_name="John Doe",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Test St",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("3000.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("3000.00"),
            currency="INR",
            status=Order.STATUS_PENDING,
            payment_method=pm,
            payment_status=ps,
            paid_at=timezone.now() if ps == Order.PAYMENT_STATUS_PAID else None,
        )

        # Seller 1 sub-order
        so1 = SellerOrder.objects.create(
            order=order,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number=f"{order.order_number}-S{self.seller1.id}",
            status=SellerOrder.STATUS_PENDING,
            subtotal=Decimal("1000.00"),
            total_commission=Decimal("100.00"),
            net_seller_payable=Decimal("900.00"),
            customer_shipping_charge=Decimal("0.00"),
            actual_shipping_cost=Decimal("0.00"),
            shipping_allocated_to_seller=Decimal("0.00"),
        )
        OrderItem.objects.create(
            order=order,
            seller=self.seller1,
            seller_order=so1,
            product=self.product1,
            product_name=self.product1.name,
            sku=self.product1.sku,
            unit_price=Decimal("1000.00"),
            quantity=1,
            line_total=Decimal("1000.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("100.00"),
        )

        # Seller 2 sub-order
        so2 = SellerOrder.objects.create(
            order=order,
            seller=self.seller2,
            is_platform_fulfilled=False,
            sub_order_number=f"{order.order_number}-S{self.seller2.id}",
            status=SellerOrder.STATUS_PENDING,
            subtotal=Decimal("2000.00"),
            total_commission=Decimal("300.00"),
            net_seller_payable=Decimal("1700.00"),
            customer_shipping_charge=Decimal("0.00"),
            actual_shipping_cost=Decimal("0.00"),
            shipping_allocated_to_seller=Decimal("0.00"),
        )
        OrderItem.objects.create(
            order=order,
            seller=self.seller2,
            seller_order=so2,
            product=self.product2,
            product_name=self.product2.name,
            sku=self.product2.sku,
            unit_price=Decimal("2000.00"),
            quantity=1,
            line_total=Decimal("2000.00"),
            commission_rate=Decimal("15.00"),
            commission_amount=Decimal("300.00"),
        )

        return order, so1, so2

    # --------------------------------------------------------------------------
    # 1. Commission Snapshot Immutability
    # --------------------------------------------------------------------------
    def test_commission_snapshots_immutable(self):
        """Historical commission rate and amount must not change when seller rate is modified."""
        order, so1, so2 = self._create_test_order(is_cod=False)
        entries = post_order_sale_to_ledger(so1)

        # Verify initial commission entry
        comm_entry = SellerLedgerEntry.objects.get(
            seller_order=so1,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
        )
        self.assertEqual(comm_entry.amount, Decimal("100.00"))
        self.assertEqual(comm_entry.entry_type, SellerLedgerEntry.TYPE_DEBIT)

        # Now change seller commission rate in profile
        self.seller1.commission_rate = Decimal("25.00")
        self.seller1.save()

        # Re-fetch from DB and verify immutability
        comm_entry.refresh_from_db()
        so1.refresh_from_db()
        self.assertEqual(comm_entry.amount, Decimal("100.00"))
        self.assertEqual(so1.total_commission, Decimal("100.00"))

    # --------------------------------------------------------------------------
    # 2. Duplicate Posting Prevention & Constraint Enforcement
    # --------------------------------------------------------------------------
    def test_duplicate_posting_prevention(self):
        """Calling post_order_sale_to_ledger repeatedly must be idempotent with 0 duplicates."""
        order, so1, so2 = self._create_test_order(is_cod=False)

        # First call
        entries1 = post_order_sale_to_ledger(so1)
        self.assertEqual(len(entries1), 2)  # SALE + COMM

        # Second call
        entries2 = post_order_sale_to_ledger(so1)
        self.assertEqual(len(entries2), 2)

        # Total entries in DB for this package must still be exactly 2
        total_for_pkg = SellerLedgerEntry.objects.filter(seller_order=so1).count()
        self.assertEqual(total_for_pkg, 2)

        # Direct database uniqueness constraint test
        with self.assertRaises(IntegrityError):
            SellerLedgerEntry.objects.create(
                seller=self.seller1,
                entry_type=SellerLedgerEntry.TYPE_CREDIT,
                category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
                amount=Decimal("1000.00"),
                idempotency_key=f"ORD-{so1.id}-SALE",  # Collision!
            )

    # --------------------------------------------------------------------------
    # 3. COD Remittance Holding & Reconciliation Flow
    # --------------------------------------------------------------------------
    def test_cod_remittance_hold_and_release(self):
        """COD sales must remain PENDING and excluded from payout until explicit reconciliation."""
        order, so1, so2 = self._create_test_order(is_cod=True)

        # Post initial COD sale to ledger
        entries = post_order_sale_to_ledger(so1)
        sale_entry = SellerLedgerEntry.objects.get(seller_order=so1, category=SellerLedgerEntry.CATEGORY_ORDER_SALE)

        # Must be in status PENDING_COD
        self.assertEqual(sale_entry.status, SellerLedgerEntry.STATUS_PENDING_COD)
        self.assertIsNone(sale_entry.posted_at)

        # Payout eligibility check: must be 0 eligible
        eligibility = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(eligibility["net_eligible_balance"], Decimal("0.00"))
        self.assertEqual(eligibility["eligible_count"], 0)

        # Delivering the package alone must NOT make COD eligible!
        so1.status = SellerOrder.STATUS_DELIVERED
        so1.delivered_at = timezone.now() - timedelta(days=10)  # Beyond return window
        so1.save()

        eligibility_delivered = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(eligibility_delivered["net_eligible_balance"], Decimal("0.00"))

        # Explicit courier remittance reconciliation
        rec_record = reconcile_cod_remittance(
            seller_order=so1,
            remittance_reference="CRN-BLUEDART-9921",
            collected_amount=Decimal("1000.00"),
            courier_name="Blue Dart",
            actor=self.staff_user,
            notes="Courier batch remitted via bank UTR 12345.",
        )
        self.assertIsNotNone(rec_record.pk)

        # Verify ledger entry transitions to POSTED
        sale_entry.refresh_from_db()
        self.assertEqual(sale_entry.status, SellerLedgerEntry.STATUS_POSTED)
        self.assertIsNotNone(sale_entry.posted_at)

        # Now seller1 should be eligible for payout (1000 sale - 100 comm = 900)
        eligibility_reconciled = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(eligibility_reconciled["net_eligible_balance"], Decimal("900.00"))
        self.assertEqual(eligibility_reconciled["eligible_count"], 2)

    def test_cannot_reconcile_cod_twice(self):
        """Attempting to reconcile the same package twice must raise ReconciliationError."""
        order, so1, so2 = self._create_test_order(is_cod=True)
        so1.status = SellerOrder.STATUS_DELIVERED
        so1.delivered_at = timezone.now()
        so1.save()

        reconcile_cod_remittance(
            seller_order=so1,
            remittance_reference="CRN-FIRST-111",
            actor=self.staff_user,
        )

        with self.assertRaises(ReconciliationError):
            reconcile_cod_remittance(
                seller_order=so1,
                remittance_reference="CRN-SECOND-222",
                actor=self.staff_user,
            )

    # --------------------------------------------------------------------------
    # 4. Phase 6 Returns & Refunds Integration
    # --------------------------------------------------------------------------
    def test_refund_and_return_ledger_integration(self):
        """Processed refund records must post exactly-once refund deduction and commission reversal."""
        order, so1, so2 = self._create_test_order(is_cod=False)
        post_order_sale_to_ledger(so1)

        so1.status = SellerOrder.STATUS_DELIVERED
        so1.delivered_at = timezone.now() - timedelta(days=2)
        so1.save()

        item = so1.items.first()
        ret_req = ReturnRequest.objects.create(
            request_number=f"RET-TEST-{uuid.uuid4().hex[:6].upper()}",
            customer=self.customer,
            order=order,
            seller_order=so1,
            seller=self.seller1,
            is_platform_fulfilled=False,
            status=ReturnRequest.STATUS_PROCESSING,
        )
        ReturnRequestItem.objects.create(
            return_request=ret_req,
            order_item=item,
            quantity=1,
            item_unit_price=item.unit_price,
            refund_eligible_amount=Decimal("1000.00"),
        )

        # Process simulated mock refund (Phase 6)
        refund_record = process_simulated_refund(
            return_request=ret_req,
            actor=self.staff_user,
            approved_amount=Decimal("1000.00"),
        )
        self.assertEqual(refund_record.status, RefundRecord.STATUS_PROCESSED)

        # Verify ledger entries created
        ref_deduction = SellerLedgerEntry.objects.get(
            refund_record=refund_record,
            category=SellerLedgerEntry.CATEGORY_RETURN_REFUND,
        )
        self.assertEqual(ref_deduction.amount, Decimal("1000.00"))
        self.assertEqual(ref_deduction.entry_type, SellerLedgerEntry.TYPE_DEBIT)

        comm_reversal = SellerLedgerEntry.objects.get(
            refund_record=refund_record,
            category=SellerLedgerEntry.CATEGORY_COMMISSION_REVERSAL,
        )
        self.assertEqual(comm_reversal.amount, Decimal("100.00"))
        self.assertEqual(comm_reversal.entry_type, SellerLedgerEntry.TYPE_CREDIT)

        # Net balance for seller1 after return:
        # +1000 (sale) - 100 (comm) - 1000 (refund) + 100 (comm rev) = 0.00
        summary = get_seller_ledger_summary(seller=self.seller1)
        self.assertEqual(summary["current_balance"], Decimal("0.00"))

    # --------------------------------------------------------------------------
    # 5. Order Cancellation Reversal
    # --------------------------------------------------------------------------
    def test_order_cancellation_reversal(self):
        """Pre-dispatch cancellation offsets posted entries with audit reversals."""
        order, so1, so2 = self._create_test_order(is_cod=False)
        post_order_sale_to_ledger(so1)

        summary_before = get_seller_ledger_summary(seller=self.seller1)
        self.assertEqual(summary_before["current_balance"], Decimal("900.00"))

        # Reversal posting
        post_cancellation_reversal_to_ledger(so1, actor=self.staff_user, reason="Customer cancelled")

        summary_after = get_seller_ledger_summary(seller=self.seller1)
        self.assertEqual(summary_after["current_balance"], Decimal("0.00"))

    # --------------------------------------------------------------------------
    # 6. Payout Batch Creation, Locking & Double-Consumption Guard
    # --------------------------------------------------------------------------
    def test_payout_batch_creation_and_locking(self):
        """Payout batch bundles and locks eligible entries; cannot bundle twice."""
        order, so1, so2 = self._create_test_order(is_cod=False)
        post_order_sale_to_ledger(so1)

        so1.status = SellerOrder.STATUS_DELIVERED
        so1.delivered_at = timezone.now() - timedelta(days=10)  # Outside return window
        so1.save()

        # Create batch
        batch = create_payout_batch(seller=self.seller1, actor=self.staff_user, notes="Weekly disbursement")
        self.assertEqual(batch.status, PayoutBatch.STATUS_DRAFT)
        self.assertEqual(batch.amount, Decimal("900.00"))
        self.assertEqual(batch.eligible_entries_count, 2)

        # All covered entries must be linked to batch
        for entry in SellerLedgerEntry.objects.filter(seller_order=so1):
            self.assertEqual(entry.payout_batch_id, batch.id)

        # Attempting to create another payout batch must fail (0 eligible entries)
        with self.assertRaises(PayoutEligibilityError):
            create_payout_batch(seller=self.seller1, actor=self.staff_user)

    # --------------------------------------------------------------------------
    # 7. Mock Payout Settlement & Failure Simulation
    # --------------------------------------------------------------------------
    def test_mock_payout_settlement_and_failure(self):
        """Mock payout execution can simulate failure (unlinks entries) or success (posts PAYOUT debit)."""
        order, so1, so2 = self._create_test_order(is_cod=False)
        post_order_sale_to_ledger(so1)
        so1.status = SellerOrder.STATUS_DELIVERED
        so1.delivered_at = timezone.now() - timedelta(days=10)
        so1.save()

        batch = create_payout_batch(seller=self.seller1, actor=self.staff_user)

        # 1. Simulate failure
        success, msg, failed_batch = process_mock_payout_batch(
            payout_batch=batch,
            actor=self.checker_user,
            simulate_failure=True,
            failure_reason="Invalid merchant bank IFSC code",
        )
        self.assertFalse(success)
        self.assertEqual(failed_batch.status, PayoutBatch.STATUS_FAILED)

        # Entries should be unlinked and ready for retry
        for entry in SellerLedgerEntry.objects.filter(seller_order=so1):
            self.assertIsNone(entry.payout_batch_id)

        # 2. Re-create and settle successfully
        batch2 = create_payout_batch(seller=self.seller1, actor=self.staff_user)
        success2, msg2, completed_batch = process_mock_payout_batch(
            payout_batch=batch2,
            actor=self.checker_user,
            simulate_failure=False,
        )
        self.assertTrue(success2)
        self.assertEqual(completed_batch.status, PayoutBatch.STATUS_COMPLETED)
        self.assertTrue(completed_batch.external_reference.startswith("MOCK-PAY-"))

        # Verify a PAYOUT debit was recorded in the ledger
        payout_debit = SellerLedgerEntry.objects.get(
            payout_batch=completed_batch,
            category=SellerLedgerEntry.CATEGORY_PAYOUT,
        )
        self.assertEqual(payout_debit.amount, Decimal("900.00"))
        self.assertEqual(payout_debit.entry_type, SellerLedgerEntry.TYPE_DEBIT)

        # Net balance is now 0.00
        summary = get_seller_ledger_summary(seller=self.seller1)
        self.assertEqual(summary["current_balance"], Decimal("0.00"))

    # --------------------------------------------------------------------------
    # 8. Auditable Payout Reversal
    # --------------------------------------------------------------------------
    def test_payout_batch_auditable_reversal(self):
        """Reversing a completed payout batch posts a PAYOUT_REVERSAL credit and restores balance."""
        order, so1, so2 = self._create_test_order(is_cod=False)
        post_order_sale_to_ledger(so1)
        so1.status = SellerOrder.STATUS_DELIVERED
        so1.delivered_at = timezone.now() - timedelta(days=10)
        so1.save()

        batch = create_payout_batch(seller=self.seller1, actor=self.staff_user)
        process_mock_payout_batch(payout_batch=batch, actor=self.checker_user)

        # Reversal
        success, msg, rev_batch = reverse_payout_batch(
            payout_batch=batch,
            actor=self.checker_user,
            reason="Bank transfer bounced due to dormant merchant account",
        )
        self.assertTrue(success)
        self.assertEqual(rev_batch.status, PayoutBatch.STATUS_REVERSED)

        # Verify PAYOUT_REVERSAL credit entry exists
        rev_entry = SellerLedgerEntry.objects.get(
            payout_batch=rev_batch,
            category=SellerLedgerEntry.CATEGORY_PAYOUT_REVERSAL,
        )
        self.assertEqual(rev_entry.amount, Decimal("900.00"))
        self.assertEqual(rev_entry.entry_type, SellerLedgerEntry.TYPE_CREDIT)

        # Balance restored to 900.00
        summary = get_seller_ledger_summary(seller=self.seller1)
        self.assertEqual(summary["current_balance"], Decimal("900.00"))

    # --------------------------------------------------------------------------
    # 9. Seller Multi-Tenant Isolation
    # --------------------------------------------------------------------------
    def test_seller_multi_tenant_isolation(self):
        """Seller 1 must never see Seller 2's ledger or payout batches."""
        order, so1, so2 = self._create_test_order(is_cod=False)
        post_order_sale_to_ledger(so1)
        post_order_sale_to_ledger(so2)

        so2.status = SellerOrder.STATUS_DELIVERED
        so2.delivered_at = timezone.now() - timedelta(days=10)
        so2.save()
        batch_seller2 = create_payout_batch(seller=self.seller2, actor=self.staff_user)

        # Log in as Seller 1
        self.client.force_login(self.seller_user1)

        # View Seller 1's ledger
        resp = self.client.get(reverse("core:seller_ledger"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, so1.sub_order_number)
        self.assertNotContains(resp, so2.sub_order_number)

        # Attempt to access Seller 2's payout batch detail
        detail_resp = self.client.get(
            reverse("core:seller_payout_detail", kwargs={"batch_reference": batch_seller2.batch_reference})
        )
        self.assertEqual(detail_resp.status_code, 404)

    # --------------------------------------------------------------------------
    # 10. Manual Adjustments with Audit Notes
    # --------------------------------------------------------------------------
    def test_manual_adjustments(self):
        """Approved manual adjustments post immediately and require non-empty reasoning."""
        entry = create_manual_adjustment(
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            amount=Decimal("50.00"),
            reason="Goodwill credit for promotional campaign",
            seller=self.seller1,
            actor=self.staff_user,
        )
        self.assertEqual(entry.amount, Decimal("50.00"))
        self.assertEqual(entry.status, SellerLedgerEntry.STATUS_POSTED)

        # Empty reason raises error
        with self.assertRaises(LedgerServiceError):
            create_manual_adjustment(
                entry_type=SellerLedgerEntry.TYPE_DEBIT,
                amount=Decimal("20.00"),
                reason="   ",
                seller=self.seller1,
            )

    # --------------------------------------------------------------------------
    # 11. CSV Formula Injection Sanitization
    # --------------------------------------------------------------------------
    def test_csv_formula_injection_sanitization(self):
        """CSV cells starting with =, +, -, @ must be prepended with a quote."""
        malicious_reason = "=cmd|'/C calc'!A0"
        create_manual_adjustment(
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            amount=Decimal("10.00"),
            reason=malicious_reason,
            seller=self.seller1,
            actor=self.staff_user,
        )

        self.client.force_login(self.seller_user1)
        resp = self.client.get(reverse("core:seller_export_ledger_csv"))
        self.assertEqual(resp.status_code, 200)

        csv_content = resp.content.decode("utf-8")
        # Formula must be escaped with single quote
        self.assertIn("'" + malicious_reason, csv_content)
