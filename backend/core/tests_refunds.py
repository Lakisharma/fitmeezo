"""
Fitmeezo Automatic Order Cancellation & Razorpay Refund System - Comprehensive Test Suite.

Verifies:
1. Successful online payment followed by cancellation and automatic Razorpay refund.
2. Unpaid (COD and unpaid online) and failed payments do not trigger refunds.
3. Ineligible order cancellation (shipped/delivered) is rejected.
4. Duplicate cancellation requests are rejected and idempotent.
5. Duplicate webhook deliveries are handled idempotently.
6. Invalid webhook signatures return 403 Forbidden.
7. Failed refund requests retain recoverable status (refund_status='failed') with error logs.
8. Partial refunds and previous refunds accounting (only refunds remaining balance).
9. API timeout recovery and reconciliation (no duplicate refunds).
10. Customer ownership and admin permission isolation.
"""

from decimal import Decimal
import json
from unittest.mock import MagicMock, patch
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

import razorpay.errors

from core.models import Category, Order, OrderItem, Product, ProductVariant, RefundRecord
from core.refund_services import (
    calculate_order_refundable_amount,
    initiate_order_cancellation_refund,
    is_order_eligible_for_automatic_refund,
    reconcile_order_refund,
)
from core.services import cancel_order

User = get_user_model()


class RazorpayRefundSystemTestCase(TestCase):
    """Full automated verification suite for Razorpay refund processing."""

    def setUp(self):
        self.client = Client()
        mail.outbox.clear()

        # Users
        self.customer1 = User.objects.create_user(
            username="rohit_customer",
            email="rohit@example.com",
            password="RohitPassword123!",
            first_name="Rohit",
            last_name="Verma",
        )
        self.customer2 = User.objects.create_user(
            username="ananya_customer",
            email="ananya@example.com",
            password="AnanyaPassword123!",
            first_name="Ananya",
            last_name="Sen",
        )
        self.staff_admin = User.objects.create_user(
            username="finance_admin",
            email="finance@fitmeezo.com",
            password="FinanceAdminPass123!",
            is_staff=True,
            is_superuser=True,
        )

        # Catalogue
        self.category = Category.objects.create(name="Fitness", slug="fitness", is_active=True)
        self.product = Product.objects.create(
            name="Pro Dumbbell Set",
            slug="pro-dumbbell-set",
            sku="PDS-001",
            category=self.category,
            price=Decimal("2000.00"),
            stock_quantity=20,
            is_active=True,
        )

    def _create_test_order(
        self,
        user,
        order_number="FM-TEST-RFND-01",
        status=Order.STATUS_PROCESSING,
        payment_method=Order.PAYMENT_METHOD_RAZORPAY,
        payment_status=Order.PAYMENT_STATUS_PAID,
        razorpay_order_id="order_rp_mock_123",
        razorpay_payment_id="pay_rp_mock_456",
        total_amount=Decimal("2000.00"),
    ) -> Order:
        order = Order.objects.create(
            order_number=order_number,
            user=user,
            shipping_full_name=f"{user.first_name} {user.last_name}",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Park Avenue",
            shipping_city="Mumbai",
            shipping_state="Maharashtra",
            shipping_postal_code="400001",
            subtotal=total_amount,
            shipping_fee=Decimal("0.00"),
            total_amount=total_amount,
            status=status,
            payment_method=payment_method,
            payment_status=payment_status,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            unit_price=total_amount,
            quantity=1,
            line_total=total_amount,
        )
        return order

    # -------------------------------------------------------------------------
    # 1. Successful online payment followed by cancellation and automatic refund
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        FITMEEZO_MOCK_REFUNDS=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    @patch("razorpay.Client")
    def test_successful_online_payment_cancellation_and_automatic_refund(self, mock_client_cls):
        """
        Tests that when customer cancels a paid Razorpay order,
        Razorpay client payment.refund is called with exact amount in paise,
        Order and RefundRecord are updated safely, inventory is restored, and customer is emailed.
        """
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.payment.refund.return_value = {
            "id": "rfnd_mock_success_789",
            "entity": "refund",
            "amount": 200000,
            "currency": "INR",
            "payment_id": "pay_rp_mock_456",
            "status": "processed",
        }

        order = self._create_test_order(self.customer1, order_number="FM-CANCEL-SUCCESS-01")
        initial_stock = self.product.stock_quantity

        self.client.force_login(self.customer1)
        response = self.client.post(
            reverse("core:order_cancel", kwargs={"order_number": order.order_number}),
            {"reason": "Changed mind, want different color"},
        )

        self.assertRedirects(response, reverse("core:order_detail", kwargs={"order_number": order.order_number}))

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_REFUNDED)
        self.assertEqual(order.razorpay_refund_id, "rfnd_mock_success_789")
        self.assertEqual(order.refund_amount, Decimal("2000.00"))
        self.assertIsNotNone(order.refunded_at)

        # Inventory stock restored
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, initial_stock + 1)

        # RefundRecord created
        ref_record = RefundRecord.objects.filter(order=order).first()
        self.assertIsNotNone(ref_record)
        self.assertEqual(ref_record.external_reference, "rfnd_mock_success_789")
        self.assertEqual(ref_record.status, RefundRecord.STATUS_PROCESSED)
        self.assertEqual(ref_record.approved_amount, Decimal("2000.00"))

        # Verify Razorpay API was called with amount in paise
        mock_instance.payment.refund.assert_called_once()
        args, kwargs = mock_instance.payment.refund.call_args
        self.assertEqual(args[0], "pay_rp_mock_456")
        self.assertEqual(args[1]["amount"], 200000)

        # Verify emails sent (cancellation + refund)
        self.assertTrue(len(mail.outbox) >= 1)

    # -------------------------------------------------------------------------
    # 2. Unpaid (COD and unpaid online) and failed payments
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        FITMEEZO_MOCK_REFUNDS=False,
    )
    @patch("razorpay.Client")
    def test_unpaid_cod_order_cancellation_triggers_no_refund(self, mock_client_cls):
        """Tests that COD order cancellation restores inventory with refund_status='not_applicable' and 0 API calls."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        order = self._create_test_order(
            self.customer1,
            order_number="FM-COD-CANCEL-01",
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            razorpay_order_id="",
            razorpay_payment_id="",
        )

        success, msg, cancelled_order = cancel_order(order, user=self.customer1, reason="No longer need it")
        self.assertTrue(success)
        cancelled_order.refresh_from_db()

        self.assertEqual(cancelled_order.status, Order.STATUS_CANCELLED)
        self.assertEqual(cancelled_order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)
        mock_instance.payment.refund.assert_not_called()
        self.assertEqual(RefundRecord.objects.filter(order=cancelled_order).count(), 0)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        FITMEEZO_MOCK_REFUNDS=False,
    )
    @patch("razorpay.Client")
    def test_unpaid_online_order_cancellation_triggers_no_refund(self, mock_client_cls):
        """Tests that an online Razorpay order that was abandoned unpaid triggers no refund call."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        order = self._create_test_order(
            self.customer1,
            order_number="FM-UNPAID-ONLINE-01",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            razorpay_payment_id="",  # Customer never completed payment
        )

        success, msg, cancelled_order = cancel_order(order, user=self.customer1)
        self.assertTrue(success)
        cancelled_order.refresh_from_db()

        self.assertEqual(cancelled_order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)
        mock_instance.payment.refund.assert_not_called()

    # -------------------------------------------------------------------------
    # 3. Ineligible order cancellation (shipped/delivered)
    # -------------------------------------------------------------------------
    def test_shipped_and_delivered_orders_ineligible_for_cancellation(self):
        """Tests that orders in Shipped or Delivered state cannot be cancelled."""
        shipped_order = self._create_test_order(
            self.customer1,
            order_number="FM-SHIPPED-01",
            status=Order.STATUS_SHIPPED,
        )
        delivered_order = self._create_test_order(
            self.customer1,
            order_number="FM-DELIVERED-01",
            status=Order.STATUS_DELIVERED,
        )

        success_shipped, msg1, _ = cancel_order(shipped_order, user=self.customer1)
        self.assertFalse(success_shipped)
        self.assertIn("cannot be cancelled", msg1)

        success_delivered, msg2, _ = cancel_order(delivered_order, user=self.customer1)
        self.assertFalse(success_delivered)
        self.assertIn("cannot be cancelled", msg2)

    # -------------------------------------------------------------------------
    # 4. Duplicate cancellation requests are rejected and idempotent
    # -------------------------------------------------------------------------
    def test_duplicate_cancellation_requests_rejected(self):
        """Tests that an order already cancelled cannot be cancelled again (prevents duplicate stock restoration or refund)."""
        order = self._create_test_order(self.customer1, order_number="FM-DUP-CANCEL-01")
        success1, _, cancelled_order = cancel_order(order, user=self.customer1)
        self.assertTrue(success1)

        # Attempt duplicate cancellation
        success2, msg2, _ = cancel_order(cancelled_order, user=self.customer1)
        self.assertFalse(success2)
        self.assertIn("already been cancelled", msg2)

    # -------------------------------------------------------------------------
    # 5. Duplicate webhook deliveries handled idempotently
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_duplicate_webhook_deliveries_handled_idempotently(self, mock_client_cls):
        """Tests that sending duplicate refund.processed webhooks does not error and produces idempotent state."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-WH-DUP-01",
            status=Order.STATUS_CANCELLED,
            payment_status=Order.PAYMENT_STATUS_PAID,
        )
        order.refund_status = Order.REFUND_STATUS_PENDING
        order.save()

        payload = json.dumps({
            "event": "refund.processed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_webhook_12345",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 200000,
                        "notes": {
                            "order_number": order.order_number,
                        },
                    }
                }
            }
        })

        # Delivery 1
        res1 = self.client.post(
            reverse("core:razorpay_webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_mock_sig",
        )
        self.assertEqual(res1.status_code, 200)

        order.refresh_from_db()
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)
        self.assertEqual(order.razorpay_refund_id, "rfnd_webhook_12345")

        # Delivery 2 (Duplicate)
        res2 = self.client.post(
            reverse("core:razorpay_webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_mock_sig",
        )
        self.assertEqual(res2.status_code, 200)

        order.refresh_from_db()
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)

    # -------------------------------------------------------------------------
    # 6. Invalid webhook signatures return 403 Forbidden
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_invalid_webhook_signature_returns_403(self, mock_client_cls):
        """Tests that a webhook with invalid signature is immediately rejected with 403."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.side_effect = razorpay.errors.SignatureVerificationError("Signature mismatch")

        response = self.client.post(
            reverse("core:razorpay_webhook"),
            data=json.dumps({"event": "refund.processed"}),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="fake_invalid_signature",
        )
        self.assertEqual(response.status_code, 403)

    # -------------------------------------------------------------------------
    # 7. Failed refund requests retain recoverable status
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        FITMEEZO_MOCK_REFUNDS=False,
    )
    @patch("razorpay.Client")
    def test_failed_refund_request_retains_recoverable_status(self, mock_client_cls):
        """
        Tests that when Razorpay API raises an exception:
        - Cancellation is preserved (not rolled back)
        - Order.refund_status is set to REFUND_STATUS_FAILED
        - Failure reason is captured
        - RefundRecord is marked STATUS_FAILED
        """
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.payment.refund.side_effect = razorpay.errors.BadRequestError(
            "Payment has already been refunded or is being disputed"
        )

        order = self._create_test_order(
            self.customer1,
            order_number="FM-FAIL-RFND-01",
            status=Order.STATUS_CANCELLED,
        )

        success, msg, ref_record = initiate_order_cancellation_refund(order, actor=self.customer1)
        self.assertFalse(success)

        order.refresh_from_db()
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_FAILED)
        self.assertIn("already been refunded", order.refund_failure_reason)

        self.assertIsNotNone(ref_record)
        self.assertEqual(ref_record.status, RefundRecord.STATUS_FAILED)

    # -------------------------------------------------------------------------
    # 8. Partial refunds and previous refunds accounting
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        FITMEEZO_MOCK_REFUNDS=False,
    )
    @patch("razorpay.Client")
    def test_partial_refunds_and_remaining_balance_calculation(self, mock_client_cls):
        """Tests that prior partial refunds are deducted so only the remaining balance is refunded."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.payment.refund.return_value = {
            "id": "rfnd_partial_second_999",
            "status": "processed",
            "amount": 120000,
        }

        order = self._create_test_order(
            self.customer1,
            order_number="FM-PARTIAL-01",
            status=Order.STATUS_CANCELLED,
            total_amount=Decimal("2000.00"),
        )

        # Record a prior processed partial refund of ₹800
        RefundRecord.objects.create(
            refund_reference="REF-PRIOR-01",
            external_reference="rfnd_prior_800",
            order=order,
            requested_amount=Decimal("800.00"),
            approved_amount=Decimal("800.00"),
            currency="INR",
            payment_method="razorpay",
            status=RefundRecord.STATUS_PROCESSED,
        )

        # Remaining balance should be 2000 - 800 = 1200
        remaining = calculate_order_refundable_amount(order)
        self.assertEqual(remaining, Decimal("1200.00"))

        success, msg, new_refund = initiate_order_cancellation_refund(order)
        self.assertTrue(success)

        # Verify Razorpay called with 1200.00 * 100 = 120000 paise
        args, _ = mock_instance.payment.refund.call_args
        self.assertEqual(args[1]["amount"], 120000)

        # When remaining balance is 0, subsequent refund request is safely prevented
        order.refresh_from_db()
        eligible, reason = is_order_eligible_for_automatic_refund(order)
        self.assertFalse(eligible)

    # -------------------------------------------------------------------------
    # 9. API timeout recovery and reconciliation (no duplicate refunds)
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        FITMEEZO_MOCK_REFUNDS=False,
    )
    @patch("razorpay.Client")
    def test_reconciliation_detects_existing_gateway_refund_without_duplication(self, mock_client_cls):
        """
        Tests that if an earlier request timed out, reconciliation fetches existing refunds from Razorpay
        and synchronizes database records without issuing a second refund.
        """
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        # Razorpay returns that refund was indeed created during timeout
        mock_instance.payment.all_refunds.return_value = {
            "count": 1,
            "items": [
                {
                    "id": "rfnd_already_at_gateway_111",
                    "entity": "refund",
                    "amount": 200000,
                    "status": "processed",
                    "payment_id": "pay_rp_mock_456",
                }
            ],
        }

        order = self._create_test_order(
            self.customer1,
            order_number="FM-RECONCILE-01",
            status=Order.STATUS_CANCELLED,
        )
        order.refund_status = Order.REFUND_STATUS_PENDING
        order.save()

        success, msg, record = reconcile_order_refund(order, actor=self.staff_admin)
        self.assertTrue(success)
        self.assertIn("No duplicate refund was created", msg)

        order.refresh_from_db()
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)
        self.assertEqual(order.razorpay_refund_id, "rfnd_already_at_gateway_111")
        # Ensure payment.refund was NEVER called again!
        mock_instance.payment.refund.assert_not_called()

    # -------------------------------------------------------------------------
    # 10. Customer ownership and admin permission isolation
    # -------------------------------------------------------------------------
    def test_customer_cannot_cancel_another_customers_order(self):
        """Tests multi-tenant security: Customer 2 cannot cancel Customer 1's order."""
        order1 = self._create_test_order(self.customer1, order_number="FM-ISOLATION-01")

        self.client.force_login(self.customer2)
        response = self.client.post(
            reverse("core:order_cancel", kwargs={"order_number": order1.order_number}),
            {"reason": "Malicious cancellation attempt"},
        )
        self.assertEqual(response.status_code, 404)

        order1.refresh_from_db()
        self.assertEqual(order1.status, Order.STATUS_PROCESSING)

    def test_admin_reconciliation_requires_staff_privileges(self):
        """Tests that regular non-staff customers cannot access the administrative refund reconciliation endpoint."""
        order = self._create_test_order(self.customer1, order_number="FM-ADMIN-AUTH-01")

        # Non-authenticated user
        anon_client = Client()
        res_anon = anon_client.post(reverse("custom_admin:order_reconcile_refund", kwargs={"pk": order.pk}))
        self.assertIn(res_anon.status_code, (302, 403))

        # Regular customer (is_staff=False)
        self.client.force_login(self.customer1)
        res_cust = self.client.post(reverse("custom_admin:order_reconcile_refund", kwargs={"pk": order.pk}))
        self.assertIn(res_cust.status_code, (302, 403))

        # Staff admin user (is_staff=True)
        self.client.force_login(self.staff_admin)
        with patch("core.refund_services.reconcile_order_refund") as mock_recon:
            mock_recon.return_value = (True, "Refund reconciled", None)
            res_admin = self.client.post(reverse("custom_admin:order_reconcile_refund", kwargs={"pk": order.pk}))
            self.assertRedirects(res_admin, reverse("custom_admin:order_detail", kwargs={"pk": order.pk}))

    # -------------------------------------------------------------------------
    # 11. Partial refund accounting, cumulative totals, and idempotency
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_partial_refund_keeps_payment_status_paid(self, mock_client_cls):
        """Tests that a ₹500 partial refund on a ₹2000 order sets refund_status='processed' but leaves payment_status='paid'."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-PART-PAY-01",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )

        payload = json.dumps({
            "event": "refund.processed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_part_500",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 50000,  # ₹500.00 in paise
                        "notes": {"order_number": order.order_number},
                    }
                }
            }
        })

        res = self.client.post(
            reverse("core:razorpay_webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res.status_code, 200)

        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("500.00"))
        # Partial refund: MUST REMAIN PAID (not REFUNDED) and refund_status remains PROCESSING
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_pending_voucher_proof_matching(self, mock_client_cls):
        """Tests that a pending voucher is ONLY matched if receipt/refund_reference proves identity."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-VOUCHER-PROOF",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )

        # Existing pending internal voucher with reference 'REF-LEGIT-01'
        pending_voucher = RefundRecord.objects.create(
            refund_reference="REF-LEGIT-01",
            external_reference="",
            order=order,
            requested_amount=Decimal("500.00"),
            approved_amount=Decimal("500.00"),
            currency="INR",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            status=RefundRecord.STATUS_PENDING,
        )

        # Webhook arrives WITHOUT voucher proof (e.g. manual dashboard refund)
        # Should NOT touch pending_voucher; should create a separate record
        unproven_payload = json.dumps({
            "event": "refund.processed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_manual_unproven_111",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 40000,
                        "notes": {},  # no refund_reference
                        "receipt": "",  # no receipt
                    }
                }
            }
        })
        res1 = self.client.post(
            reverse("core:razorpay_webhook"),
            data=unproven_payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res1.status_code, 200)

        pending_voucher.refresh_from_db()
        # Pending voucher must remain untouched!
        self.assertEqual(pending_voucher.external_reference, "")
        self.assertEqual(pending_voucher.status, RefundRecord.STATUS_PENDING)

        # A separate RefundRecord must have been created for rfnd_manual_unproven_111
        separate_rec = RefundRecord.objects.filter(external_reference="rfnd_manual_unproven_111").first()
        self.assertIsNotNone(separate_rec)
        self.assertEqual(separate_rec.status, RefundRecord.STATUS_PROCESSED)
        self.assertEqual(separate_rec.approved_amount, Decimal("400.00"))

        # Webhook arrives WITH proven receipt reference matching 'REF-LEGIT-01'
        proven_payload = json.dumps({
            "event": "refund.processed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_proven_legit_222",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 50000,
                        "receipt": "REF-LEGIT-01",  # Matches!
                    }
                }
            }
        })
        res2 = self.client.post(
            reverse("core:razorpay_webhook"),
            data=proven_payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res2.status_code, 200)

        pending_voucher.refresh_from_db()
        # Now proven voucher is linked and settled!
        self.assertEqual(pending_voucher.external_reference, "rfnd_proven_legit_222")
        self.assertEqual(pending_voucher.status, RefundRecord.STATUS_PROCESSED)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_duplicate_partial_refund_does_not_double_count(self, mock_client_cls):
        """Tests that redelivering the same refund ID does not add the amount twice."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-DUP-COUNT-01",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )

        payload = json.dumps({
            "event": "refund.processed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_part_dup_500",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 50000,
                        "notes": {"order_number": order.order_number},
                    }
                }
            }
        })

        # Delivery 1
        res1 = self.client.post(
            reverse("core:razorpay_webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res1.status_code, 200)

        # Delivery 2 (Duplicate)
        res2 = self.client.post(
            reverse("core:razorpay_webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res2.status_code, 200)

        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("500.00"))
        self.assertEqual(RefundRecord.objects.filter(order=order).count(), 1)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_cumulative_partial_refunds_and_full_refund_transition(self, mock_client_cls):
        """Tests ₹500 + ₹300 cumulative = ₹800 (PAID, processing), then remaining ₹1200 transitions payment_status to REFUNDED (processed)."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-CUMULATIVE-01",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )

        def _send_refund_webhook(rfnd_id, amount_paise):
            return self.client.post(
                reverse("core:razorpay_webhook"),
                data=json.dumps({
                    "event": "refund.processed",
                    "payload": {
                        "refund": {
                            "entity": {
                                "id": rfnd_id,
                                "payment_id": order.razorpay_payment_id,
                                "amount": amount_paise,
                                "notes": {"order_number": order.order_number},
                            }
                        }
                    }
                }),
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
            )

        # Step 1: ₹500 refund
        res1 = _send_refund_webhook("rfnd_cumul_1", 50000)
        self.assertEqual(res1.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("500.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

        # Step 2: ₹300 refund (different refund ID)
        res2 = _send_refund_webhook("rfnd_cumul_2", 30000)
        self.assertEqual(res2.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("800.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

        # Step 3: Remaining ₹1200 refund -> full amount reached
        res3 = _send_refund_webhook("rfnd_cumul_3", 120000)
        self.assertEqual(res3.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("2000.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_REFUNDED)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)

    # -------------------------------------------------------------------------
    # 12. Cross-order mismatch protection
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_cross_order_mismatched_payment_id_rejected(self, mock_client_cls):
        """Tests that if notes specify order_number A, but payment_id belongs to a different order, no update occurs."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order_a = self._create_test_order(
            self.customer1,
            order_number="FM-ORDER-A",
            razorpay_payment_id="pay_genuine_order_A",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_PROCESSING,
        )

        # Payload has order_number for Order A, but payment_id for completely different payment
        mismatched_payload = json.dumps({
            "event": "refund.processed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_fake_999",
                        "payment_id": "pay_DIFFERENT_order_B",
                        "amount": 200000,
                        "notes": {"order_number": order_a.order_number},
                    }
                }
            }
        })

        res = self.client.post(
            reverse("core:razorpay_webhook"),
            data=mismatched_payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res.status_code, 200)

        order_a.refresh_from_db()
        # Order A MUST NOT be touched
        self.assertEqual(order_a.refund_amount, Decimal("0.00"))
        self.assertEqual(order_a.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order_a.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)
        self.assertEqual(RefundRecord.objects.filter(order=order_a).count(), 0)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_order_number_fallback_rejected_when_payment_id_empty(self, mock_client_cls):
        """Tests that order_number fallback is rejected if payment_id is empty or unverified."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-EMPTY-PAYID",
            razorpay_payment_id="pay_genuine_order_C",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_PROCESSING,
        )

        # Empty payment_id in webhook payload
        payload_no_payid = json.dumps({
            "event": "refund.processed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_nopay_888",
                        "payment_id": "",  # Empty!
                        "amount": 200000,
                        "notes": {"order_number": order.order_number},
                    }
                }
            }
        })

        res = self.client.post(
            reverse("core:razorpay_webhook"),
            data=payload_no_payid,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res.status_code, 200)

        order.refresh_from_db()
        # Order must not be touched without verified payment identity
        self.assertEqual(order.refund_amount, Decimal("0.00"))
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)

    # -------------------------------------------------------------------------
    # 13. Anti-downgrade protection for processed refunds
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_processed_refund_cannot_be_downgraded_by_created_or_failed(self, mock_client_cls):
        """Tests that out-of-order refund.created or refund.failed cannot downgrade an already processed refund."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-DOWNGRADE-01",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_REFUNDED,
            status=Order.STATUS_CANCELLED,
        )
        order.refund_status = Order.REFUND_STATUS_PROCESSED
        order.razorpay_refund_id = "rfnd_settled_permanent"
        order.refund_amount = Decimal("2000.00")
        order.save()

        # Out-of-order refund.created event
        res_created = self.client.post(
            reverse("core:razorpay_webhook"),
            data=json.dumps({
                "event": "refund.created",
                "payload": {
                    "refund": {
                        "entity": {
                            "id": "rfnd_settled_permanent",
                            "payment_id": order.razorpay_payment_id,
                            "amount": 200000,
                        }
                    }
                }
            }),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_created.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)

        # Spurious refund.failed event
        res_failed = self.client.post(
            reverse("core:razorpay_webhook"),
            data=json.dumps({
                "event": "refund.failed",
                "payload": {
                    "refund": {
                        "entity": {
                            "id": "rfnd_settled_permanent",
                            "payment_id": order.razorpay_payment_id,
                            "error_description": "Network timeout at upstream",
                        }
                    }
                }
            }),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_failed.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)

    # -------------------------------------------------------------------------
    # 14. Unverified pending vouchers protection in refund.created and refund.failed
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_unverified_voucher_not_associated_in_refund_created_and_failed(self, mock_client_cls):
        """Tests that pending records with empty external_reference are NOT mutated without proof in created/failed events."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-UNVERIFIED-VOUCHER",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )

        # Existing pending voucher with no external_reference yet
        pending_voucher = RefundRecord.objects.create(
            refund_reference="REF-PENDING-VOUCHER-01",
            external_reference="",
            order=order,
            requested_amount=Decimal("500.00"),
            approved_amount=Decimal("500.00"),
            currency="INR",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            status=RefundRecord.STATUS_PENDING,
        )

        # 1. refund.created with a different refund ID and NO proof
        created_payload = json.dumps({
            "event": "refund.created",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_unproven_created_111",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 30000,
                        "notes": {},
                        "receipt": "",
                    }
                }
            }
        })
        res_c = self.client.post(
            reverse("core:razorpay_webhook"),
            data=created_payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_c.status_code, 200)

        pending_voucher.refresh_from_db()
        # Pending voucher must remain untouched
        self.assertEqual(pending_voucher.external_reference, "")
        self.assertEqual(pending_voucher.status, RefundRecord.STATUS_PENDING)

        # A separate RefundRecord must have been created for rfnd_unproven_created_111
        created_rec = RefundRecord.objects.filter(external_reference="rfnd_unproven_created_111").first()
        self.assertIsNotNone(created_rec)
        self.assertEqual(created_rec.status, RefundRecord.STATUS_PROCESSING)

        # 2. refund.failed with a different refund ID and NO proof
        failed_payload = json.dumps({
            "event": "refund.failed",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_unproven_failed_222",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 20000,
                        "error_description": "Card issuer declined",
                        "notes": {},
                        "receipt": "",
                    }
                }
            }
        })
        res_f = self.client.post(
            reverse("core:razorpay_webhook"),
            data=failed_payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_f.status_code, 200)

        pending_voucher.refresh_from_db()
        # Pending voucher must STILL remain untouched
        self.assertEqual(pending_voucher.external_reference, "")
        self.assertEqual(pending_voucher.status, RefundRecord.STATUS_PENDING)

        failed_rec = RefundRecord.objects.filter(external_reference="rfnd_unproven_failed_222").first()
        self.assertIsNotNone(failed_rec)
        self.assertEqual(failed_rec.status, RefundRecord.STATUS_FAILED)

        # 3. refund.created with VERIFIED proof matching pending voucher
        proven_created_payload = json.dumps({
            "event": "refund.created",
            "payload": {
                "refund": {
                    "entity": {
                        "id": "rfnd_proven_created_333",
                        "payment_id": order.razorpay_payment_id,
                        "amount": 50000,
                        "receipt": "REF-PENDING-VOUCHER-01",  # Verified match
                    }
                }
            }
        })
        res_proven = self.client.post(
            reverse("core:razorpay_webhook"),
            data=proven_created_payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_proven.status_code, 200)

        pending_voucher.refresh_from_db()
        # Now proven voucher is linked and set to processing
        self.assertEqual(pending_voucher.external_reference, "rfnd_proven_created_333")
        self.assertEqual(pending_voucher.status, RefundRecord.STATUS_PROCESSING)

    # -------------------------------------------------------------------------
    # 15. Two distinct partial refund IDs, duplicate delivery, and cumulative total
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_two_distinct_partial_refunds_duplicate_delivery_and_cumulative_total(self, mock_client_cls):
        """Tests two distinct partial refund IDs with duplicate deliveries, ensuring cumulative total and distinct records."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-TWO-PART-DUP",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )

        def _send(rfnd_id, amount_paise):
            return self.client.post(
                reverse("core:razorpay_webhook"),
                data=json.dumps({
                    "event": "refund.processed",
                    "payload": {
                        "refund": {
                            "entity": {
                                "id": rfnd_id,
                                "payment_id": order.razorpay_payment_id,
                                "amount": amount_paise,
                                "notes": {"order_number": order.order_number},
                            }
                        }
                    }
                }),
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
            )

        # Partial 1: ₹400
        r1 = _send("rfnd_part_alpha_400", 40000)
        self.assertEqual(r1.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("400.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

        # Duplicate Partial 1: ₹400 (MUST NOT double count)
        r1_dup = _send("rfnd_part_alpha_400", 40000)
        self.assertEqual(r1_dup.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("400.00"))

        # Partial 2: ₹600
        r2 = _send("rfnd_part_beta_600", 60000)
        self.assertEqual(r2.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("1000.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

        # Duplicate Partial 2: ₹600 (MUST NOT double count)
        r2_dup = _send("rfnd_part_beta_600", 60000)
        self.assertEqual(r2_dup.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("1000.00"))

        # Verify exactly 2 distinct RefundRecords exist with respective amounts
        records = RefundRecord.objects.filter(order=order)
        self.assertEqual(records.count(), 2)
        rec_alpha = records.get(external_reference="rfnd_part_alpha_400")
        rec_beta = records.get(external_reference="rfnd_part_beta_600")
        self.assertEqual(rec_alpha.approved_amount, Decimal("400.00"))
        self.assertEqual(rec_beta.approved_amount, Decimal("600.00"))
        self.assertEqual(rec_alpha.status, RefundRecord.STATUS_PROCESSED)
        self.assertEqual(rec_beta.status, RefundRecord.STATUS_PROCESSED)

    # -------------------------------------------------------------------------
    # 16. Out-of-order created/failed on partially refunded order anti-downgrade
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_out_of_order_created_and_failed_on_partial_refund_anti_downgrade(self, mock_client_cls):
        """Tests that out-of-order refund.created or refund.failed cannot downgrade an already processed partial refund."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-PART-ANTI-DOWNGRADE",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )

        # Process a partial refund of ₹500
        ref_rec = RefundRecord.objects.create(
            refund_reference="REF-PART-PERM-01",
            external_reference="rfnd_partial_permanent_500",
            order=order,
            requested_amount=Decimal("500.00"),
            approved_amount=Decimal("500.00"),
            currency="INR",
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            status=RefundRecord.STATUS_PROCESSED,
        )
        order.refund_amount = Decimal("500.00")
        order.refund_status = Order.REFUND_STATUS_PROCESSING
        order.razorpay_refund_id = "rfnd_partial_permanent_500"
        order.save()

        # Out-of-order refund.created arrives for this refund ID
        res_created = self.client.post(
            reverse("core:razorpay_webhook"),
            data=json.dumps({
                "event": "refund.created",
                "payload": {
                    "refund": {
                        "entity": {
                            "id": "rfnd_partial_permanent_500",
                            "payment_id": order.razorpay_payment_id,
                            "amount": 50000,
                        }
                    }
                }
            }),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_created.status_code, 200)

        ref_rec.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(ref_rec.status, RefundRecord.STATUS_PROCESSED)
        self.assertEqual(order.refund_amount, Decimal("500.00"))
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

        # Spurious out-of-order refund.failed arrives for this refund ID
        res_failed = self.client.post(
            reverse("core:razorpay_webhook"),
            data=json.dumps({
                "event": "refund.failed",
                "payload": {
                    "refund": {
                        "entity": {
                            "id": "rfnd_partial_permanent_500",
                            "payment_id": order.razorpay_payment_id,
                            "error_description": "Network timeout at upstream",
                        }
                    }
                }
            }),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_failed.status_code, 200)

        ref_rec.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(ref_rec.status, RefundRecord.STATUS_PROCESSED)
        self.assertEqual(order.refund_amount, Decimal("500.00"))
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

    # -------------------------------------------------------------------------
    # 17. Stepwise status transitions from partial to full refund
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_stepwise_partial_to_full_refund_status_transitions(self, mock_client_cls):
        """Tests clean step-by-step transition: initial (PAID) -> partial 1 (PAID, processing) -> partial 2 (PAID, processing) -> full (REFUNDED, processed)."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-STEPWISE-TRANSITION",
            total_amount=Decimal("1500.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)

        def _send(rfnd_id, amount_paise):
            return self.client.post(
                reverse("core:razorpay_webhook"),
                data=json.dumps({
                    "event": "refund.processed",
                    "payload": {
                        "refund": {
                            "entity": {
                                "id": rfnd_id,
                                "payment_id": order.razorpay_payment_id,
                                "amount": amount_paise,
                                "notes": {"order_number": order.order_number},
                            }
                        }
                    }
                }),
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
            )

        # Step 1: ₹500 of ₹1500 -> Partial 1
        res1 = _send("rfnd_step_1", 50000)
        self.assertEqual(res1.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("500.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

        # Step 2: Another ₹500 -> ₹1000 of ₹1500 -> Partial 2
        res2 = _send("rfnd_step_2", 50000)
        self.assertEqual(res2.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("1000.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSING)

        # Step 3: Final ₹500 -> ₹1500 of ₹1500 -> Full refund reached!
        res3 = _send("rfnd_step_3", 50000)
        self.assertEqual(res3.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("1500.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_REFUNDED)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)

        # Step 4: Redelivery of Step 3 (duplicate delivery of final refund)
        res4 = _send("rfnd_step_3", 50000)
        self.assertEqual(res4.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("1500.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_REFUNDED)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PROCESSED)
        self.assertEqual(RefundRecord.objects.filter(order=order).count(), 3)

    # -------------------------------------------------------------------------
    # 18. Verified refund ID match rejected if payment_id missing or mismatched
    # -------------------------------------------------------------------------
    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret_xyz",
    )
    @patch("razorpay.Client")
    def test_webhook_refund_id_match_rejected_if_payment_id_missing_or_mismatched(self, mock_client_cls):
        """Tests that even when target order matches via refund ID, missing or mismatched payment_id prevents any update."""
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = self._create_test_order(
            self.customer1,
            order_number="FM-RFNDID-MATCH",
            razorpay_payment_id="pay_genuine_rfnd_match",
            total_amount=Decimal("2000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_CANCELLED,
        )
        order.razorpay_refund_id = "rfnd_known_exact_001"
        order.refund_status = Order.REFUND_STATUS_NOT_APPLICABLE
        order.refund_amount = Decimal("0.00")
        order.save()

        # 1. Missing payment_id in webhook
        res_missing = self.client.post(
            reverse("core:razorpay_webhook"),
            data=json.dumps({
                "event": "refund.processed",
                "payload": {
                    "refund": {
                        "entity": {
                            "id": "rfnd_known_exact_001",
                            "payment_id": "",  # Missing / empty
                            "amount": 200000,
                        }
                    }
                }
            }),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_missing.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("0.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)

        # 2. Mismatched payment_id in webhook
        res_mismatch = self.client.post(
            reverse("core:razorpay_webhook"),
            data=json.dumps({
                "event": "refund.processed",
                "payload": {
                    "refund": {
                        "entity": {
                            "id": "rfnd_known_exact_001",
                            "payment_id": "pay_impostor_999",  # Mismatch!
                            "amount": 200000,
                        }
                    }
                }
            }),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_sig",
        )
        self.assertEqual(res_mismatch.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("0.00"))
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)
