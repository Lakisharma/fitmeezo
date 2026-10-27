"""
Fitmeezo Seller Payout Management & Controls Test Suite.

Covers:
1. 7-day delivery cooling period hold
2. Previous-month calculation and monthly cutoff
3. Refunds, returns and COD reconciliation exclusions
4. Bank verification requirements & account masking
5. Hold/unhold and independent seller isolation
6. Server-side permission enforcement & CSRF/POST constraints
7. Duplicate batch prevention & idempotency
8. Repeated approval and duplicate disbursement prevention
9. Ledger balance integrity and auditable reversal
10. Live payouts disabled without approved provider configuration
"""

from datetime import datetime, timedelta
from decimal import Decimal
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    CODReceiptRecord,
    Order,
    OrderItem,
    PayoutAuditLog,
    PayoutBatch,
    Product,
    ProductVariant,
    RefundRecord,
    ReturnRequest,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from core.ledger_services import (
    approve_payout_batch,
    calculate_seller_payout_eligibility,
    create_manual_adjustment,
    create_payout_batch,
    get_cycle_bounds,
    hold_payout_batch,
    post_order_sale_to_ledger,
    process_mock_payout_batch,
    reconcile_cod_remittance,
    reject_payout_batch,
    release_payout_batch,
    reverse_payout_batch,
    LedgerServiceError,
    PayoutEligibilityError,
    DuplicatePostingError,
)
from core.payout_adapters import (
    get_payout_system_status,
    LivePayoutProviderAdapter,
    MockPayoutProviderAdapter,
    PayoutAdapterError,
)

User = get_user_model()


class SellerPayoutManagementTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Staff Admin (Maker)
        self.admin_user = User.objects.create_user(
            username="payout_admin",
            email="admin@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )

        # Staff Admin (Checker / Auditor)
        self.checker_user = User.objects.create_user(
            username="payout_checker",
            email="checker@fitmeezo.com",
            password="CheckerPassword123!",
            is_staff=True,
            is_superuser=True,
        )

        # Non-staff customer
        self.customer_user = User.objects.create_user(
            username="payout_customer",
            email="customer@fitmeezo.com",
            password="CustomerPassword123!",
        )

        # Seller 1 (Verified Bank)
        self.seller_user1 = User.objects.create_user(
            username="seller_verified_user",
            email="seller1@merchant.com",
            password="SellerPassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Royal Fabrics",
            legal_business_name="Royal Fabrics Pvt Ltd",
            support_email="support@royalfabrics.com",
            support_phone="9988776655",
            registered_address="10 Silk Board",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560068",
            pickup_address="10 Silk Board",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560068",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Royal Fabrics Pvt Ltd",
            bank_account_number="12345678901234",
            bank_ifsc_code="HDFC0001234",
            bank_name="HDFC Bank",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.admin_user,
        )

        # Seller 2 (Unverified Bank)
        self.seller_user2 = User.objects.create_user(
            username="seller_unverified_user",
            email="seller2@merchant.com",
            password="SellerPassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Urban Sneakers",
            legal_business_name="Urban Sneakers LLP",
            support_email="support@urbansneakers.com",
            support_phone="9988776656",
            registered_address="20 MG Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="20 MG Road",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z6",
            pan_number="ABCDE1234G",
            commission_rate=Decimal("12.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Urban Sneakers LLP",
            bank_account_number="98765432109876",
            bank_ifsc_code="ICIC0005678",
            bank_name="ICICI Bank",
            bank_verification_status="pending",
        )

        # Category & Product
        self.category = Category.objects.create(
            name="Ethnic Wear",
            slug="ethnic-wear",
            is_active=True,
        )
        self.product = Product.objects.create(
            name="Silk Kurta",
            slug="silk-kurta",
            sku="SK-001",
            seller=self.seller1,
            category=self.category,
            price=Decimal("1000.00"),
            stock_quantity=50,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="L",
            sku="SK-001-L",
            stock_quantity=20,
            is_active=True,
        )

    def _create_delivered_order(
        self,
        seller,
        delivered_days_ago=10,
        subtotal=Decimal("1000.00"),
        payment_method=Order.PAYMENT_METHOD_RAZORPAY,
        created_at_override=None,
        delivered_at_override=None,
    ):
        """Helper to create a delivered order and post its entries to ledger."""
        now = timezone.now()
        if delivered_at_override:
            delivered_time = delivered_at_override
        elif created_at_override:
            # Anchor delivery 2 days after order placement if created_at is overridden
            delivered_time = created_at_override + timedelta(days=2)
        else:
            delivered_time = now - timedelta(days=delivered_days_ago)

        order = Order.objects.create(
            order_number=f"FM-TEST-{uuid.uuid4().hex[:6].upper()}",
            user=self.customer_user,
            shipping_full_name="Test Customer",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Test St",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            payment_method=payment_method,
            payment_status=Order.PAYMENT_STATUS_PAID if payment_method == Order.PAYMENT_METHOD_RAZORPAY else Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_DELIVERED,
            subtotal=subtotal,
            shipping_fee=Decimal("50.00"),
            total_amount=subtotal + Decimal("50.00"),
        )
        if created_at_override:
            Order.objects.filter(id=order.id).update(created_at=created_at_override)
            order.refresh_from_db()

        comm_amount = (subtotal * (seller.commission_rate / Decimal("100.00"))).quantize(Decimal("0.01"))
        net_payable = subtotal - comm_amount
        seller_order = SellerOrder.objects.create(
            order=order,
            seller=seller,
            is_platform_fulfilled=False,
            sub_order_number=f"SO-{uuid.uuid4().hex[:8].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=subtotal,
            total_commission=comm_amount,
            net_seller_payable=net_payable,
            customer_shipping_charge=Decimal("0.00"),
            actual_shipping_cost=Decimal("0.00"),
            shipping_allocated_to_seller=Decimal("0.00"),
            delivered_at=delivered_time,
        )
        if created_at_override or delivered_at_override:
            update_dict = {"delivered_at": delivered_time}
            if created_at_override:
                update_dict["created_at"] = created_at_override
            SellerOrder.objects.filter(id=seller_order.id).update(**update_dict)
            seller_order.refresh_from_db()

        OrderItem.objects.create(
            order=order,
            seller=seller,
            seller_order=seller_order,
            product=self.product,
            product_name="Silk Kurta",
            sku="SK-001-L",
            unit_price=subtotal,
            quantity=1,
            line_total=subtotal,
            commission_rate=seller.commission_rate,
            commission_amount=comm_amount,
        )

        entries = post_order_sale_to_ledger(seller_order=seller_order)
        if created_at_override:
            SellerLedgerEntry.objects.filter(seller_order=seller_order).update(
                created_at=created_at_override,
                posted_at=created_at_override,
            )

        return order, seller_order, entries

    # ==========================================================================
    # 1. 7-Day Delivery Cooling Period Hold
    # ==========================================================================
    def test_seven_day_delivery_cooling_hold(self):
        """
        Orders delivered less than 7 days ago must be kept on hold.
        Orders delivered 7 or more days ago become payout-eligible.
        """
        # Order 1: Delivered 3 days ago (cooling period active)
        _, pkg_recent, _ = self._create_delivered_order(self.seller1, delivered_days_ago=3)
        # Order 2: Delivered 10 days ago (matured)
        _, pkg_matured, _ = self._create_delivered_order(self.seller1, delivered_days_ago=10)

        eligibility = calculate_seller_payout_eligibility(seller=self.seller1)

        # Recent package entries must be held with cooling explanation
        held_reasons = [h["reason"] for h in eligibility["held_entry_details"]]
        self.assertTrue(any("cooling period active" in r for r in held_reasons))
        self.assertGreater(eligibility["held_count"], 0)

        # Matured package entries must be eligible
        self.assertEqual(eligibility["eligible_sales"], Decimal("1000.00"))
        self.assertEqual(eligibility["eligible_commission"], Decimal("100.00"))
        self.assertEqual(eligibility["net_eligible_balance"], Decimal("900.00"))

    # ==========================================================================
    # 2. Previous-Month Calculation and Monthly Cutoff
    # ==========================================================================
    def test_previous_month_calculation_and_monthly_cutoff(self):
        """
        Monthly cycle on 5th calculates earnings strictly from the previous calendar month.
        Transactions created in the current month are carried forward.
        """
        # Compute cycle bounds for an explicit cycle e.g. "2026-10"
        canonical_cycle, cycle_payout_date, prev_start, prev_end = get_cycle_bounds(cycle_str="2026-10")
        self.assertEqual(canonical_cycle, "2026-10")
        self.assertEqual(prev_start.month, 9)
        self.assertEqual(prev_start.year, 2026)
        self.assertEqual(prev_end.month, 9)

        # Order in Sept 2026 (previous month)
        sept_date = prev_start + timedelta(days=10)
        self._create_delivered_order(
            self.seller1,
            delivered_days_ago=25,
            created_at_override=sept_date,
        )

        # Order in Oct 2026 (current month)
        oct_date = cycle_payout_date + timedelta(days=1)
        self._create_delivered_order(
            self.seller1,
            delivered_days_ago=10,
            created_at_override=oct_date,
        )

        # Evaluate cycle 2026-10
        elig = calculate_seller_payout_eligibility(
            seller=self.seller1,
            cycle_month="2026-10",
            as_of_date=cycle_payout_date,
        )

        # Only the Sept order should be eligible
        self.assertEqual(elig["eligible_sales"], Decimal("1000.00"))
        # Oct order entries should be held with carry forward explanation
        held_reasons = [h["reason"] for h in elig["held_entry_details"]]
        self.assertTrue(any("carried forward" in r for r in held_reasons))

    # ==========================================================================
    # 3. Refunds, Returns and COD Reconciliation Exclusion
    # ==========================================================================
    def test_unresolved_return_and_refund_exclusion(self):
        """
        Packages with active unresolved return requests or pending refunds are excluded.
        """
        _, pkg, entries = self._create_delivered_order(self.seller1, delivered_days_ago=10)

        # Create active ReturnRequest
        ReturnRequest.objects.create(
            request_number=f"RET-TEST-{uuid.uuid4().hex[:6].upper()}",
            order=pkg.order,
            seller_order=pkg,
            customer=self.customer_user,
            request_type=ReturnRequest.TYPE_RETURN,
            status=ReturnRequest.STATUS_REQUESTED,  # unresolved
            reason=ReturnRequest.REASON_DEFECTIVE,
        )

        elig = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(elig["eligible_sales"], Decimal("0.00"))
        held_reasons = [h["reason"] for h in elig["held_entry_details"]]
        self.assertTrue(any("return or exchange request unresolved" in r for r in held_reasons))

    def test_unreconciled_cod_remittance_exclusion_and_reconciliation(self):
        """
        COD entries remain PENDING_COD and are excluded until explicitly reconciled.
        """
        _, pkg, entries = self._create_delivered_order(
            self.seller1,
            delivered_days_ago=10,
            payment_method=Order.PAYMENT_METHOD_COD,
        )

        # Entries start in PENDING_COD
        self.assertTrue(all(e.status == SellerLedgerEntry.STATUS_PENDING_COD for e in entries))

        # Eligibility check: excluded
        elig_before = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(elig_before["net_eligible_balance"], Decimal("0.00"))

        # Reconcile COD remittance
        receipt = reconcile_cod_remittance(
            seller_order=pkg,
            remittance_reference="UTR-TEST-123456",
            collected_amount=pkg.subtotal,
            courier_name="Shiprocket",
            actor=self.admin_user,
        )

        # After reconciliation, entries are POSTED and become eligible
        updated_entries = SellerLedgerEntry.objects.filter(seller_order=pkg)
        self.assertTrue(all(e.status == SellerLedgerEntry.STATUS_POSTED for e in updated_entries))
        elig_after = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(elig_after["eligible_sales"], Decimal("1000.00"))
        self.assertEqual(elig_after["net_eligible_balance"], Decimal("900.00"))

    # ==========================================================================
    # 4. Bank Verification Requirements & Masking
    # ==========================================================================
    def test_bank_verification_requirements_and_masking(self):
        """
        Unverified seller is ineligible for payout batch creation when bank enforcement is on.
        Account number must be masked.
        """
        # Seller 2 is pending verification
        self.assertFalse(self.seller2.is_bank_payout_eligible)
        self.assertTrue(self.seller2.get_masked_account_number().endswith("9876"))
        self.assertNotIn("109876", self.seller2.get_masked_account_number())

        self._create_delivered_order(self.seller2, delivered_days_ago=10)

        # Attempting to create payout batch with bank enforcement raises PayoutEligibilityError
        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_payout_batch(
                seller=self.seller2,
                enforce_bank_verification=True,
                actor=self.admin_user,
            )
        self.assertIn("verified bank account", str(ctx.exception).lower())

        # Seller 1 has verified bank
        self.assertTrue(self.seller1.is_bank_payout_eligible)
        self.assertTrue(self.seller1.get_masked_account_number().endswith("1234"))

    # ==========================================================================
    # 5. Hold/Unhold and Independent Seller Isolation
    # ==========================================================================
    def test_hold_unhold_and_independent_seller_isolation(self):
        """
        Admin can place Seller 1 payout batch on hold with a mandatory reason.
        Seller 2 can still be processed without being blocked by Seller 1.
        """
        self._create_delivered_order(self.seller1, delivered_days_ago=10)

        # Verify Seller 2 bank so it can have a batch
        self.seller2.bank_verification_status = "verified"
        self.seller2.save()
        self._create_delivered_order(self.seller2, delivered_days_ago=10)

        # Create batches for both sellers
        batch1 = create_payout_batch(seller=self.seller1, actor=self.admin_user, enforce_bank_verification=True)
        batch2 = create_payout_batch(seller=self.seller2, actor=self.admin_user, enforce_bank_verification=True)

        # Place batch 1 on hold
        _, _, held_batch1 = hold_payout_batch(
            payout_batch=batch1,
            actor=self.admin_user,
            reason="Auditing customer disputes",
        )
        self.assertEqual(held_batch1.status, PayoutBatch.STATUS_ON_HOLD)
        self.assertEqual(held_batch1.hold_reason, "Auditing customer disputes")
        self.assertEqual(held_batch1.held_by, self.admin_user)

        # Held batch cannot be disbursed
        with self.assertRaises(LedgerServiceError):
            process_mock_payout_batch(payout_batch=held_batch1, actor=self.admin_user)

        # Batch 2 is unaffected and can be approved and disbursed
        _, _, approved_batch2 = approve_payout_batch(payout_batch=batch2, actor=self.checker_user)
        self.assertIsNotNone(approved_batch2.approved_at)

        success, msg, completed_batch2 = process_mock_payout_batch(
            payout_batch=approved_batch2,
            actor=self.checker_user,
        )
        self.assertTrue(success)
        self.assertEqual(completed_batch2.status, PayoutBatch.STATUS_COMPLETED)

        # Release hold on batch 1
        _, _, released_batch1 = release_payout_batch(
            payout_batch=held_batch1,
            actor=self.admin_user,
            notes="Dispute cleared",
        )
        self.assertEqual(released_batch1.status, PayoutBatch.STATUS_DRAFT)
        self.assertIsNotNone(released_batch1.released_at)
        self.assertEqual(released_batch1.released_by, self.admin_user)

        # Audit logs recorded for all actions
        logs = PayoutAuditLog.objects.filter(payout_batch=batch1).order_by("created_at")
        actions = [log.action for log in logs]
        self.assertIn(PayoutAuditLog.ACTION_CREATED, actions)
        self.assertIn(PayoutAuditLog.ACTION_HELD, actions)
        self.assertIn(PayoutAuditLog.ACTION_RELEASED, actions)

    # ==========================================================================
    # 6. Permission Enforcement & CSRF / POST Constraints
    # ==========================================================================
    def test_permission_enforcement_and_csrf_constraints(self):
        """
        Unauthenticated requests redirect to custom admin login.
        Non-staff customer requests receive 403 Forbidden.
        State-changing actions require POST.
        """
        dashboard_url = reverse("custom_admin:seller_payout_management")

        # 1. Unauthenticated GET -> redirect to custom_admin:login
        res_anon = self.client.get(dashboard_url)
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn(reverse("custom_admin:login"), res_anon.url)

        # 2. Customer GET -> 403 Forbidden
        self.client.force_login(self.customer_user)
        res_cust = self.client.get(dashboard_url)
        self.assertEqual(res_cust.status_code, 403)

        # 3. Staff Admin GET -> 200 OK
        self.client.force_login(self.admin_user)
        res_admin = self.client.get(dashboard_url)
        self.assertEqual(res_admin.status_code, 200)

        # 4. State-changing actions require POST (GET returns 405)
        self._create_delivered_order(self.seller1, delivered_days_ago=10)
        batch = create_payout_batch(seller=self.seller1, actor=self.admin_user)

        approve_url = reverse("custom_admin:payout_approve", kwargs={"batch_reference": batch.batch_reference})
        res_get_approve = self.client.get(approve_url)
        self.assertEqual(res_get_approve.status_code, 405)

        hold_url = reverse("custom_admin:payout_hold", kwargs={"batch_reference": batch.batch_reference})
        res_get_hold = self.client.get(hold_url)
        self.assertEqual(res_get_hold.status_code, 405)

    # ==========================================================================
    # 7. Duplicate Batch Prevention & Idempotency
    # ==========================================================================
    def test_duplicate_batch_prevention_idempotency(self):
        """
        Attempting to create duplicate active batches for the same seller in the same cycle
        is blocked by uniqueness constraints and service validation.
        """
        sept_date = timezone.make_aware(datetime(2026, 9, 15, 12, 0, 0))
        self._create_delivered_order(
            self.seller1,
            delivered_days_ago=25,
            created_at_override=sept_date,
        )

        # Create first batch for cycle 2026-10
        batch1 = create_payout_batch(
            seller=self.seller1,
            payout_cycle="2026-10",
            actor=self.admin_user,
        )
        self.assertEqual(batch1.payout_cycle, "2026-10")

        # Attempt to create second batch for the same seller and same cycle
        with self.assertRaises(DuplicatePostingError) as ctx:
            create_payout_batch(
                seller=self.seller1,
                payout_cycle="2026-10",
                actor=self.admin_user,
            )
        self.assertIn("already exists", str(ctx.exception))

    # ==========================================================================
    # 8. Repeated Approval and Duplicate Disbursement Prevention
    # ==========================================================================
    def test_repeated_approval_and_duplicate_disbursement_prevention(self):
        """
        Repeated approval on an already approved batch is idempotent.
        Disbursement on a completed batch prevents duplicate payouts.
        """
        self._create_delivered_order(self.seller1, delivered_days_ago=10)
        batch = create_payout_batch(seller=self.seller1, actor=self.admin_user)

        # First approval
        _, _, approved = approve_payout_batch(payout_batch=batch, actor=self.checker_user)
        self.assertIsNotNone(approved.approved_at)

        # Second approval is idempotent and safe
        ok2, msg2, approved2 = approve_payout_batch(payout_batch=approved, actor=self.checker_user)
        self.assertTrue(ok2)
        self.assertIn("already approved", msg2.lower())

        # Settle batch
        success, msg, completed = process_mock_payout_batch(payout_batch=approved, actor=self.checker_user)
        self.assertTrue(success)
        self.assertEqual(completed.status, PayoutBatch.STATUS_COMPLETED)

        # Duplicate settlement execution attempt on already completed batch
        success_dup, msg_dup, _ = process_mock_payout_batch(payout_batch=completed, actor=self.checker_user)
        self.assertTrue(success_dup)
        self.assertIn("already completed", msg_dup.lower())

    # ==========================================================================
    # 9. Ledger Balance Integrity & Reversal
    # ==========================================================================
    def test_ledger_balance_integrity_and_reversal(self):
        """
        Double-entry integrity: GMV (sales) + shipping - commission - debits = net payable.
        Reversing a completed payout credits the amount back with PAYOUT_REVERSAL.
        """
        self._create_delivered_order(self.seller1, delivered_days_ago=10, subtotal=Decimal("2000.00"))

        # Manual adjustment debit of ₹200
        create_manual_adjustment(
            seller=self.seller1,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            amount=Decimal("200.00"),
            reason="Platform packaging charges",
            actor=self.admin_user,
        )

        elig = calculate_seller_payout_eligibility(seller=self.seller1)
        # Sales: 2000, Commission: 200, Adjustment: 200 -> Net: 1600
        self.assertEqual(elig["eligible_sales"], Decimal("2000.00"))
        self.assertEqual(elig["eligible_commission"], Decimal("200.00"))
        self.assertEqual(elig["net_eligible_balance"], Decimal("1600.00"))

        # Create, approve, and disburse
        batch = create_payout_batch(seller=self.seller1, actor=self.admin_user)
        _, _, approved = approve_payout_batch(payout_batch=batch, actor=self.checker_user)
        success, _, completed = process_mock_payout_batch(payout_batch=approved, actor=self.checker_user)
        self.assertTrue(success)

        # Ledger after payout has a PAYOUT debit entry
        payout_debits = SellerLedgerEntry.objects.filter(
            seller=self.seller1,
            category=SellerLedgerEntry.CATEGORY_PAYOUT,
        )
        self.assertEqual(payout_debits.count(), 1)
        self.assertEqual(payout_debits.first().amount, Decimal("1600.00"))

        # Reverse payout batch
        success_rev, _, reversed_batch = reverse_payout_batch(
            payout_batch=completed,
            actor=self.admin_user,
            reason="Bank returned funds due to IFSC merge",
        )
        self.assertTrue(success_rev)
        self.assertEqual(reversed_batch.status, PayoutBatch.STATUS_REVERSED)

        # Ledger now has a PAYOUT_REVERSAL credit entry
        reversal_credits = SellerLedgerEntry.objects.filter(
            seller=self.seller1,
            category=SellerLedgerEntry.CATEGORY_PAYOUT_REVERSAL,
        )
        self.assertEqual(reversal_credits.count(), 1)
        self.assertEqual(reversal_credits.first().amount, Decimal("1600.00"))

    # ==========================================================================
    # 10. Live Payouts Disabled Without Provider Configuration
    # ==========================================================================
    def test_live_payouts_disabled_without_provider_configuration(self):
        """
        System displays 'Live payouts not configured'.
        Live provider raises error if live credentials/config are disabled.
        Mock provider marks status as simulated_sandbox and labels outputs clearly.
        """
        status_info = get_payout_system_status()
        self.assertFalse(status_info["is_live_configured"])
        self.assertEqual(status_info["status_label"], "Live payouts not configured")

        # Live provider adapter throws exception when initiate_payout is called without active config
        live_adapter = LivePayoutProviderAdapter()
        with self.assertRaises(PayoutAdapterError) as ctx:
            live_adapter.initiate_payout(batch=None)
        self.assertIn("disabled", str(ctx.exception).lower())

        # Mock adapter labels all transactions as simulated
        self._create_delivered_order(self.seller1, delivered_days_ago=10)
        batch = create_payout_batch(seller=self.seller1, actor=self.admin_user)
        approve_payout_batch(payout_batch=batch, actor=self.checker_user)

        mock_adapter = MockPayoutProviderAdapter()
        success, message, data = mock_adapter.initiate_payout(batch=batch, actor=self.admin_user)
        self.assertTrue(success)
        self.assertIn("[TEST/SIMULATED]", message)
        batch.refresh_from_db()
        self.assertEqual(batch.provider_status, "simulated_sandbox")


# =============================================================================
# 11. Seller Payout Review & History View Tests
# =============================================================================
class SellerPayoutReviewAndHistoryViewTests(TestCase):
    """
    Tests for admin_seller_payout_review_view and admin_seller_payout_history_view.

    Covers:
    - Valid seller lookup by Seller pk returns 200 OK
    - Nonexistent seller pk returns 404
    - Unauthenticated access redirects to custom admin login
    - Non-staff access is denied with 403
    - Dashboard table generates review/history URLs using actual Seller pk
    - The named URL 'seller_payout_review' resolves to the correct pattern
    - The named URL 'seller_payout_history' resolves to the correct pattern
    """

    def setUp(self):
        self.client = Client()

        # Admin user
        self.admin_user = User.objects.create_user(
            username="review_admin",
            email="review_admin@fitmeezo.com",
            password="AdminReviewPass123!",
            is_staff=True,
            is_superuser=True,
        )

        # Non-staff customer
        self.customer_user = User.objects.create_user(
            username="review_customer",
            email="review_customer@fitmeezo.com",
            password="CustomerReviewPass123!",
        )

        # Seller under test
        self.seller_user = User.objects.create_user(
            username="review_seller_user",
            email="review_seller@merchant.com",
            password="SellerReviewPass123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Review Test Store",
            legal_business_name="Review Test Pvt Ltd",
            support_email="support@reviewtest.com",
            support_phone="9876500001",
            registered_address="1 Review Lane",
            city="Mumbai",
            state="Maharashtra",
            postal_code="400001",
            pickup_address="1 Review Lane",
            pickup_city="Mumbai",
            pickup_state="Maharashtra",
            pickup_postal_code="400001",
            gstin="27ABCDE9999F1Z1",
            pan_number="ABCDE9999F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Review Test Pvt Ltd",
            bank_account_number="11112222333344",
            bank_ifsc_code="SBIN0012345",
            bank_name="SBI",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.admin_user,
        )

    # ------------------------------------------------------------------
    # Review view — auth & permissions
    # ------------------------------------------------------------------
    def test_review_view_unauthenticated_redirects_to_login(self):
        """Unauthenticated GET on review page redirects to custom admin login."""
        url = reverse("custom_admin:seller_payout_review", kwargs={"seller_id": self.seller.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("custom_admin:login"), response.url)

    def test_review_view_non_staff_returns_403(self):
        """Non-staff user receives 403 Forbidden on review page."""
        self.client.force_login(self.customer_user)
        url = reverse("custom_admin:seller_payout_review", kwargs={"seller_id": self.seller.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_review_view_valid_seller_pk_returns_200(self):
        """Admin accessing review page with correct Seller pk receives 200 OK."""
        self.client.force_login(self.admin_user)
        url = reverse("custom_admin:seller_payout_review", kwargs={"seller_id": self.seller.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review Test Store")

    def test_review_view_nonexistent_seller_pk_returns_404(self):
        """Admin accessing review page with a nonexistent Seller pk receives 404."""
        self.client.force_login(self.admin_user)
        nonexistent_pk = 999999
        # Confirm it really does not exist
        self.assertFalse(Seller.objects.filter(pk=nonexistent_pk).exists())
        url = reverse("custom_admin:seller_payout_review", kwargs={"seller_id": nonexistent_pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_review_view_pk_1_returns_404_when_no_seller_with_pk_1(self):
        """
        Regression: /payout-management/review/1/ returns 404 when Seller pk=1
        does not exist. The fix is to use actual seller pk from the dashboard.
        """
        if Seller.objects.filter(pk=1).exists():
            self.skipTest("Seller pk=1 exists in test DB; skip regression check")
        self.client.force_login(self.admin_user)
        url = reverse("custom_admin:seller_payout_review", kwargs={"seller_id": 1})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------------
    # History view — auth & permissions
    # ------------------------------------------------------------------
    def test_history_view_unauthenticated_redirects_to_login(self):
        """Unauthenticated GET on history page redirects to custom admin login."""
        url = reverse("custom_admin:seller_payout_history", kwargs={"seller_id": self.seller.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("custom_admin:login"), response.url)

    def test_history_view_non_staff_returns_403(self):
        """Non-staff user receives 403 Forbidden on history page."""
        self.client.force_login(self.customer_user)
        url = reverse("custom_admin:seller_payout_history", kwargs={"seller_id": self.seller.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_history_view_valid_seller_pk_returns_200(self):
        """Admin accessing history page with correct Seller pk receives 200 OK."""
        self.client.force_login(self.admin_user)
        url = reverse("custom_admin:seller_payout_history", kwargs={"seller_id": self.seller.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review Test Store")

    def test_history_view_nonexistent_seller_pk_returns_404(self):
        """Admin accessing history page with a nonexistent Seller pk receives 404."""
        self.client.force_login(self.admin_user)
        nonexistent_pk = 999998
        self.assertFalse(Seller.objects.filter(pk=nonexistent_pk).exists())
        url = reverse("custom_admin:seller_payout_history", kwargs={"seller_id": nonexistent_pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_history_view_pk_1_returns_404_when_no_seller_with_pk_1(self):
        """
        Regression: /payout-management/seller/1/history/ returns 404 when Seller
        pk=1 does not exist.
        """
        if Seller.objects.filter(pk=1).exists():
            self.skipTest("Seller pk=1 exists in test DB; skip regression check")
        self.client.force_login(self.admin_user)
        url = reverse("custom_admin:seller_payout_history", kwargs={"seller_id": 1})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    # ------------------------------------------------------------------
    # URL resolution and dashboard link correctness
    # ------------------------------------------------------------------
    def test_named_review_url_resolves_with_seller_pk(self):
        """
        The named URL 'seller_payout_review' is bound to the Seller pk.
        The resolved URL must contain the actual seller pk, not a hardcoded constant.
        """
        url = reverse("custom_admin:seller_payout_review", kwargs={"seller_id": self.seller.pk})
        self.assertIn(str(self.seller.pk), url)
        self.assertNotIn("/1/", url if self.seller.pk != 1 else "/999999/")

    def test_named_history_url_resolves_with_seller_pk(self):
        """
        The named URL 'seller_payout_history' is bound to the Seller pk.
        """
        url = reverse("custom_admin:seller_payout_history", kwargs={"seller_id": self.seller.pk})
        self.assertIn(str(self.seller.pk), url)

    def test_dashboard_links_use_actual_seller_pk(self):
        """
        The payout management dashboard table generates review/history links
        using the actual Seller pk so that the URLs are always valid.
        Accessing the dashboard as admin must return 200.
        """
        self.client.force_login(self.admin_user)
        dashboard_url = reverse("custom_admin:seller_payout_management")
        response = self.client.get(dashboard_url)
        self.assertEqual(response.status_code, 200)

        # The response HTML must contain the correct review URL for our seller
        expected_review_url = reverse(
            "custom_admin:seller_payout_review",
            kwargs={"seller_id": self.seller.pk},
        )
        expected_history_url = reverse(
            "custom_admin:seller_payout_history",
            kwargs={"seller_id": self.seller.pk},
        )
        # Seller is active and should appear in the dashboard table
        self.assertContains(response, expected_review_url)
        self.assertContains(response, expected_history_url)

    def test_payout_management_empty_state_when_no_active_sellers(self):
        """
        When no active sellers exist, the dashboard shows the empty state message
        rather than a 404 or server error.
        """
        # Deactivate our seller
        self.seller.is_active = False
        self.seller.save()

        self.client.force_login(self.admin_user)
        dashboard_url = reverse("custom_admin:seller_payout_management")
        response = self.client.get(dashboard_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No sellers found")
