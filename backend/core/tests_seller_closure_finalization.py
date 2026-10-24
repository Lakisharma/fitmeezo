"""
Fitmeezo Seller Account Closure & Final Settlement Phase 5 Tests.

Tests the authorized admin final-closure action, pessimistic row locking,
re-evaluation of final clearance checks inside transaction.atomic(),
invariants (orders, returns, refunds, COD, 120h cooling holds, negative balance,
unbatched residuals, active batches), product delisting, durable audit logging,
failsafe email dispatch, and historical read-only access.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.admin_permissions import (
    ROLE_SUPER_ADMIN,
    ROLE_FINANCE_MANAGER,
    ROLE_SUPPORT_MANAGER,
    ensure_admin_groups,
    set_user_role,
)
from core.ledger_services import (
    audit_seller_closure_clearance,
    create_final_settlement_batch,
    finalize_seller_account_closure,
    is_seller_eligible_for_final_closure,
    process_mock_payout_batch,
    record_manual_bank_transfer,
    LedgerServiceError,
    PayoutEligibilityError,
)
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

User = get_user_model()


class BaseSellerClosureFinalizationTestCase(TestCase):
    """Common test fixture setting up users, roles, seller in CLOSING status, and catalog items."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        ensure_admin_groups()

        # 1. Staff users with roles
        self.super_admin = User.objects.create_user(
            username="super_admin_test",
            email="superadmin@fitmeezo.com",
            password="SecurePassword123!",
            is_staff=True,
            is_superuser=True,
        )
        set_user_role(self.super_admin, ROLE_SUPER_ADMIN)

        self.finance_user = User.objects.create_user(
            username="finance_mgr_test",
            email="finance@fitmeezo.com",
            password="SecurePassword123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        self.support_user = User.objects.create_user(
            username="support_rep_test",
            email="support@fitmeezo.com",
            password="SecurePassword123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        # 2. Seller users
        self.seller_user = User.objects.create_user(
            username="closing_vendor",
            email="vendor@closingcrafts.in",
            password="VendorPassword123!",
        )

        self.other_seller_user = User.objects.create_user(
            username="active_vendor",
            email="vendor@activecrafts.in",
            password="VendorPassword123!",
        )

        self.customer = User.objects.create_user(
            username="customer_buyer",
            email="buyer@example.com",
            password="CustomerPassword123!",
        )

        # 3. Seller Profile in STATUS_CLOSING
        self.seller_closing = Seller.objects.create(
            user=self.seller_user,
            store_name="Jaipur Textiles Runoff",
            legal_business_name="Jaipur Textiles Private Limited",
            support_email="support@jaipurtextiles.in",
            support_phone="9876543210",
            registered_address="12 Johari Bazar",
            city="Jaipur",
            state="Rajasthan",
            postal_code="302003",
            pickup_address="12 Johari Bazar",
            pickup_city="Jaipur",
            pickup_state="Rajasthan",
            pickup_postal_code="302003",
            gstin="08ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_CLOSING,
            is_active=True,
            bank_account_holder_name="Jaipur Textiles Pvt Ltd",
            bank_account_number="12345678901234",
            bank_ifsc_code="HDFC0001234",
            bank_name="HDFC Bank",
            bank_verification_status="verified",
            bank_verified_at=self.now,
            bank_verified_by=self.super_admin,
            closure_requested_at=self.now - timedelta(days=10),
            closure_request_reason="Transitioning to local retail storefront only.",
            closure_approved_at=self.now - timedelta(days=9),
            closure_approved_by=self.super_admin,
            closure_notes="Approved for runoff settlement.",
        )

        # 4. Catalog & Products
        self.category = Category.objects.create(
            name="Textiles",
            slug=f"textiles-{uuid.uuid4().hex[:6]}",
        )
        self.product1 = Product.objects.create(
            seller=self.seller_closing,
            category=self.category,
            name="Block Print Bedsheet",
            slug=f"bedsheet-{uuid.uuid4().hex[:6]}",
            sku=f"SKU-{uuid.uuid4().hex[:8].upper()}",
            price=Decimal("1000.00"),
            stock_quantity=15,
            is_approved=True,
            is_active=True,
        )
        self.product2 = Product.objects.create(
            seller=self.seller_closing,
            category=self.category,
            name="Cotton Quilt",
            slug=f"quilt-{uuid.uuid4().hex[:6]}",
            sku=f"SKU-{uuid.uuid4().hex[:8].upper()}",
            price=Decimal("2500.00"),
            stock_quantity=5,
            is_approved=True,
            is_active=True,
        )

    def _create_payable_final_settlement_batch(self, amount=Decimal("540.00")):
        """Helper to create delivered orders, valid ledger entries, and generate an active final settlement batch."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-SETTLE-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_DELIVERED,
            subtotal=amount + Decimal("60.00"),
            total_amount=amount + Decimal("60.00"),
        )
        SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-SETTLE-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=amount + Decimal("60.00"),
            total_commission=Decimal("60.00"),
            delivered_at=self.now - timedelta(days=7),
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=amount + Decimal("60.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            order=order,
            idempotency_key=f"SALE-SETTLE-{uuid.uuid4().hex[:8]}",
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
            amount=Decimal("60.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            order=order,
            idempotency_key=f"COMM-SETTLE-{uuid.uuid4().hex[:8]}",
        )
        return create_final_settlement_batch(self.seller_closing, actor=self.finance_user)


class SellerClosureFinalizationServiceTests(BaseSellerClosureFinalizationTestCase):
    """Unit tests evaluating finalize_seller_account_closure() service invariants."""

    def test_successful_closure_after_full_clearance_and_completed_final_settlement(self):
        """
        When seller is in STATUS_CLOSING, delivered orders have cleared the 120h cooling hold,
        final settlement batch is created and marked COMPLETED via manual bank transfer,
        and all ledger entries balance to ₹0.00, account closure finalizes successfully.
        """
        # Create delivered order >120 hours ago
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_DELIVERED,
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
        )
        SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("1000.00"),
            total_commission=Decimal("100.00"),
            delivered_at=self.now - timedelta(days=6),
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1000.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=order,
            idempotency_key=f"SALE-{order.id}",
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
            amount=Decimal("100.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=order,
            idempotency_key=f"COMM-{order.id}",
        )

        # Create final settlement batch (₹900)
        batch = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertEqual(batch.amount, Decimal("900.00"))

        # Approve and record external manual bank transfer
        batch.approved_at = self.now
        batch.approved_by = self.finance_user
        batch.save(update_fields=["approved_at", "approved_by"])

        success, msg, completed_batch = record_manual_bank_transfer(
            payout_batch=batch,
            actor=self.finance_user,
            utr_reference=f"UTR-CLOSE-{uuid.uuid4().hex[:8].upper()}",
            transfer_date=date.today(),
            confirmation=True,
            notes="Final settlement paid via NEFT.",
        )
        self.assertTrue(success)
        self.assertEqual(completed_batch.status, PayoutBatch.STATUS_COMPLETED)

        # Ensure clearance check verifies full closure readiness
        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertTrue(clearance["can_fully_close"])
        self.assertEqual(clearance["ledger_balance"]["net_lifetime_balance"], Decimal("0.00"))
        self.assertEqual(clearance["ledger_balance"]["unbatched_residual_net"], Decimal("0.00"))

        # Finalize seller account closure
        closed_seller = finalize_seller_account_closure(
            seller=self.seller_closing,
            actor=self.finance_user,
            notes="All accounts settled, bank transfer verified via UTR.",
            confirm=True,
        )

        # Assert status mutations
        self.assertEqual(closed_seller.status, Seller.STATUS_CLOSED)
        self.assertFalse(closed_seller.is_active)
        self.assertIsNotNone(closed_seller.closed_at)
        self.assertEqual(closed_seller.closed_by, self.finance_user)
        self.assertIn("ACCOUNT CLOSED by finance_mgr_test", closed_seller.closure_notes)
        self.assertTrue(closed_seller.is_closed)
        self.assertFalse(closed_seller.is_closing)

        # Assert products were delisted
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertFalse(self.product1.is_active)
        self.assertFalse(self.product2.is_active)

        # Assert administrative LogEntry was created
        log_entry = LogEntry.objects.filter(
            object_id=str(self.seller_closing.id),
            action_flag=CHANGE,
        ).order_by("-action_time").first()
        self.assertIsNotNone(log_entry)
        self.assertIn("Finalized seller account closure", log_entry.change_message)
        self.assertEqual(log_entry.user, self.finance_user)

    def test_successful_closure_zero_balance_no_sales(self):
        """
        A merchant in CLOSING status who had zero sales and zero ledger balance (net ₹0.00)
        can be finalized directly without requiring a final settlement payout batch.
        """
        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertTrue(clearance["can_fully_close"])
        self.assertIsNone(clearance["existing_final_batch"])

        closed_seller = finalize_seller_account_closure(
            seller=self.seller_closing,
            actor=self.super_admin,
            notes="Zero activity merchant closure verified.",
            confirm=True,
        )
        self.assertEqual(closed_seller.status, Seller.STATUS_CLOSED)
        self.assertFalse(closed_seller.is_active)
        self.assertEqual(closed_seller.closed_by, self.super_admin)

    def test_closure_blocked_by_open_unfulfilled_order(self):
        """Open orders in processing or confirmed block finalization."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_PROCESSING,
            payment_status=Order.PAYMENT_STATUS_PAID,
            subtotal=Decimal("500.00"),
            total_amount=Decimal("500.00"),
        )
        SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-OPEN-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("500.00"),
        )

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with open orders.",
                confirm=True,
            )
        self.assertIn("open/unfulfilled sub-order", str(ctx.exception))
        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSING)

    def test_closure_blocked_by_unresolved_return_request(self):
        """Active return requests must be resolved before closure."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-RET-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_DELIVERED,
            payment_status=Order.PAYMENT_STATUS_PAID,
            subtotal=Decimal("1200.00"),
            total_amount=Decimal("1200.00"),
        )
        seller_order = SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-RET-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("1200.00"),
            delivered_at=self.now - timedelta(days=10),
        )
        ReturnRequest.objects.create(
            seller_order=seller_order,
            order=order,
            customer=self.customer,
            seller=self.seller_closing,
            request_number=f"RET-{uuid.uuid4().hex[:6].upper()}",
            reason=ReturnRequest.REASON_OTHER,
            status=ReturnRequest.STATUS_REQUESTED,
        )

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with open return.",
                confirm=True,
            )
        self.assertIn("active return/exchange request", str(ctx.exception))

    def test_closure_blocked_by_pending_or_failed_refund(self):
        """Pending or failed refund records block closure."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-REF-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_DELIVERED,
            payment_status=Order.PAYMENT_STATUS_PAID,
            subtotal=Decimal("800.00"),
            total_amount=Decimal("800.00"),
        )
        seller_order = SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-REF-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("800.00"),
            delivered_at=self.now - timedelta(days=8),
        )
        RefundRecord.objects.create(
            seller_order=seller_order,
            order=order,
            refund_reference=f"REF-{uuid.uuid4().hex[:6].upper()}",
            requested_amount=Decimal("800.00"),
            approved_amount=Decimal("0.00"),
            status=RefundRecord.STATUS_PENDING,
        )

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with pending refund.",
                confirm=True,
            )
        self.assertIn("refund record(s) pending processing", str(ctx.exception))

    def test_closure_blocked_by_unreconciled_cod(self):
        """Courier Cash on Delivery pending reconciliation blocks closure."""
        cod_order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-COD-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_DELIVERED,
            subtotal=Decimal("950.00"),
            total_amount=Decimal("950.00"),
        )
        SellerOrder.objects.create(
            order=cod_order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-COD-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("950.00"),
            delivered_at=self.now - timedelta(days=8),
        )

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with unreconciled COD.",
                confirm=True,
            )
        self.assertIn("remittance record(s) pending reconciliation", str(ctx.exception))

    def test_closure_blocked_by_active_cooling_hold(self):
        """Sub-order delivered recently with < 120 hours remaining blocks closure."""
        recent_order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-COOL-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_DELIVERED,
            subtotal=Decimal("1500.00"),
            total_amount=Decimal("1500.00"),
        )
        # Delivered 24 hours ago (120 - 24 = 96 hours cooling remaining)
        cool_suborder = SellerOrder.objects.create(
            order=recent_order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-COOL-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("1500.00"),
            total_commission=Decimal("150.00"),
            delivered_at=self.now - timedelta(hours=24),
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1500.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            order=recent_order,
            seller_order=cool_suborder,
            idempotency_key=f"SALE-COOL-{recent_order.id}",
        )

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with active cooling hold.",
                confirm=True,
            )
        self.assertIn("cooling period pending", str(ctx.exception))

    def test_closure_blocked_by_negative_balance(self):
        """Negative ledger balance (outstanding merchant liability) blocks closure."""
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_RETURN_REFUND,
            amount=Decimal("500.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            idempotency_key=f"DEBIT-NEG-{uuid.uuid4().hex[:6]}",
        )

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with negative balance.",
                confirm=True,
            )
        self.assertIn("Negative ledger balance", str(ctx.exception))

    def test_closure_blocked_by_unbatched_positive_residual(self):
        """Positive unbatched eligible balance must be disbursed via final batch before closure."""
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("350.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            idempotency_key=f"CREDIT-UNBATCHED-{uuid.uuid4().hex[:6]}",
        )

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with unbatched balance.",
                confirm=True,
            )
        self.assertIn("Final settlement batch must be created and disbursed", str(ctx.exception))

    def test_closure_blocked_by_draft_final_settlement_batch(self):
        """Active final settlement batch in DRAFT status blocks closure."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-BATCH-{uuid.uuid4().hex[:8].upper()}",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_DELIVERED,
            subtotal=Decimal("600.00"),
            total_amount=Decimal("600.00"),
        )
        SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-BATCH-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("600.00"),
            total_commission=Decimal("60.00"),
            delivered_at=self.now - timedelta(days=7),
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("600.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            order=order,
            idempotency_key=f"SALE-BATCH-{order.id}",
        )
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
            amount=Decimal("60.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            order=order,
            idempotency_key=f"COMM-BATCH-{order.id}",
        )

        # Create draft final settlement batch
        batch = create_final_settlement_batch(self.seller_closing, actor=self.finance_user)
        self.assertEqual(batch.status, PayoutBatch.STATUS_DRAFT)

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Attempting closure with uncompleted final batch.",
                confirm=True,
            )
        self.assertIn("must be completed", str(ctx.exception))

    def test_prohibit_direct_finalization_from_non_closing_statuses(self):
        """Direct finalization from STATUS_APPROVED or STATUS_CLOSURE_REQUESTED is strictly rejected."""
        # 1. From STATUS_APPROVED
        self.seller_closing.status = Seller.STATUS_APPROVED
        self.seller_closing.save(update_fields=["status"])

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.super_admin,
                notes="Bypassing closing state.",
                confirm=True,
            )
        self.assertIn("must be in 'Closing / Settlement Run-off'", str(ctx.exception))

        # 2. From STATUS_CLOSURE_REQUESTED
        self.seller_closing.status = Seller.STATUS_CLOSURE_REQUESTED
        self.seller_closing.save(update_fields=["status"])

        with self.assertRaises(PayoutEligibilityError) as ctx2:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.super_admin,
                notes="Bypassing closing state from request.",
                confirm=True,
            )
        self.assertIn("must be in 'Closing / Settlement Run-off'", str(ctx2.exception))

    def test_prohibit_seller_self_finalization(self):
        """Seller user cannot finalize their own account closure."""
        with self.assertRaises(LedgerServiceError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.seller_user,
                notes="Self-closing my account.",
                confirm=True,
            )
        self.assertIn("strictly prohibited from finalizing their own account closure", str(ctx.exception))

    def test_unauthorized_staff_role_rejected(self):
        """Support staff or non-finance/non-super_admin staff cannot finalize closure."""
        with self.assertRaises(LedgerServiceError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.support_user,
                notes="Support attempting closure.",
                confirm=True,
            )
        self.assertIn("Super Admin or Finance Manager role required", str(ctx.exception))

    def test_missing_confirmation_or_notes_rejected(self):
        """Confirmation flag and notes (min 5 chars) are required."""
        with self.assertRaises(LedgerServiceError) as ctx1:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Valid notes here",
                confirm=False,
            )
        self.assertIn("Explicit confirmation required", str(ctx1.exception))

        with self.assertRaises(LedgerServiceError) as ctx2:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="bad",  # <5 chars
                confirm=True,
            )
        self.assertIn("minimum 5 characters", str(ctx2.exception))

    def test_idempotent_duplicate_closure_rejected(self):
        """Finalizing an already closed account is safely rejected."""
        self.seller_closing.status = Seller.STATUS_CLOSED
        self.seller_closing.closed_at = self.now
        self.seller_closing.save(update_fields=["status", "closed_at"])

        with self.assertRaises(LedgerServiceError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Closing again.",
                confirm=True,
            )
        self.assertIn("already closed", str(ctx.exception))

    def test_notification_failure_does_not_rollback_closure(self):
        """If email dispatch throws an exception on commit, closure transaction completes safely."""
        with patch("core.emails.send_seller_closure_finalized_email", side_effect=Exception("SMTP Connection Error")):
            with self.captureOnCommitCallbacks(execute=True):
                closed_seller = finalize_seller_account_closure(
                    seller=self.seller_closing,
                    actor=self.finance_user,
                    notes="Closing with failing email server.",
                    confirm=True,
                )
            self.assertEqual(closed_seller.status, Seller.STATUS_CLOSED)
            self.assertFalse(closed_seller.is_active)

    def test_closure_notification_dispatches_only_after_commit(self):
        """Notification email callback is scheduled via transaction.on_commit and executes post-commit."""
        with patch("core.emails.send_seller_closure_finalized_email") as mock_email:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                closed_seller = finalize_seller_account_closure(
                    seller=self.seller_closing,
                    actor=self.finance_user,
                    notes="Testing deferred commit callback execution.",
                    confirm=True,
                )
                # Before exiting commit context, email must NOT have been called
                mock_email.assert_not_called()

            # Callback is now captured upon commit block completion
            self.assertEqual(len(callbacks), 1)
            mock_email.assert_not_called()

            # Now execute captured callback explicitly (simulating post-commit)
            callbacks[0]()
            mock_email.assert_called_once()
            called_seller, = mock_email.call_args[0]
            self.assertEqual(called_seller.id, closed_seller.id)
            self.assertEqual(called_seller.status, Seller.STATUS_CLOSED)
            self.assertEqual(mock_email.call_args[1]["notes"], "Testing deferred commit callback execution.")

    def test_closure_notification_does_not_dispatch_on_transaction_rollback(self):
        """If finalization encounters an error causing rollback, on_commit callback is never executed."""
        self.seller_closing.status = Seller.STATUS_APPROVED
        self.seller_closing.save(update_fields=["status"])

        with patch("core.emails.send_seller_closure_finalized_email") as mock_email:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(PayoutEligibilityError):
                    finalize_seller_account_closure(
                        seller=self.seller_closing,
                        actor=self.super_admin,
                        notes="Should fail and rollback.",
                        confirm=True,
                    )
            self.assertEqual(len(callbacks), 0)
            mock_email.assert_not_called()

    # =========================================================================
    # Fix 2 Tests: Manual Transfer & UTR / Date Binding on Final Batch
    # =========================================================================

    def test_final_batch_requires_both_utr_and_date_for_clearance(self):
        """Completed final batch requires both non-empty manual_utr_reference and manual_transfer_date."""
        batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller_closing,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
            amount=Decimal("500.00"),
            currency="INR",
            status=PayoutBatch.STATUS_COMPLETED,
            payout_channel=PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
            manual_utr_reference="",
            manual_transfer_date=None,
        )

        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["can_fully_close"])
        self.assertTrue(any("missing required transfer details" in b for b in clearance["closure_blockers"]))

        # Add only UTR (transfer date still missing)
        batch.manual_utr_reference = "UTR-TEST-12345"
        batch.save(update_fields=["manual_utr_reference"])
        clearance_no_date = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance_no_date["can_fully_close"])
        self.assertTrue(any("transfer execution date" in b for b in clearance_no_date["closure_blockers"]))

        # Add only date (remove UTR)
        batch.manual_utr_reference = ""
        batch.manual_transfer_date = date.today()
        batch.save(update_fields=["manual_utr_reference", "manual_transfer_date"])
        clearance_no_utr = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance_no_utr["can_fully_close"])
        self.assertTrue(any("verified bank UTR reference" in b for b in clearance_no_utr["closure_blockers"]))

        # Both present
        batch.manual_utr_reference = "UTR-TEST-12345"
        batch.manual_transfer_date = date.today()
        batch.save(update_fields=["manual_utr_reference", "manual_transfer_date"])
        clearance_ok = audit_seller_closure_clearance(self.seller_closing)
        self.assertTrue(clearance_ok["can_fully_close"])
        self.assertEqual(clearance_ok["closure_blockers"], [])

    def test_mock_payout_rejected_on_final_settlement_batch(self):
        """process_mock_payout_batch rejects attempts to settle final settlement batches."""
        batch = self._create_payable_final_settlement_batch()
        self.assertEqual(batch.batch_type, PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT)

        with self.assertRaises(LedgerServiceError) as ctx:
            process_mock_payout_batch(payout_batch=batch, actor=self.finance_user)
        self.assertIn("final settlement batch", str(ctx.exception))
        self.assertIn("verified manual bank transfer workflow", str(ctx.exception))

    def test_mock_payout_succeeds_on_regular_cycle_batch(self):
        """Regular cycle payout batch continues to allow mock disbursement settlement."""
        cycle_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-CYCLE-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller_closing,
            batch_type=PayoutBatch.BATCH_TYPE_CYCLE,
            amount=Decimal("200.00"),
            currency="INR",
            status=PayoutBatch.STATUS_DRAFT,
            payout_channel=PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
        )
        success, msg, updated = process_mock_payout_batch(payout_batch=cycle_batch, actor=self.finance_user)
        self.assertTrue(success)
        self.assertEqual(updated.status, PayoutBatch.STATUS_COMPLETED)

    def test_duplicate_manual_transfer_request_does_not_create_duplicate_ledger_debit(self):
        """Recording manual bank transfer with same UTR is idempotent and does not post second debit entry."""
        batch = self._create_payable_final_settlement_batch()
        batch.approved_at = timezone.now()
        batch.approved_by = self.finance_user
        batch.save(update_fields=["approved_at", "approved_by"])

        utr = f"UTR-IDEMP-{uuid.uuid4().hex[:8].upper()}"
        val_date = date.today()

        # First record
        success1, msg1, b1 = record_manual_bank_transfer(
            payout_batch=batch,
            actor=self.finance_user,
            utr_reference=utr,
            transfer_date=val_date,
            confirmation=True,
        )
        self.assertTrue(success1)
        debit_count1 = SellerLedgerEntry.objects.filter(
            seller=self.seller_closing,
            category=SellerLedgerEntry.CATEGORY_PAYOUT,
        ).count()
        self.assertEqual(debit_count1, 1)

        # Second record with same UTR
        success2, msg2, b2 = record_manual_bank_transfer(
            payout_batch=b1,
            actor=self.finance_user,
            utr_reference=utr,
            transfer_date=val_date,
            confirmation=True,
        )
        self.assertTrue(success2)
        self.assertIn("already recorded", msg2)
        debit_count2 = SellerLedgerEntry.objects.filter(
            seller=self.seller_closing,
            category=SellerLedgerEntry.CATEGORY_PAYOUT,
        ).count()
        self.assertEqual(debit_count2, 1)

    # =========================================================================
    # Fix 3 Tests: Order-Level Cancellation Refunds Detection
    # =========================================================================

    def test_closure_blocked_by_pending_order_level_refund(self):
        """Order-level refund (seller_order IS NULL) in PENDING status blocks closure."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-OL-PEND-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_CANCELLED,
            payment_status=Order.PAYMENT_STATUS_PAID,
            subtotal=Decimal("800.00"),
            total_amount=Decimal("800.00"),
        )
        SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-OL-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_CANCELLED,
            subtotal=Decimal("800.00"),
        )
        RefundRecord.objects.create(
            order=order,
            seller_order=None,
            refund_reference=f"REF-OL-PEND-{uuid.uuid4().hex[:6].upper()}",
            requested_amount=Decimal("800.00"),
            approved_amount=Decimal("0.00"),
            status=RefundRecord.STATUS_PENDING,
        )

        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["can_fully_close"])
        self.assertTrue(any("refund record(s) pending processing" in b for b in clearance["closure_blockers"]))

        with self.assertRaises(PayoutEligibilityError) as ctx:
            finalize_seller_account_closure(
                seller=self.seller_closing,
                actor=self.finance_user,
                notes="Testing order-level pending refund blocker.",
                confirm=True,
            )
        self.assertIn("refund record(s) pending processing", str(ctx.exception))

    def test_closure_blocked_by_failed_order_level_refund(self):
        """Order-level refund in FAILED status blocks closure."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-OL-FAIL-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_CANCELLED,
            payment_status=Order.PAYMENT_STATUS_PAID,
            subtotal=Decimal("600.00"),
            total_amount=Decimal("600.00"),
        )
        SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-FAIL-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_CANCELLED,
            subtotal=Decimal("600.00"),
        )
        RefundRecord.objects.create(
            order=order,
            seller_order=None,
            refund_reference=f"REF-OL-FAIL-{uuid.uuid4().hex[:6].upper()}",
            requested_amount=Decimal("600.00"),
            approved_amount=Decimal("0.00"),
            status=RefundRecord.STATUS_FAILED,
        )

        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertFalse(clearance["can_fully_close"])
        self.assertTrue(any("failed state" in b for b in clearance["closure_blockers"]))

    def test_unrelated_seller_order_level_refund_does_not_block_closure(self):
        """Order-level refund belonging to a different seller does not block this seller's closure."""
        other_seller = Seller.objects.create(
            user=self.other_seller_user,
            store_name="Other Crafts",
            legal_business_name="Other Crafts Pvt Ltd",
            support_email="support@othercrafts.in",
            support_phone="9876543211",
            registered_address="13 Johari Bazar",
            city="Jaipur",
            state="Rajasthan",
            postal_code="302003",
            pickup_address="13 Johari Bazar",
            pickup_city="Jaipur",
            pickup_state="Rajasthan",
            pickup_postal_code="302003",
            gstin="08ABCDE1234F1Z6",
            pan_number="ABCDE1234G",
            status=Seller.STATUS_APPROVED,
        )
        other_order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-OTHER-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_CANCELLED,
            payment_status=Order.PAYMENT_STATUS_PAID,
            subtotal=Decimal("750.00"),
            total_amount=Decimal("750.00"),
        )
        SellerOrder.objects.create(
            order=other_order,
            seller=other_seller,
            sub_order_number=f"FM-PKG-OTHER-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_CANCELLED,
            subtotal=Decimal("750.00"),
        )
        RefundRecord.objects.create(
            order=other_order,
            seller_order=None,
            refund_reference=f"REF-OTHER-{uuid.uuid4().hex[:6].upper()}",
            requested_amount=Decimal("750.00"),
            approved_amount=Decimal("0.00"),
            status=RefundRecord.STATUS_PENDING,
        )

        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertTrue(clearance["can_fully_close"])
        self.assertEqual(clearance["closure_blockers"], [])

    def test_terminal_order_level_refund_does_not_block_closure(self):
        """Order-level refund in PROCESSED or CANCELLED status does not block closure."""
        order = Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-TERM-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_CANCELLED,
            payment_status=Order.PAYMENT_STATUS_REFUNDED,
            subtotal=Decimal("450.00"),
            total_amount=Decimal("450.00"),
        )
        SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-TERM-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_CANCELLED,
            subtotal=Decimal("450.00"),
        )
        RefundRecord.objects.create(
            order=order,
            seller_order=None,
            refund_reference=f"REF-TERM-1-{uuid.uuid4().hex[:6].upper()}",
            requested_amount=Decimal("450.00"),
            approved_amount=Decimal("450.00"),
            status=RefundRecord.STATUS_PROCESSED,
        )
        RefundRecord.objects.create(
            order=order,
            seller_order=None,
            refund_reference=f"REF-TERM-2-{uuid.uuid4().hex[:6].upper()}",
            requested_amount=Decimal("100.00"),
            approved_amount=Decimal("0.00"),
            status=RefundRecord.STATUS_CANCELLED,
        )

        clearance = audit_seller_closure_clearance(self.seller_closing)
        self.assertTrue(clearance["can_fully_close"])
        self.assertEqual(clearance["closure_blockers"], [])


class SellerClosureFinalizationViewTests(BaseSellerClosureFinalizationTestCase):
    """Integration tests for admin_seller_closure_finalize_view and closure_detail template."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.finalize_url = reverse(
            "custom_admin:seller_closure_finalize",
            kwargs={"seller_id": self.seller_closing.pk},
        )
        self.detail_url = reverse(
            "custom_admin:seller_closure_detail",
            kwargs={"seller_id": self.seller_closing.pk},
        )

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated requests are redirected to admin login."""
        response = self.client.post(self.finalize_url, {
            "confirm_closure": "on",
            "closure_notes": "Attempting anonymous closure.",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_seller_user_forbidden(self):
        """Seller user attempting to hit the admin endpoint receives 302 or 403."""
        self.client.force_login(self.seller_user)
        response = self.client.post(self.finalize_url, {
            "confirm_closure": "on",
            "closure_notes": "Attempting vendor self closure.",
        })
        # @admin_role_required redirects unauthorized users to custom_admin:dashboard or returns 403
        self.assertIn(response.status_code, [302, 403])

    def test_support_staff_forbidden(self):
        """Staff with ROLE_SUPPORT_MANAGER is unauthorized and received 403."""
        self.client.force_login(self.support_user)
        response = self.client.post(self.finalize_url, {
            "confirm_closure": "on",
            "closure_notes": "Support role attempting closure.",
        })
        self.assertIn(response.status_code, [302, 403])
        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSING)

    def test_get_request_rejected(self):
        """GET request to finalize endpoint returns 405 Method Not Allowed."""
        self.client.force_login(self.finance_user)
        response = self.client.get(self.finalize_url)
        self.assertEqual(response.status_code, 405)

    def test_post_missing_confirmation_checkbox_fails(self):
        """POST without confirmation checkbox fails validation and redirects with message."""
        self.client.force_login(self.finance_user)
        response = self.client.post(self.finalize_url, {
            "closure_notes": "Valid notes but missing checkbox.",
        })
        self.assertEqual(response.status_code, 302)
        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSING)

    def test_post_missing_notes_fails(self):
        """POST with empty or too-short notes fails validation."""
        self.client.force_login(self.finance_user)
        response = self.client.post(self.finalize_url, {
            "confirm_closure": "on",
            "closure_notes": "abc",  # <5 chars
        })
        self.assertEqual(response.status_code, 302)
        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSING)

    def test_post_successful_finalization_by_finance_manager(self):
        """Finance Manager with valid confirmation and notes successfully finalizes clean seller."""
        self.client.force_login(self.finance_user)
        response = self.client.post(self.finalize_url, {
            "confirm_closure": "on",
            "closure_notes": "All clearance verified. Merchant account is now permanently closed.",
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, self.detail_url)

        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSED)
        self.assertFalse(self.seller_closing.is_active)
        self.assertEqual(self.seller_closing.closed_by, self.finance_user)
        self.assertIsNotNone(self.seller_closing.closed_at)

        # Products unlisted
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertFalse(self.product1.is_active)
        self.assertFalse(self.product2.is_active)

    def test_post_blocked_when_clearance_fails(self):
        """When an unresolved blocker exists, view rejects closure and displays error message."""
        # Add open order
        Order.objects.create(
            user=self.customer,
            order_number=f"FM-ORD-BLOCK-{uuid.uuid4().hex[:8].upper()}",
            status=Order.STATUS_CONFIRMED,
            subtotal=Decimal("400.00"),
            total_amount=Decimal("400.00"),
        )
        SellerOrder.objects.create(
            order=Order.objects.latest("id"),
            seller=self.seller_closing,
            sub_order_number=f"FM-PKG-BLOCK-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("400.00"),
        )

        self.client.force_login(self.super_admin)
        response = self.client.post(self.finalize_url, {
            "confirm_closure": "on",
            "closure_notes": "Attempting closure with open orders.",
        })
        self.assertEqual(response.status_code, 302)
        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSING)

    def test_admin_detail_view_renders_final_closure_form_when_eligible(self):
        """Detail view renders the Final Account Closure form and confirmation checkbox when eligible."""
        self.client.force_login(self.finance_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Final Account Closure")
        self.assertContains(response, "Action Ready")
        self.assertContains(response, 'id="finalizeClosureForm"')
        self.assertContains(response, 'name="confirm_closure"')
        self.assertContains(response, 'name="closure_notes"')
        self.assertContains(response, "Finalize Account Closure")

    def test_admin_detail_view_renders_blockers_when_ineligible(self):
        """Detail view renders clearance blockers and blocks finalization when ineligible."""
        # Add negative balance
        SellerLedgerEntry.objects.create(
            seller=self.seller_closing,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_RETURN_REFUND,
            amount=Decimal("300.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            idempotency_key=f"DEBIT-BLOCK-UI-{uuid.uuid4().hex[:6]}",
        )
        self.client.force_login(self.finance_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Clearance Blocked")
        self.assertContains(response, "Negative ledger balance")
        self.assertNotContains(response, 'id="finalizeClosureForm"')

    def test_admin_detail_view_renders_closed_card_when_already_closed(self):
        """Detail view renders the Account Formally Closed summary card when status is CLOSED."""
        self.seller_closing.status = Seller.STATUS_CLOSED
        self.seller_closing.closed_at = self.now
        self.seller_closing.closed_by = self.finance_user
        self.seller_closing.save(update_fields=["status", "closed_at", "closed_by"])

        self.client.force_login(self.finance_user)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Account Formally Closed")
        self.assertContains(response, "Closed / Terminated")
        self.assertContains(response, "finance_mgr_test")

    def test_admin_process_view_rejects_final_settlement_batch(self):
        """Admin payout_process view rejects attempts to mock-process a final settlement batch."""
        batch = self._create_payable_final_settlement_batch()
        self.client.force_login(self.finance_user)
        process_url = reverse("custom_admin:payout_process", kwargs={"batch_reference": batch.batch_reference})
        response = self.client.post(process_url, {})
        self.assertEqual(response.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatch.STATUS_DRAFT)

    def test_payout_detail_template_renders_policy_card_for_final_settlement_batch(self):
        """Payout detail template hides mock execution form and renders policy alert for final batch."""
        batch = self._create_payable_final_settlement_batch()
        self.client.force_login(self.finance_user)
        detail_url = reverse("custom_admin:payout_detail", kwargs={"batch_reference": batch.batch_reference})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Final Settlement Disbursal Policy")
        self.assertNotContains(response, "Execute Mock Settlement")


class SellerClosureFinalSafetyAndReadonlyVerificationTests(BaseSellerClosureFinalizationTestCase):
    """Safety and regression tests verifying Phase 4 read-only portal retention after closure."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        # Finalize seller account closure
        finalize_seller_account_closure(
            seller=self.seller_closing,
            actor=self.finance_user,
            notes="Formal account closure completed for safety verification.",
            confirm=True,
        )
        self.seller_closing.refresh_from_db()
        self.assertEqual(self.seller_closing.status, Seller.STATUS_CLOSED)

    def test_closed_seller_can_view_portal_read_only(self):
        """Closed seller can log in and view historical reports and orders."""
        self.client.force_login(self.seller_user)

        # Dashboard
        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(dash_resp.status_code, 200)

        # Orders list
        orders_resp = self.client.get(reverse("core:seller_order_list"))
        self.assertEqual(orders_resp.status_code, 200)

        # Reports
        reports_resp = self.client.get(reverse("core:seller_reports"))
        self.assertEqual(reports_resp.status_code, 200)

        # Profile
        profile_resp = self.client.get(reverse("core:seller_profile"))
        self.assertEqual(profile_resp.status_code, 200)

    def test_closed_seller_portal_mutations_blocked(self):
        """Mutations on closed seller account (profile update, bank update, product add) are blocked."""
        self.client.force_login(self.seller_user)

        # POST to profile update
        profile_post = self.client.post(reverse("core:seller_profile"), {
            "store_description": "New bio attempt",
        })
        self.assertIn(profile_post.status_code, [302, 403])

        # POST to bank details
        bank_post = self.client.post(reverse("core:seller_bank_details"), {
            "bank_account_holder_name": "Hack Attempt",
        })
        self.assertIn(bank_post.status_code, [302, 403])

        # POST to product add
        product_post = self.client.post(reverse("core:seller_product_add"), {
            "name": "New Product Attempt",
            "price": "100.00",
        })
        self.assertIn(product_post.status_code, [302, 403])

    def test_products_delisted_and_unsearchable_on_storefront(self):
        """Storefront queries filtering active products exclude closed seller items."""
        active_storefront_products = Product.objects.filter(is_active=True)
        self.assertFalse(active_storefront_products.filter(seller=self.seller_closing).exists())
