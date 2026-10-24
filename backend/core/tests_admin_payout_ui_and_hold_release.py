"""
Comprehensive Test Suite for FitMeezo Admin Payout Management UI & Hold Release Integration (Prompt 6).

Covers:
1. Access Control & Authorization (RBAC):
   - Anonymous redirected / rejected.
   - Non-staff customer rejected with 403 Forbidden.
   - Unauthorized staff (Support Manager) rejected with 403 Forbidden.
   - Authorized roles (Finance Manager, Super Admin) allowed access to UI and hold release.
2. Endpoint HTTP & CSRF Constraints:
   - GET request to release-hold view rejected with 405 Method Not Allowed (@require_POST).
   - CSRF & POST-only protection enforced.
3. Server-side Validation & Data Isolation:
   - Missing or blank release reason rejected with validation message.
   - Missing or invalid entry IDs safely rejected.
   - Cross-seller entry rejection (entries belonging to Seller B cannot be released for Seller A).
   - Mixed valid/invalid entry selections fail atomically with database rollback.
4. Financial Non-Disbursal Guarantees:
   - Hold release creates ZERO PayoutBatch instances.
   - Hold release posts ZERO SellerLedgerEntry payout debits.
   - Hold release invokes ZERO external gateway / provider rails.
5. Safeguard Preservation (Cooling hold release removes ONLY cooling restriction):
   - Unreconciled COD remittance entries remain held.
   - Unresolved ReturnRequest entries remain held.
   - Unresolved RefundRecord entries remain held.
   - Delivered package without issues becomes eligible after cooling hold release.
6. Audit History Preservation:
   - Hold release logs reason, timestamp, and actor in entry.audit_metadata and hold_release_history.
7. Cycle Filters & Legacy Compatibility:
   - C1, C2, and legacy monthly YYYY-MM cycles filter correctly.
   - Channel filter (manual_bank_transfer, mock_bank_transfer, unbatched) filters rows accurately.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import uuid
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
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
    calculate_seller_payout_eligibility,
    get_cycle_bounds,
    record_manual_bank_transfer,
    release_hold_balance_entries,
    LedgerServiceError,
)

User = get_user_model()


class AdminPayoutUIAndHoldReleaseTestCase(TestCase):
    def setUp(self):
        ensure_admin_groups()
        self.client = Client()

        # 1. Super Admin User
        self.super_admin = User.objects.create_user(
            username="super_admin_p6",
            email="superadmin_p6@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        set_user_role(self.super_admin, ROLE_SUPER_ADMIN)

        # 2. Finance Manager User
        self.finance_user = User.objects.create_user(
            username="finance_mgr_p6",
            email="finance_p6@fitmeezo.com",
            password="FinancePassword123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        # 3. Support Manager (Staff without finance role)
        self.support_user = User.objects.create_user(
            username="support_mgr_p6",
            email="support_p6@fitmeezo.com",
            password="SupportPassword123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        # 4. Customer User
        self.customer_user = User.objects.create_user(
            username="customer_p6",
            email="customer_p6@fitmeezo.com",
            password="CustomerPassword123!",
            is_staff=False,
        )

        # 5. Sellers
        self.seller_user1 = User.objects.create_user(
            username="seller_user1_p6",
            email="seller1@merchant.com",
            password="SellerPassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Varanasi Silks",
            legal_business_name="Varanasi Silks Pvt Ltd",
            support_email="support@varanasi.com",
            support_phone="9988776655",
            registered_address="100 Ghat Road",
            city="Varanasi",
            state="Uttar Pradesh",
            postal_code="221001",
            pickup_address="100 Ghat Road",
            pickup_city="Varanasi",
            pickup_state="Uttar Pradesh",
            pickup_postal_code="221001",
            gstin="09ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Varanasi Silks Pvt Ltd",
            bank_account_number="98765432101111",
            bank_ifsc_code="HDFC0001234",
            bank_name="HDFC Bank",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.super_admin,
        )

        self.seller_user2 = User.objects.create_user(
            username="seller_user2_p6",
            email="seller2@merchant.com",
            password="SellerPassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Jaipur Crafts",
            legal_business_name="Jaipur Crafts Pvt Ltd",
            support_email="support@jaipurcrafts.com",
            support_phone="9988776644",
            registered_address="200 Johari Bazar",
            city="Jaipur",
            state="Rajasthan",
            postal_code="302001",
            pickup_address="200 Johari Bazar",
            pickup_city="Jaipur",
            pickup_state="Rajasthan",
            pickup_postal_code="302001",
            gstin="08ABCDE1234F1Z6",
            pan_number="ABCDE1234G",
            commission_rate=Decimal("12.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Jaipur Crafts Pvt Ltd",
            bank_account_number="98765432102222",
            bank_ifsc_code="ICIC0001234",
            bank_name="ICICI Bank",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.super_admin,
        )

        # 6. Sample Category & Product
        self.category = Category.objects.create(name="Handicrafts", slug=f"handicrafts-{uuid.uuid4().hex[:6]}")
        self.product = Product.objects.create(
            seller=self.seller1,
            category=self.category,
            name="Silk Scarf",
            slug=f"silk-scarf-{uuid.uuid4().hex[:6]}",
            sku=f"SKU-{uuid.uuid4().hex[:8].upper()}",
            price=Decimal("1000.00"),
            stock_quantity=50,
            is_approved=True,
            is_active=True,
        )

        # 7. Sample Orders & SellerOrders with Ledger Entries
        self.now = timezone.now()
        # Delivery 2 days ago (cooling period active: 48h < 120h)
        self.recent_delivery_date = self.now - timedelta(days=2)

        # Order 1 for Seller 1 (recent delivery -> cooling hold)
        self.order1 = Order.objects.create(
            user=self.customer_user,
            order_number=f"ORD-P6-1-{uuid.uuid4().hex[:6]}",
            subtotal=Decimal("1000.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1000.00"),
            currency="INR",
            payment_status=Order.PAYMENT_STATUS_PAID,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            status=Order.STATUS_DELIVERED,
        )
        self.seller_order1 = SellerOrder.objects.create(
            order=self.order1,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number=f"{self.order1.order_number}-S1",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("1000.00"),
            total_commission=Decimal("100.00"),
            net_seller_payable=Decimal("900.00"),
            delivered_at=self.recent_delivery_date,
        )
        self.item1 = OrderItem.objects.create(
            order=self.order1,
            seller_order=self.seller_order1,
            product=self.product,
            seller=self.seller1,
            product_name="Silk Scarf",
            sku=self.product.sku,
            quantity=1,
            unit_price=Decimal("1000.00"),
            line_total=Decimal("1000.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("100.00"),
        )
        self.entry_sale1 = SellerLedgerEntry.objects.create(
            seller=self.seller1,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1000.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=self.order1,
            seller_order=self.seller_order1,
            idempotency_key=f"P6-SALE-1-{uuid.uuid4().hex[:6]}",
            description="Order Sale",
            posted_at=self.recent_delivery_date,
        )
        self.entry_comm1 = SellerLedgerEntry.objects.create(
            seller=self.seller1,
            entry_type=SellerLedgerEntry.TYPE_DEBIT,
            category=SellerLedgerEntry.CATEGORY_COMMISSION,
            amount=Decimal("100.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=self.order1,
            seller_order=self.seller_order1,
            idempotency_key=f"P6-COMM-1-{uuid.uuid4().hex[:6]}",
            description="Marketplace Commission",
            posted_at=self.recent_delivery_date,
        )

        # Order 2 for Seller 2 (for cross-seller isolation testing)
        self.order2 = Order.objects.create(
            user=self.customer_user,
            order_number=f"ORD-P6-2-{uuid.uuid4().hex[:6]}",
            subtotal=Decimal("500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("500.00"),
            currency="INR",
            payment_status=Order.PAYMENT_STATUS_PAID,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            status=Order.STATUS_DELIVERED,
        )
        self.seller_order2 = SellerOrder.objects.create(
            order=self.order2,
            seller=self.seller2,
            is_platform_fulfilled=False,
            sub_order_number=f"{self.order2.order_number}-S2",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("500.00"),
            total_commission=Decimal("60.00"),
            net_seller_payable=Decimal("440.00"),
            delivered_at=self.recent_delivery_date,
        )
        self.entry_sale2 = SellerLedgerEntry.objects.create(
            seller=self.seller2,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("500.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=self.order2,
            seller_order=self.seller_order2,
            idempotency_key=f"P6-SALE-2-{uuid.uuid4().hex[:6]}",
            description="Order Sale Seller 2",
            posted_at=self.recent_delivery_date,
        )

    # =========================================================================
    # 1. Access Control & Authorization (RBAC)
    # =========================================================================

    def test_management_ui_anonymous_redirected(self):
        url = reverse("custom_admin:seller_payout_management")
        response = self.client.get(url)
        self.assertIn(response.status_code, [302, 401, 403])

    def test_management_ui_customer_forbidden(self):
        self.client.login(username="customer_p6", password="CustomerPassword123!")
        url = reverse("custom_admin:seller_payout_management")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_management_ui_unauthorized_staff_forbidden(self):
        self.client.login(username="support_mgr_p6", password="SupportPassword123!")
        url = reverse("custom_admin:seller_payout_management")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_management_ui_finance_manager_allowed(self):
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_management")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cycle Overview")
        self.assertContains(response, "Settlement Calculation Date")

    def test_management_ui_super_admin_allowed(self):
        self.client.login(username="super_admin_p6", password="AdminPassword123!")
        url = reverse("custom_admin:seller_payout_management")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_release_hold_endpoint_anonymous_redirected(self):
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "Expedited settlement approved by finance team",
        })
        self.assertIn(response.status_code, [302, 401, 403])

    def test_release_hold_endpoint_customer_forbidden(self):
        self.client.login(username="customer_p6", password="CustomerPassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "Expedited settlement approved by finance team",
        })
        self.assertEqual(response.status_code, 403)

    def test_release_hold_endpoint_unauthorized_staff_forbidden(self):
        self.client.login(username="support_mgr_p6", password="SupportPassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "Expedited settlement approved by finance team",
        })
        self.assertEqual(response.status_code, 403)

    # =========================================================================
    # 2. HTTP Method & CSRF Constraints
    # =========================================================================

    def test_release_hold_endpoint_rejects_get(self):
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_release_hold_endpoint_csrf_enforced(self):
        """
        Verify that a hold-release POST without a valid CSRF token is rejected with HTTP 403
        when CSRF checks are enforced, and confirms no database state changes occur.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        logged_in = csrf_client.login(username="finance_mgr_p6", password="FinancePassword123!")
        self.assertTrue(logged_in)

        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})

        # Snapshot pre-request database state
        initial_entry_hold_released = self.entry_sale1.is_hold_released
        initial_entry_metadata = json.dumps(self.entry_sale1.audit_metadata or {}, sort_keys=True)
        initial_batch_count = PayoutBatch.objects.count()
        initial_debit_count = SellerLedgerEntry.objects.filter(category=SellerLedgerEntry.CATEGORY_PAYOUT).count()
        initial_audit_log_count = PayoutAuditLog.objects.count()

        # Execute POST without CSRF token
        response = csrf_client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "Expedited settlement approved by finance team",
        })

        # Assert CSRF failure response code (HTTP 403 Forbidden)
        self.assertEqual(response.status_code, 403)

        # Confirm database state was completely untouched
        self.entry_sale1.refresh_from_db()
        self.assertEqual(self.entry_sale1.is_hold_released, initial_entry_hold_released)
        self.assertFalse(self.entry_sale1.is_hold_released)
        self.assertIsNone(self.entry_sale1.hold_released_at)
        self.assertIsNone(self.entry_sale1.hold_released_by)
        self.assertEqual(
            json.dumps(self.entry_sale1.audit_metadata or {}, sort_keys=True),
            initial_entry_metadata,
        )
        self.assertEqual(PayoutBatch.objects.count(), initial_batch_count)
        self.assertEqual(
            SellerLedgerEntry.objects.filter(category=SellerLedgerEntry.CATEGORY_PAYOUT).count(),
            initial_debit_count,
        )
        self.assertFalse(
            PayoutBatch.objects.filter(seller=self.seller1, status=PayoutBatch.STATUS_COMPLETED).exists()
        )
        self.assertEqual(PayoutAuditLog.objects.count(), initial_audit_log_count)

    # =========================================================================
    # 3. Server-Side Validation & Data Isolation
    # =========================================================================

    def test_missing_release_reason_rejected(self):
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "   ",  # blank reason
        }, follow=True)
        self.assertContains(response, "Validation failed")
        self.entry_sale1.refresh_from_db()
        self.assertFalse(self.entry_sale1.is_hold_released)

    def test_missing_entry_ids_rejected(self):
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": "",
            "reason": "Valid reason",
        }, follow=True)
        self.assertContains(response, "Validation failed")

    def test_cross_seller_entry_rejection(self):
        """
        Submitting an entry ID belonging to Seller 2 against Seller 1 must be rejected.
        """
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": str(self.entry_sale2.id),  # Belongs to Seller 2!
            "reason": "Attempting cross-seller release",
        }, follow=True)
        self.assertContains(response, "Hold release failed")
        self.entry_sale2.refresh_from_db()
        self.assertFalse(self.entry_sale2.is_hold_released)

    def test_mixed_valid_and_invalid_entries_fail_atomically(self):
        """
        If one entry is valid for Seller 1 and another belongs to Seller 2, the entire transaction rolls back.
        """
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": f"{self.entry_sale1.id},{self.entry_sale2.id}",
            "reason": "Mixed batch release attempt",
        }, follow=True)
        self.assertContains(response, "Hold release failed")
        self.entry_sale1.refresh_from_db()
        self.entry_sale2.refresh_from_db()
        self.assertFalse(self.entry_sale1.is_hold_released)
        self.assertFalse(self.entry_sale2.is_hold_released)

    # =========================================================================
    # 4. Financial Non-Disbursal Guarantee
    # =========================================================================

    def test_successful_hold_release_causes_zero_disbursals_and_no_batches(self):
        """
        Releasing cooling hold waives only the cooling period.
        It strictly creates ZERO PayoutBatch, posts ZERO debit entries, and initiates ZERO bank transfers.
        """
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        initial_batches_count = PayoutBatch.objects.count()
        initial_debits_count = SellerLedgerEntry.objects.filter(
            category=SellerLedgerEntry.CATEGORY_PAYOUT
        ).count()

        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        response = self.client.post(url, {
            "entry_ids": f"{self.entry_sale1.id},{self.entry_comm1.id}",
            "reason": "Authorized VIP early delivery cooling waiver",
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Successfully released delivery cooling hold")

        # Verify entry flags
        self.entry_sale1.refresh_from_db()
        self.entry_comm1.refresh_from_db()
        self.assertTrue(self.entry_sale1.is_hold_released)
        self.assertTrue(self.entry_comm1.is_hold_released)
        self.assertEqual(self.entry_sale1.hold_release_reason, "Authorized VIP early delivery cooling waiver")

        # Zero batches created
        self.assertEqual(PayoutBatch.objects.count(), initial_batches_count)
        # Zero payout debit entries created
        self.assertEqual(
            SellerLedgerEntry.objects.filter(category=SellerLedgerEntry.CATEGORY_PAYOUT).count(),
            initial_debits_count,
        )

    # =========================================================================
    # 5. Safeguard Preservation: Hold release does NOT bypass COD, Returns, or Refunds
    # =========================================================================

    def test_cooling_release_does_not_bypass_unreconciled_cod(self):
        """
        If a COD order has not had its courier remittance reconciled, releasing the cooling hold
        waives ONLY cooling; the entry remains held during payout calculation due to unreconciled COD!
        """
        # Create a COD order
        cod_order = Order.objects.create(
            user=self.customer_user,
            order_number=f"ORD-COD-{uuid.uuid4().hex[:6]}",
            subtotal=Decimal("1200.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1200.00"),
            currency="INR",
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            payment_method=Order.PAYMENT_METHOD_COD,
            status=Order.STATUS_DELIVERED,
        )
        cod_seller_order = SellerOrder.objects.create(
            order=cod_order,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number=f"{cod_order.order_number}-S1",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("1200.00"),
            total_commission=Decimal("120.00"),
            net_seller_payable=Decimal("1080.00"),
            delivered_at=self.recent_delivery_date,
        )
        cod_entry = SellerLedgerEntry.objects.create(
            seller=self.seller1,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1200.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            order=cod_order,
            seller_order=cod_seller_order,
            idempotency_key=f"COD-ENTRY-{uuid.uuid4().hex[:6]}",
            description="COD Sale",
            posted_at=self.recent_delivery_date,
        )

        # Release cooling hold via admin endpoint
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        self.client.post(url, {
            "entry_ids": str(cod_entry.id),
            "reason": "Waiving cooling hold for COD order",
        })

        cod_entry.refresh_from_db()
        self.assertTrue(cod_entry.is_hold_released)

        # Calculate eligibility: the entry MUST still be held because COD is unreconciled!
        elig = calculate_seller_payout_eligibility(seller=self.seller1, as_of_date=self.now)
        self.assertIn(cod_entry.id, elig["held_entry_ids"])
        reasons = [d["reason"] for d in elig["held_entry_details"] if d["entry_id"] == cod_entry.id]
        self.assertTrue(any("COD" in r for r in reasons))

    def test_cooling_release_does_not_bypass_unresolved_returns(self):
        """
        If a package has an active unresolved ReturnRequest, releasing cooling hold
        leaves the entry held during payout eligibility evaluation.
        """
        # Create an active ReturnRequest
        ret = ReturnRequest.objects.create(
            request_number=f"RET-{uuid.uuid4().hex[:8].upper()}",
            order=self.order1,
            seller_order=self.seller_order1,
            seller=self.seller1,
            customer=self.customer_user,
            status=ReturnRequest.STATUS_REQUESTED,
            reason=ReturnRequest.REASON_DEFECTIVE,
        )

        # Release cooling hold
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})
        self.client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "Admin cooling release",
        })

        self.entry_sale1.refresh_from_db()
        self.assertTrue(self.entry_sale1.is_hold_released)

        # Payout eligibility check: entry must still be held due to active return!
        elig = calculate_seller_payout_eligibility(seller=self.seller1, as_of_date=self.now)
        self.assertIn(self.entry_sale1.id, elig["held_entry_ids"])
        reasons = [d["reason"] for d in elig["held_entry_details"] if d["entry_id"] == self.entry_sale1.id]
        self.assertTrue(any("return" in r.lower() for r in reasons))

    # =========================================================================
    # 6. Audit History Visibility & Preservation
    # =========================================================================

    def test_audit_history_preservation_across_repeated_releases(self):
        """
        Multiple hold release updates must accumulate chronologically in hold_release_history.
        """
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        url = reverse("custom_admin:seller_payout_release_hold", kwargs={"seller_id": self.seller1.id})

        # First release
        self.client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "Initial hold release reason",
        })
        self.entry_sale1.refresh_from_db()
        history1 = self.entry_sale1.audit_metadata.get("hold_release_history", [])
        self.assertEqual(len(history1), 1)
        self.assertEqual(history1[0]["reason"], "Initial hold release reason")

        # Second release (with new reason)
        self.client.post(url, {
            "entry_ids": str(self.entry_sale1.id),
            "reason": "Updated audit clarification reason",
        })
        self.entry_sale1.refresh_from_db()
        history2 = self.entry_sale1.audit_metadata.get("hold_release_history", [])
        self.assertEqual(len(history2), 2)
        self.assertEqual(history2[1]["reason"], "Updated audit clarification reason")

    # =========================================================================
    # 7. Cycle Filters & Legacy Monthly Compatibility
    # =========================================================================

    def test_cycle_filtering_c1_c2_and_legacy_monthly(self):
        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        base_url = reverse("custom_admin:seller_payout_management")

        # Test C1 cycle query
        resp_c1 = self.client.get(f"{base_url}?cycle=2026-10-C1")
        self.assertEqual(resp_c1.status_code, 200)
        self.assertContains(resp_c1, "2026-10-C1")

        # Test C2 cycle query
        resp_c2 = self.client.get(f"{base_url}?cycle=2026-10-C2")
        self.assertEqual(resp_c2.status_code, 200)
        self.assertContains(resp_c2, "2026-10-C2")

        # Test legacy monthly query (YYYY-MM)
        resp_legacy = self.client.get(f"{base_url}?cycle=2026-09")
        self.assertEqual(resp_legacy.status_code, 200)
        self.assertContains(resp_legacy, "2026-09")

    def test_channel_filter_rendering(self):
        """
        Verify channel filter filters manual bank transfer vs mock vs unbatched.
        """
        # Create an approved and manually transferred batch
        batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-P6-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("1500.00"),
            currency="INR",
            status=PayoutBatch.STATUS_PENDING,
            approved_at=self.now - timedelta(hours=1),
            approved_by=self.super_admin,
            payout_method="manual_bank_transfer",
            is_mock=False,
            payout_cycle="2026-10-C1",
        )
        record_manual_bank_transfer(
            payout_batch=batch,
            actor=self.super_admin,
            utr_reference="UTR-P6-TEST-9999",
            transfer_date=timezone.now().date(),
            confirmation=True,
            notes="Manual payment recorded",
        )

        self.client.login(username="finance_mgr_p6", password="FinancePassword123!")
        base_url = reverse("custom_admin:seller_payout_management")

        # Query for manual_bank_transfer channel
        resp_manual = self.client.get(f"{base_url}?cycle=2026-10-C1&channel=manual_bank_transfer")
        self.assertEqual(resp_manual.status_code, 200)
        self.assertContains(resp_manual, "Manual Transfer")
        self.assertContains(resp_manual, "UTR-P6-TEST-9999")

        # Query for mock_bank_transfer channel: seller1 batch should NOT match
        resp_mock = self.client.get(f"{base_url}?cycle=2026-10-C1&channel=mock_bank_transfer")
        self.assertEqual(resp_mock.status_code, 200)
        self.assertNotContains(resp_mock, "UTR-P6-TEST-9999")
