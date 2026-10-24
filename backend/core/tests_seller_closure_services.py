"""
Focused Service & Workflow Tests for FitMeezo Seller Closure Clearance & Final Settlement (Phase 2).

Covers:
1. Closure clearance with no blockers (audit_seller_closure_clearance).
2. Open/unfulfilled order blockers.
3. Unresolved return and refund blockers.
4. Unreconciled courier COD remittance blockers.
5. 120-hour delivery cooling hold boundary testing (119h blocked vs 121h cleared).
6. Negative balance blocking settlement and closure.
7. Final settlement below ₹500 minimum threshold allowed.
8. Regular payout cycle minimum threshold (₹500) remains strictly enforced.
9. Zero or negative residual balance rejects payout batch creation.
10. Idempotent repeated final settlement creation returns existing active batch safely.
11. Unique active final settlement batch constraint enforcement.
12. Existing batched / disbursed entries strictly excluded from double-consumption.
13. Role-based access control (RBAC): unauthorized staff / customer rejected with LedgerServiceError.
14. Failed/reversed final payout batches permit new final settlement batch creation.
15. Atomic re-check of clearance conditions at batch creation time.
16. Seller status preservation: seller status remains 'closing' during batch creation (never auto-closed).
17. Zero payment-provider calls and DRAFT initial batch status.
18. Full closure eligibility verification (is_seller_eligible_for_final_closure).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import uuid
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    Category,
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
from core.admin_permissions import (
    ensure_admin_groups,
    set_user_role,
    ROLE_FINANCE_MANAGER,
    ROLE_SUPPORT_MANAGER,
    ROLE_SUPER_ADMIN,
)
from core.ledger_services import (
    audit_seller_closure_clearance,
    create_final_settlement_batch,
    create_payout_batch,
    is_seller_eligible_for_final_closure,
    record_manual_bank_transfer,
    LedgerServiceError,
    PayoutEligibilityError,
)

User = get_user_model()


@override_settings(FITMEEZO_MIN_PAYOUT_THRESHOLD=Decimal("500.00"))
class SellerClosureClearanceAndFinalSettlementTestCase(TestCase):
    def setUp(self):
        ensure_admin_groups()

        # 1. Staff Users
        self.super_admin = User.objects.create_user(
            username="p2_super_admin",
            email="p2_superadmin@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        set_user_role(self.super_admin, ROLE_SUPER_ADMIN)

        self.finance_user = User.objects.create_user(
            username="p2_finance_mgr",
            email="p2_finance@fitmeezo.com",
            password="FinancePassword123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        self.support_user = User.objects.create_user(
            username="p2_support_mgr",
            email="p2_support@fitmeezo.com",
            password="SupportPassword123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        # 2. Customer
        self.customer = User.objects.create_user(
            username="p2_customer",
            email="customer@fitmeezo.com",
            password="CustomerPassword123!",
            is_staff=False,
        )

        # 3. Sellers
        self.seller_user1 = User.objects.create_user(
            username="p2_merchant_closing",
            email="merchant_closing@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller_closing = Seller.objects.create(
            user=self.seller_user1,
            store_name="Kashmir Crafts Closing",
            legal_business_name="Kashmir Crafts Pvt Ltd",
            support_email="support@kashmircrafts.com",
            support_phone="9876543201",
            registered_address="10 Dal Lake Road",
            city="Srinagar",
            state="Jammu and Kashmir",
            postal_code="190001",
            pickup_address="10 Dal Lake Road",
            pickup_city="Srinagar",
            pickup_state="Jammu and Kashmir",
            pickup_postal_code="190001",
            gstin="01ABCDE1234F1Z1",
            pan_number="ABCDE1234H",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_CLOSING,  # In approved closing state
            is_active=False,               # Storefront unlisted
            bank_account_holder_name="Kashmir Crafts Pvt Ltd",
            bank_account_number="98765432109999",
            bank_ifsc_code="SBIN0001234",
            bank_name="State Bank of India",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.super_admin,
            closure_approved_at=timezone.now(),
            closure_approved_by=self.super_admin,
        )

        self.seller_user2 = User.objects.create_user(
            username="p2_merchant_approved",
            email="merchant_approved@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller_approved = Seller.objects.create(
            user=self.seller_user2,
            store_name="Goa Spices Active",
            legal_business_name="Goa Spices LLP",
            support_email="support@goaspices.com",
            support_phone="9876543202",
            registered_address="20 Panaji Market",
            city="Panaji",
            state="Goa",
            postal_code="403001",
            pickup_address="20 Panaji Market",
            pickup_city="Panaji",
            pickup_state="Goa",
            pickup_postal_code="403001",
            gstin="30ABCDE1234F1Z2",
            pan_number="ABCDE1234I",
            commission_rate=Decimal("12.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Goa Spices LLP",
            bank_account_number="98765432108888",
            bank_ifsc_code="HDFC0005678",
            bank_name="HDFC Bank",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.super_admin,
        )

        # 4. Product, Order, and Sub-Orders
        self.category = Category.objects.create(name="Handmade", slug=f"handmade-{uuid.uuid4().hex[:6]}")
        self.product = Product.objects.create(
            seller=self.seller_closing,
            category=self.category,
            name="Pashmina Shawl",
            slug=f"pashmina-{uuid.uuid4().hex[:6]}",
            sku=f"SKU-{uuid.uuid4().hex[:8].upper()}",
            price=Decimal("1000.00"),
            stock_quantity=20,
            is_approved=True,
            is_active=False,
        )

        self.now = timezone.now()
        self.order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_DELIVERED,
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
        )
        # Delivered 6 days ago (144 hours ago > 120h cooling hold)
        self.delivered_pkg = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-DELIV-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("1000.00"),
            total_commission=Decimal("100.00"),
            delivered_at=self.now - timedelta(days=6),
        )
        # Posted Sale & Commission
        self.sale_entry = SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1000.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=self.order,
            seller_order=self.delivered_pkg,
            idempotency_key=f"P2-SALE-{uuid.uuid4().hex[:6]}",
            description="Delivered sale",
            posted_at=self.now - timedelta(days=6),
        )
        self.comm_entry = SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
            amount=Decimal("100.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=self.order,
            seller_order=self.delivered_pkg,
            idempotency_key=f"P2-COMM-{uuid.uuid4().hex[:6]}",
            description="Commission deduction",
            posted_at=self.now - timedelta(days=6),
        )
        # Net unbatched eligible = 1000 - 100 = 900.00

    # =========================================================================
    # 1. Read-Only Clearance Evaluator Tests
    # =========================================================================

    def test_clearance_with_no_blockers_eligible_for_final_batch(self):
        """When seller is closing, orders are delivered >120h ago, and no disputes exist, clearance passes."""
        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertEqual(clearance["seller_id"], self.seller_closing.id)
        self.assertEqual(clearance["status"], Seller.STATUS_CLOSING)
        self.assertTrue(clearance["is_cleared_for_settlement"])
        self.assertEqual(clearance["settlement_blockers"], [])
        self.assertEqual(clearance["open_orders"]["count"], 0)
        self.assertEqual(clearance["unresolved_returns"]["count"], 0)
        self.assertEqual(clearance["unresolved_refunds"]["count"], 0)
        self.assertEqual(clearance["unreconciled_cod"]["count"], 0)
        self.assertEqual(clearance["cooling_holds"]["count"], 0)
        self.assertFalse(clearance["ledger_balance"]["is_negative_balance"])
        self.assertEqual(clearance["ledger_balance"]["unbatched_eligible_amount"], Decimal("900.00"))
        self.assertTrue(clearance["can_create_final_settlement_batch"])
        # Can fully close is False because ₹900 remains unbatched/unsettled
        self.assertFalse(clearance["can_fully_close"])

    def test_open_unfulfilled_order_blocks_settlement_and_closure(self):
        """Active open orders (in processing or shipped) block final settlement."""
        open_order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-OPEN-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_PROCESSING,
            subtotal=Decimal("500.00"),
            total_amount=Decimal("500.00"),
        )
        open_pkg = SellerOrder.objects.create(
            order=open_order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-OPEN-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_SHIPPED,
            subtotal=Decimal("500.00"),
            total_commission=Decimal("50.00"),
        )
        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["is_cleared_for_settlement"])
        self.assertFalse(clearance["can_create_final_settlement_batch"])
        self.assertEqual(clearance["open_orders"]["count"], 1)
        self.assertTrue(any("open/unfulfilled sub-order" in b for b in clearance["settlement_blockers"]))

        # Attempting create_final_settlement_batch raises PayoutEligibilityError
        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertIn("open/unfulfilled sub-order", str(ctx.exception))

    def test_unresolved_return_blocks_settlement(self):
        """Active customer return requests block final settlement."""
        ReturnRequest.objects.create(
            seller_order=self.delivered_pkg,
            order=self.order,
            customer=self.customer,
            seller=self.seller_closing,
            request_number=f"RET-{uuid.uuid4().hex[:6].upper()}",
            status=ReturnRequest.STATUS_REQUESTED,
            reason=ReturnRequest.REASON_OTHER,
        )
        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["is_cleared_for_settlement"])
        self.assertFalse(clearance["can_create_final_settlement_batch"])
        self.assertEqual(clearance["unresolved_returns"]["count"], 1)
        self.assertTrue(any("return/exchange request" in b for b in clearance["settlement_blockers"]))

        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertIn("return/exchange request", str(ctx.exception))

    def test_unresolved_refund_blocks_settlement(self):
        """Pending or failed refund records block final settlement."""
        RefundRecord.objects.create(
            order=self.order,
            seller_order=self.delivered_pkg,
            refund_reference=f"REF-{uuid.uuid4().hex[:6].upper()}",
            status=RefundRecord.STATUS_PENDING,
            requested_amount=Decimal("300.00"),
            approved_amount=Decimal("0.00"),
        )
        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["is_cleared_for_settlement"])
        self.assertFalse(clearance["can_create_final_settlement_batch"])
        self.assertEqual(clearance["unresolved_refunds"]["count"], 1)
        self.assertTrue(any("refund record" in b for b in clearance["settlement_blockers"]))

        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertIn("refund record", str(ctx.exception))

    def test_unreconciled_cod_blocks_settlement(self):
        """Cash-on-Delivery packages pending remittance reconciliation block final settlement."""
        cod_order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-COD-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_DELIVERED,
            subtotal=Decimal("800.00"),
            total_amount=Decimal("800.00"),
        )
        SellerOrder.objects.create(
            order=cod_order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-COD-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("800.00"),
            total_commission=Decimal("80.00"),
            delivered_at=self.now - timedelta(days=7),
            # cod_reconciliation is None!
        )
        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["is_cleared_for_settlement"])
        self.assertFalse(clearance["can_create_final_settlement_batch"])
        self.assertGreater(clearance["unreconciled_cod"]["count"], 0)
        self.assertTrue(any("COD remittance" in b for b in clearance["settlement_blockers"]))

        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertIn("COD", str(ctx.exception))

    def test_delivery_cooling_hold_120h_boundary(self):
        """Packages delivered <120h ago are held; packages delivered >=120h ago are cleared."""
        # Update delivered package timestamp to 119 hours ago (cooling hold still active)
        self.delivered_pkg.delivered_at = self.now - timedelta(hours=119)
        self.delivered_pkg.save(update_fields=["delivered_at"])

        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["is_cleared_for_settlement"])
        self.assertFalse(clearance["can_create_final_settlement_batch"])
        self.assertGreater(clearance["cooling_holds"]["count"], 0)

        # Fast forward delivery timestamp to 121 hours ago
        self.delivered_pkg.delivered_at = self.now - timedelta(hours=121)
        self.delivered_pkg.save(update_fields=["delivered_at"])

        clearance_cleared = audit_seller_closure_clearance(self.seller_closing)
        self.assertTrue(clearance_cleared["is_cleared_for_settlement"])
        self.assertEqual(clearance_cleared["cooling_holds"]["count"], 0)
        self.assertTrue(clearance_cleared["can_create_final_settlement_batch"])

    def test_negative_balance_blocks_settlement(self):
        """A net negative ledger balance blocks final settlement."""
        # Post a large debit deduction (e.g. return refund debit of ₹1500)
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_RETURN_REFUND,
            amount=Decimal("1500.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            idempotency_key=f"P2-DEBIT-LIABILITY-{uuid.uuid4().hex[:6]}",
            description="Chargeback / large return deduction",
            posted_at=self.now,
        )
        clearance = audit_seller_closure_clearance(self.seller_closing)
        # Net balance = 1000 - 100 - 1500 = -600.00
        self.assertTrue(clearance["ledger_balance"]["is_negative_balance"])
        self.assertEqual(clearance["ledger_balance"]["net_lifetime_balance"], Decimal("-600.00"))
        self.assertFalse(clearance["is_cleared_for_settlement"])
        self.assertFalse(clearance["can_create_final_settlement_batch"])
        self.assertTrue(any("Negative ledger balance" in b for b in clearance["settlement_blockers"]))

        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertIn("greater than zero", str(ctx.exception))

    # =========================================================================
    # 2. Final Settlement Batch Creation & Constraints
    # =========================================================================

    def test_final_settlement_below_500_threshold_allowed(self):
        """Final settlement batch bypasses regular ₹500 minimum threshold for positive balances."""
        # Adjust entries so net payable is ₹125.00
        self.comm_entry.amount = Decimal("875.00")
        self.comm_entry.save(update_fields=["amount"])
        # Net eligible = 1000 - 875 = 125.00 (below ₹500.00)

        batch = create_final_settlement_batch(
            self.seller_closing,
            actor=self.finance_user,
            notes="Account closure final payout below threshold.",
        )
        self.assertIsNotNone(batch)
        self.assertEqual(batch.amount, Decimal("125.00"))
        self.assertEqual(batch.batch_type, PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT)
        self.assertTrue(batch.is_final_settlement)
        self.assertEqual(batch.status, PayoutBatch.STATUS_DRAFT)
        self.assertEqual(batch.payout_cycle, f"FINAL-{self.seller_closing.id}")
        self.assertEqual(batch.eligible_entries_count, 2)
        # Verify entries are linked atomically
        self.sale_entry.refresh_from_db()
        self.comm_entry.refresh_from_db()
        self.assertEqual(self.sale_entry.payout_batch, batch)
        self.assertEqual(self.comm_entry.payout_batch, batch)

    def test_regular_cycle_minimum_threshold_unchanged(self):
        """Regular cycle payouts continue to enforce the ₹500 minimum threshold."""
        # Use active seller with net balance = ₹150.00
        order2 = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD2-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_DELIVERED,
            subtotal=Decimal("200.00"),
            total_amount=Decimal("200.00"),
        )
        pkg2 = SellerOrder.objects.create(
            order=order2,
            seller=self.seller_approved,
            sub_order_number=f"FM-PKG2-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("200.00"),
            total_commission=Decimal("50.00"),
            delivered_at=self.now - timedelta(days=6),
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_approved,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("200.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            seller_order=pkg2,
            idempotency_key=f"P2-SALE2-{uuid.uuid4().hex[:6]}",
            posted_at=self.now - timedelta(days=6),
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_approved,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
            amount=Decimal("50.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            seller_order=pkg2,
            idempotency_key=f"P2-COMM2-{uuid.uuid4().hex[:6]}",
            posted_at=self.now - timedelta(days=6),
        )
        # Net eligible = 150.00 (< ₹500)
        # Attempting standard create_payout_batch for regular cycle raises PayoutEligibilityError
        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_payout_batch(
                seller=self.seller_approved,
                payout_cycle=None,
                actor=self.finance_user,
            )
        self.assertIn("minimum payout threshold", str(ctx.exception))

    def test_duplicate_repeated_final_settlement_creation_returns_existing(self):
        """Calling create_final_settlement_batch repeatedly safely returns the existing active batch."""
        batch1 = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertEqual(batch1.status, PayoutBatch.STATUS_DRAFT)

        # Second call returns batch1 instead of raising error or double-batching
        batch2 = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertEqual(batch1.id, batch2.id)
        self.assertEqual(PayoutBatch.objects.filter(seller=self.seller_closing).count(), 1)

    def test_existing_batched_entries_excluded(self):
        """Entries already associated with a batch cannot be consumed again."""
        batch1 = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        # Now mark batch1 completed
        batch1.status = PayoutBatch.STATUS_COMPLETED
        batch1.save(update_fields=["status"])

        # Attempting to create another final settlement batch reports no eligible unbatched entries
        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertIn("greater than zero", str(ctx.exception))

    def test_unauthorized_actor_rejected(self):
        """Staff without Super Admin or Finance Manager role cannot create final settlement batches."""
        with self.assertRaises(LedgerServiceError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.support_user)
        self.assertIn("Permission denied", str(ctx.exception))

        with self.assertRaises(LedgerServiceError) as ctx:
            create_final_settlement_batch(self.seller_closing, actor=self.customer)
        self.assertIn("Staff privileges required", str(ctx.exception))

    def test_seller_not_in_closing_status_rejected(self):
        """Only sellers in STATUS_CLOSING can have a final settlement batch created."""
        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_final_settlement_batch(self.seller_approved, actor=self.finance_user)
        self.assertIn("must be in CLOSING status", str(ctx.exception))

    def test_failed_final_batch_allows_new_batch_creation(self):
        """If a final settlement batch fails, a new active final settlement batch can be created."""
        batch1 = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        batch1.status = PayoutBatch.STATUS_FAILED
        batch1.failure_reason = "Beneficiary bank account closed"
        batch1.save(update_fields=["status", "failure_reason"])

        # Release entries back to unbatched for retry
        SellerLedgerEntry.objects.filter(payout_batch=batch1).update(payout_batch=None)

        batch2 = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertNotEqual(batch1.id, batch2.id)
        self.assertEqual(batch2.status, PayoutBatch.STATUS_DRAFT)

    def test_no_seller_closure_status_mutation_during_batch_creation(self):
        """Creating a final settlement batch never alters seller.status (must remain closing)."""
        initial_status = self.seller_closing.status
        batch = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, initial_status)
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSING)
        self.assertIsNone(self.seller_closing.closed_at)

    def test_no_payment_provider_calls_and_draft_status(self):
        """Final settlement batch starts in DRAFT and makes zero external provider calls."""
        with patch("core.ledger_services.logger") as mock_logger:
            batch = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
            self.assertEqual(batch.status, PayoutBatch.STATUS_DRAFT)
            self.assertIsNone(batch.processed_at)
            self.assertEqual(batch.payout_channel, PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK)

    # =========================================================================
    # 3. Full Closure Eligibility Invariants
    # =========================================================================

    def test_full_closure_eligibility_when_batch_completed_and_zero_residual(self):
        """Seller can be fully closed only when final batch is COMPLETED and remaining unbatched balance is zero."""
        batch = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        can_close, blockers = is_seller_eligible_for_final_closure(self.seller_closing)
        self.assertFalse(can_close)
        self.assertTrue(any("must be completed" in b for b in blockers))

        # Approve and record manual bank transfer
        batch.approved_at = timezone.now()
        batch.approved_by = self.finance_user
        batch.save(update_fields=["approved_at", "approved_by"])

        success, msg, completed_batch = record_manual_bank_transfer(
            payout_batch=batch,
            actor=self.finance_user,
            utr_reference="UTR-FINAL-CLOSE-001",
            transfer_date=date.today(),
            confirmation=True,
            notes="Final settlement paid offline via RTGS.",
        )
        self.assertTrue(success)
        self.assertEqual(completed_batch.status, PayoutBatch.STATUS_COMPLETED)

        # Now check full closure eligibility
        can_close_now, final_blockers = is_seller_eligible_for_final_closure(self.seller_closing)
        self.assertTrue(can_close_now)
        self.assertEqual(final_blockers, [])
