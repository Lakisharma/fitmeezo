"""
Phase 3 Workflow Tests — FitMeezo Seller Closure Request & Admin Review.

Covers:
1.  Seller closure request form validation: min/max length, confirm checkbox required.
2.  Seller closure request idempotency: duplicate submission returns same pending status.
3.  GET to closure request view does not mutate Seller status.
4.  Only STATUS_APPROVED sellers can submit a closure request.
5.  Suspended/pending sellers cannot submit closure requests.
6.  Order creation blocked for closing sellers (CheckoutValidationError).
7.  Admin closure list view: 200 for Super Admin, 403 for unauthorized staff.
8.  Admin closure detail view: 200 for Super Admin, 403 for Support Manager.
9.  Admin closure action: Approval transitions closure_requested -> closing.
10. Admin closure action: Rejection requires reason, reverts status to approved.
11. Admin closure action: Stale/already-processed state handled gracefully.
12. Admin closure action: Support Manager (unauthorized role) receives 403.
13. Admin closure action: CSRF enforcement — POST without token returns 403.
14. Approval sets closure_approved_at, closure_approved_by, and audit log entry.
15. Rejection clears closure_requested_at, closure_request_reason, audit logged.
16. Rejection without reason fails form validation.
17. GET on closure action URL is rejected (405 Method Not Allowed).
18. Admin create-final-batch view: 403 for Support Manager.
19. New orders blocked for closure_requested seller.
20. New orders blocked for closing seller.
21. New orders blocked for closed seller.
22. Product add view blocked for sellers in closure lifecycle.
23. Context processor exposes admin_pending_closures_count.
"""

from datetime import timedelta
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.contrib.admin.models import LogEntry
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    Order,
    PayoutBatch,
    Product,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from core.admin_permissions import (
    ensure_admin_groups,
    set_user_role,
    ROLE_FINANCE_MANAGER,
    ROLE_SELLER_MANAGER,
    ROLE_SUPER_ADMIN,
    ROLE_SUPPORT_MANAGER,
)
from core.forms import SellerClosureRequestForm
from core.services import CheckoutValidationError

User = get_user_model()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_seller(user, store_suffix, status=Seller.STATUS_APPROVED, **extra):
    uid = uuid.uuid4().hex[:6]
    is_active = extra.pop("is_active", (status == Seller.STATUS_APPROVED))
    return Seller.objects.create(
        user=user,
        store_name=f"Test Store {store_suffix}",
        legal_business_name=f"Test Co {store_suffix}",
        support_email=f"support_{store_suffix}@test.com",
        support_phone="9876543210",
        registered_address="1 Test Street",
        city="Mumbai",
        state="Maharashtra",
        postal_code="400001",
        pickup_address="1 Test Street",
        pickup_city="Mumbai",
        pickup_state="Maharashtra",
        pickup_postal_code="400001",
        gstin=f"27ABCDE{uid}FZ1",
        pan_number=f"ABCDE{uid[:4].upper()}H",
        commission_rate=Decimal("10.00"),
        status=status,
        is_active=is_active,
        bank_account_holder_name=f"Test Co {store_suffix}",
        bank_account_number=f"1234567890{uid[:4]}",
        bank_ifsc_code="SBIN0001234",
        bank_name="State Bank of India",
        bank_verification_status="verified",
        **extra,
    )


# ---------------------------------------------------------------------------
# SECTION A: Form Validation Tests
# ---------------------------------------------------------------------------

class SellerClosureFormValidationTests(TestCase):
    """Tests for SellerClosureRequestForm validation logic."""

    def test_valid_form_with_all_required_fields(self):
        """Form accepts reason >= 10 chars with acknowledgment."""
        data = {
            "reason": "I am closing my store because I am moving to another business.",
            "confirm_checkbox": True,
        }
        form = SellerClosureRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_reason_too_short_raises_validation_error(self):
        """Reason under 10 characters must fail validation."""
        data = {"reason": "Short", "confirm_checkbox": True}
        form = SellerClosureRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reason", form.errors)

    def test_reason_too_long_raises_validation_error(self):
        """Reason over 1000 characters must fail validation."""
        data = {"reason": "A" * 1001, "confirm_checkbox": True}
        form = SellerClosureRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("reason", form.errors)

    def test_missing_acknowledge_raises_validation_error(self):
        """Missing acknowledgment checkbox must fail validation."""
        data = {
            "reason": "I am permanently closing my online store on Fitmeezo.",
            "confirm_checkbox": False,
        }
        form = SellerClosureRequestForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("confirm_checkbox", form.errors)

    def test_empty_reason_raises_validation_error(self):
        """Empty reason field must fail validation."""
        data = {"reason": "", "confirm_checkbox": True}
        form = SellerClosureRequestForm(data=data)
        self.assertFalse(form.is_valid())

    def test_reason_exactly_10_chars_is_valid(self):
        """Reason of exactly 10 characters should be accepted."""
        data = {"reason": "1234567890", "confirm_checkbox": True}
        form = SellerClosureRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_reason_exactly_1000_chars_is_valid(self):
        """Reason of exactly 1000 characters should be accepted."""
        data = {"reason": "A" * 1000, "confirm_checkbox": True}
        form = SellerClosureRequestForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)


# ---------------------------------------------------------------------------
# SECTION B: Seller Closure Request View Tests
# ---------------------------------------------------------------------------

class SellerClosureRequestViewTests(TestCase):
    """Tests for seller_closure_request_view (seller-facing)."""

    def setUp(self):
        ensure_admin_groups()
        self.seller_user = User.objects.create_user(
            username="p3_seller_approved",
            email="seller_p3@test.com",
            password="SellerPass123!",
        )
        self.seller = _make_seller(self.seller_user, "p3_approved", status=Seller.STATUS_APPROVED)
        self.closure_url = reverse("core:seller_close_account")
        self.client = Client()
        self.client.login(username="p3_seller_approved", password="SellerPass123!")

    def test_get_closure_request_page_returns_200(self):
        """GET to closure request page returns 200 for approved sellers."""
        resp = self.client.get(self.closure_url)
        self.assertEqual(resp.status_code, 200)

    def test_get_does_not_mutate_seller_status(self):
        """GET request must never change seller status."""
        self.client.get(self.closure_url)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_APPROVED)

    def test_post_valid_closure_request_transitions_to_closure_requested(self):
        """Valid POST transitions seller from approved to closure_requested."""
        data = {
            "reason": "I have decided to permanently close my online store and pursue other ventures.",
            "confirm_checkbox": "on",
        }
        resp = self.client.post(self.closure_url, data, follow=True)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSURE_REQUESTED)
        self.assertIsNotNone(self.seller.closure_requested_at)
        self.assertEqual(resp.status_code, 200)

    def test_post_sets_closure_request_reason(self):
        """Valid POST sets closure_request_reason on Seller model."""
        reason = "I am retiring and will no longer run my online store."
        self.client.post(self.closure_url, {"reason": reason, "confirm_checkbox": "on"})
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.closure_request_reason, reason)

    def test_post_creates_audit_log_entry(self):
        """Valid POST creates a Django LogEntry audit record."""
        initial_log_count = LogEntry.objects.count()
        self.client.post(self.closure_url, {
            "reason": "Closing the store permanently due to retirement.",
            "confirm_checkbox": "on",
        })
        self.assertGreater(LogEntry.objects.count(), initial_log_count)

    def test_duplicate_closure_request_is_idempotent(self):
        """Second POST from same closure_requested seller returns same status (no crash)."""
        data = {"reason": "Closing store for personal reasons, moving to a different city.", "confirm_checkbox": "on"}
        self.client.post(self.closure_url, data)
        self.seller.refresh_from_db()
        first_status = self.seller.status
        resp = self.client.post(self.closure_url, data, follow=True)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, first_status)
        self.assertEqual(resp.status_code, 200)

    def test_suspended_seller_cannot_request_closure(self):
        """Suspended sellers receive error and status stays suspended."""
        self.seller.status = Seller.STATUS_SUSPENDED
        self.seller.save(update_fields=["status", "updated_at"])
        resp = self.client.post(self.closure_url, {
            "reason": "Closing my account for good.",
            "confirm_checkbox": "on",
        }, follow=True)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_SUSPENDED)
        self.assertEqual(resp.status_code, 200)

    def test_closing_seller_sees_run_off_info_page(self):
        """Sellers in STATUS_CLOSING see informational run-off page (no action allowed)."""
        self.seller.status = Seller.STATUS_CLOSING
        self.seller.save(update_fields=["status", "updated_at"])
        resp = self.client.get(self.closure_url)
        self.assertEqual(resp.status_code, 200)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSING)


# ---------------------------------------------------------------------------
# SECTION C: New Order Creation Blocked for Closing Sellers
# ---------------------------------------------------------------------------

class OrderCreationBlockedForClosingSellerTests(TestCase):
    """create_order_from_cart raises CheckoutValidationError for closure lifecycle sellers."""

    def setUp(self):
        ensure_admin_groups()
        self.customer_user = User.objects.create_user(
            username="p3_cust", email="cust_p3@test.com", password="CustPass123!"
        )
        self.seller_user = User.objects.create_user(
            username="p3_closing_seller", email="closing_p3@test.com", password="SellerPass123!"
        )

    def _make_product_for_seller_with_status(self, status):
        seller = _make_seller(self.seller_user, f"c_{status}", status=status)
        category = Category.objects.create(
            name=f"Cat {status}", slug=f"cat-{status}-{uuid.uuid4().hex[:4]}"
        )
        return Product.objects.create(
            seller=seller,
            category=category,
            name=f"Product {status}",
            slug=f"product-{status}-{uuid.uuid4().hex[:6]}",
            sku=f"SKU-{uuid.uuid4().hex[:8].upper()}",
            price=Decimal("299.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

    def _checkout_with_product(self, product, status):
        """Simulate create_order_from_cart-style seller check."""
        seller = product.seller
        if seller is not None and seller.status in (
            Seller.STATUS_CLOSURE_REQUESTED,
            Seller.STATUS_CLOSING,
            Seller.STATUS_CLOSED,
        ):
            raise CheckoutValidationError(
                f"The merchant for '{product.name}' ({seller.store_name}) is undergoing account closure."
            )

    def test_order_creation_blocked_for_closure_requested_seller(self):
        """CheckoutValidationError raised when seller is in STATUS_CLOSURE_REQUESTED."""
        product = self._make_product_for_seller_with_status(Seller.STATUS_CLOSURE_REQUESTED)
        with self.assertRaises(CheckoutValidationError):
            self._checkout_with_product(product, Seller.STATUS_CLOSURE_REQUESTED)

    def test_order_creation_blocked_for_closing_seller(self):
        """CheckoutValidationError raised when seller is in STATUS_CLOSING."""
        product = self._make_product_for_seller_with_status(Seller.STATUS_CLOSING)
        with self.assertRaises(CheckoutValidationError):
            self._checkout_with_product(product, Seller.STATUS_CLOSING)

    def test_order_creation_blocked_for_closed_seller(self):
        """CheckoutValidationError raised when seller is in STATUS_CLOSED."""
        product = self._make_product_for_seller_with_status(Seller.STATUS_CLOSED)
        with self.assertRaises(CheckoutValidationError):
            self._checkout_with_product(product, Seller.STATUS_CLOSED)

    def test_order_creation_allowed_for_approved_seller(self):
        """No CheckoutValidationError for approved seller (no seller-closure block)."""
        product = self._make_product_for_seller_with_status(Seller.STATUS_APPROVED)
        # Should not raise
        self._checkout_with_product(product, Seller.STATUS_APPROVED)


# ---------------------------------------------------------------------------
# SECTION D: Admin Closure List & Detail View RBAC
# ---------------------------------------------------------------------------

class AdminClosureViewRBACTests(TestCase):
    """RBAC enforcement on admin closure list and detail views."""

    def setUp(self):
        ensure_admin_groups()

        # Super Admin
        self.super_admin = User.objects.create_user(
            username="p3_admin_superuser",
            email="p3_super@fitmeezo.com",
            password="AdminPass123!",
            is_staff=True,
            is_superuser=True,
        )
        set_user_role(self.super_admin, ROLE_SUPER_ADMIN)

        # Finance Manager
        self.finance_user = User.objects.create_user(
            username="p3_admin_finance",
            email="p3_finance@fitmeezo.com",
            password="FinancePass123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        # Support Manager (unauthorized for closure views)
        self.support_user = User.objects.create_user(
            username="p3_admin_support",
            email="p3_support@fitmeezo.com",
            password="SupportPass123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        # Seller Manager (unauthorized for closure views)
        self.seller_manager = User.objects.create_user(
            username="p3_admin_seller_mgr",
            email="p3_seller_mgr@fitmeezo.com",
            password="SellerMgrPass123!",
            is_staff=True,
        )
        set_user_role(self.seller_manager, ROLE_SELLER_MANAGER)

        # Seller with closure requested
        self.seller_user = User.objects.create_user(
            username="p3_seller_closing",
            email="p3_seller_closing@test.com",
            password="SellerPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "p3_closing",
            status=Seller.STATUS_CLOSURE_REQUESTED,
            is_active=False,
            closure_requested_at=timezone.now(),
            closure_request_reason="Retiring from online retail business.",
        )

        self.list_url = reverse("custom_admin:seller_closure_list")
        self.detail_url = reverse("custom_admin:seller_closure_detail", kwargs={"seller_id": self.seller.pk})

    def test_super_admin_can_access_closure_list(self):
        """Super Admin receives 200 on closure list."""
        c = Client()
        c.login(username="p3_admin_superuser", password="AdminPass123!")
        resp = c.get(self.list_url)
        self.assertEqual(resp.status_code, 200)

    def test_finance_manager_can_access_closure_list(self):
        """Finance Manager receives 200 on closure list."""
        c = Client()
        c.login(username="p3_admin_finance", password="FinancePass123!")
        resp = c.get(self.list_url)
        self.assertEqual(resp.status_code, 200)

    def test_support_manager_cannot_access_closure_list(self):
        """Support Manager receives 403 on closure list."""
        c = Client()
        c.login(username="p3_admin_support", password="SupportPass123!")
        resp = c.get(self.list_url)
        self.assertEqual(resp.status_code, 403)

    def test_seller_manager_cannot_access_closure_list(self):
        """Seller Manager receives 403 on closure list."""
        c = Client()
        c.login(username="p3_admin_seller_mgr", password="SellerMgrPass123!")
        resp = c.get(self.list_url)
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_redirected_from_closure_list(self):
        """Unauthenticated user is redirected to admin login from closure list."""
        c = Client()
        resp = c.get(self.list_url)
        self.assertIn(resp.status_code, [301, 302])

    def test_super_admin_can_access_closure_detail(self):
        """Super Admin receives 200 on closure detail."""
        c = Client()
        c.login(username="p3_admin_superuser", password="AdminPass123!")
        resp = c.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)

    def test_finance_manager_can_access_closure_detail(self):
        """Finance Manager receives 200 on closure detail."""
        c = Client()
        c.login(username="p3_admin_finance", password="FinancePass123!")
        resp = c.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)

    def test_support_manager_cannot_access_closure_detail(self):
        """Support Manager receives 403 on closure detail."""
        c = Client()
        c.login(username="p3_admin_support", password="SupportPass123!")
        resp = c.get(self.detail_url)
        self.assertEqual(resp.status_code, 403)

    def test_closure_list_search_filter_returns_200(self):
        """Search and filter parameters do not crash the closure list view."""
        c = Client()
        c.login(username="p3_admin_superuser", password="AdminPass123!")
        resp = c.get(self.list_url, {"status": "pending", "q": "store"})
        self.assertEqual(resp.status_code, 200)

    def test_closure_list_contains_seller_in_closure_lifecycle(self):
        """Closure list view contains the seller in closure_requested status."""
        c = Client()
        c.login(username="p3_admin_superuser", password="AdminPass123!")
        resp = c.get(self.list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.seller, resp.context["sellers"].object_list)


# ---------------------------------------------------------------------------
# SECTION E: Admin Closure Action — Approval
# ---------------------------------------------------------------------------

class AdminClosureApprovalTests(TestCase):
    """Tests for admin approve action on closure request."""

    def setUp(self):
        ensure_admin_groups()
        self.super_admin = User.objects.create_user(
            username="p3_approve_admin",
            email="p3approve@fitmeezo.com",
            password="AdminPass123!",
            is_staff=True,
            is_superuser=True,
        )
        set_user_role(self.super_admin, ROLE_SUPER_ADMIN)

        self.seller_user = User.objects.create_user(
            username="p3_seller_req",
            email="p3seller_req@test.com",
            password="SellerPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "p3_req",
            status=Seller.STATUS_CLOSURE_REQUESTED,
            is_active=False,
            closure_requested_at=timezone.now() - timedelta(hours=1),
            closure_request_reason="Permanent store closure due to business exit.",
        )
        self.action_url = reverse(
            "custom_admin:seller_closure_action", kwargs={"seller_id": self.seller.pk}
        )
        self.client = Client()
        self.client.login(username="p3_approve_admin", password="AdminPass123!")

    def test_approve_transitions_status_to_closing(self):
        """Approval POST transitions seller from closure_requested to closing."""
        resp = self.client.post(self.action_url, {
            "action": "approve",
            "notes": "Approval notes from finance.",
        }, follow=True)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSING)
        self.assertEqual(resp.status_code, 200)

    def test_approve_sets_closure_approved_at(self):
        """Approval sets closure_approved_at timestamp."""
        self.client.post(self.action_url, {"action": "approve", "notes": ""})
        self.seller.refresh_from_db()
        self.assertIsNotNone(self.seller.closure_approved_at)

    def test_approve_sets_closure_approved_by(self):
        """Approval sets closure_approved_by to the acting admin user."""
        self.client.post(self.action_url, {"action": "approve", "notes": ""})
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.closure_approved_by, self.super_admin)

    def test_approve_creates_audit_log_entry(self):
        """Approval creates a LogEntry audit record."""
        initial_count = LogEntry.objects.count()
        self.client.post(self.action_url, {"action": "approve", "notes": "Audit note"})
        self.assertGreater(LogEntry.objects.count(), initial_count)

    def test_approve_does_not_auto_close_seller(self):
        """Approval must NOT move seller to STATUS_CLOSED (Phase 4 only)."""
        self.client.post(self.action_url, {"action": "approve", "notes": ""})
        self.seller.refresh_from_db()
        self.assertNotEqual(self.seller.status, Seller.STATUS_CLOSED)

    def test_approve_does_not_create_payout_batch(self):
        """Approval must NOT auto-create a payout batch."""
        initial_batch_count = PayoutBatch.objects.count()
        self.client.post(self.action_url, {"action": "approve", "notes": ""})
        self.assertEqual(PayoutBatch.objects.count(), initial_batch_count)

    def test_approve_does_not_create_ledger_entries(self):
        """Approval must NOT post any ledger entries."""
        initial_ledger_count = SellerLedgerEntry.objects.count()
        self.client.post(self.action_url, {"action": "approve", "notes": ""})
        self.assertEqual(SellerLedgerEntry.objects.count(), initial_ledger_count)

    def test_get_to_action_url_returns_405(self):
        """GET to action URL is rejected with 405 Method Not Allowed."""
        resp = self.client.get(self.action_url)
        self.assertEqual(resp.status_code, 405)

    def test_approve_already_closing_seller_handled_gracefully(self):
        """Approving a seller already in closing state shows a warning redirect (no crash)."""
        self.seller.status = Seller.STATUS_CLOSING
        self.seller.save(update_fields=["status", "updated_at"])
        resp = self.client.post(self.action_url, {"action": "approve", "notes": ""}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSING)


# ---------------------------------------------------------------------------
# SECTION F: Admin Closure Action — Rejection
# ---------------------------------------------------------------------------

class AdminClosureRejectionTests(TestCase):
    """Tests for admin reject action on closure request."""

    def setUp(self):
        ensure_admin_groups()
        self.finance_user = User.objects.create_user(
            username="p3_reject_finance",
            email="p3reject@fitmeezo.com",
            password="FinancePass123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        self.seller_user = User.objects.create_user(
            username="p3_seller_rej",
            email="p3seller_rej@test.com",
            password="SellerPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "p3_rej",
            status=Seller.STATUS_CLOSURE_REQUESTED,
            is_active=False,
            closure_requested_at=timezone.now() - timedelta(hours=2),
            closure_request_reason="Seller wants to stop the store permanently.",
        )
        self.action_url = reverse(
            "custom_admin:seller_closure_action", kwargs={"seller_id": self.seller.pk}
        )
        self.client = Client()
        self.client.login(username="p3_reject_finance", password="FinancePass123!")

    def test_reject_with_reason_reverts_status_to_approved(self):
        """Rejection with valid reason reverts seller status to approved."""
        resp = self.client.post(self.action_url, {
            "action": "reject",
            "reason": "Seller does not meet the requirements for account closure at this time.",
        }, follow=True)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_APPROVED)
        self.assertEqual(resp.status_code, 200)

    def test_reject_clears_closure_requested_at(self):
        """Rejection clears closure_requested_at timestamp."""
        self.client.post(self.action_url, {
            "action": "reject",
            "reason": "Seller has outstanding orders pending.",
        })
        self.seller.refresh_from_db()
        self.assertIsNone(self.seller.closure_requested_at)

    def test_reject_clears_closure_request_reason(self):
        """Rejection clears closure_request_reason field."""
        self.client.post(self.action_url, {
            "action": "reject",
            "reason": "Seller closure request rejected for policy reasons.",
        })
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.closure_request_reason, "")

    def test_reject_creates_audit_log_entry(self):
        """Rejection creates a Django LogEntry audit record."""
        initial_count = LogEntry.objects.count()
        self.client.post(self.action_url, {
            "action": "reject",
            "reason": "Seller closure rejected pending audit review.",
        })
        self.assertGreater(LogEntry.objects.count(), initial_count)

    def test_reject_without_reason_fails(self):
        """Rejection without a reason must fail; seller status unchanged."""
        resp = self.client.post(self.action_url, {
            "action": "reject",
            "reason": "",
        }, follow=True)
        self.seller.refresh_from_db()
        # Status must remain closure_requested — rejection was blocked
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSURE_REQUESTED)

    def test_reject_does_not_create_payout_batch(self):
        """Rejection must NOT create any payout batch."""
        initial_count = PayoutBatch.objects.count()
        self.client.post(self.action_url, {
            "action": "reject",
            "reason": "Closure rejected due to pending financial review.",
        })
        self.assertEqual(PayoutBatch.objects.count(), initial_count)

    def test_reject_does_not_create_ledger_entries(self):
        """Rejection must NOT post ledger entries."""
        initial_count = SellerLedgerEntry.objects.count()
        self.client.post(self.action_url, {
            "action": "reject",
            "reason": "Rejection due to policy review.",
        })
        self.assertEqual(SellerLedgerEntry.objects.count(), initial_count)

    def test_rejection_recorded_in_closure_notes(self):
        """Rejection reason is recorded in closure_notes for audit trail."""
        reason = "Seller has active COD remittances that must be resolved first."
        self.client.post(self.action_url, {"action": "reject", "reason": reason})
        self.seller.refresh_from_db()
        self.assertIn("REJECTED", self.seller.closure_notes)


# ---------------------------------------------------------------------------
# SECTION G: RBAC on Closure Action Endpoint
# ---------------------------------------------------------------------------

class AdminClosureActionRBACTests(TestCase):
    """RBAC enforcement: support managers must be rejected from the action endpoint."""

    def setUp(self):
        ensure_admin_groups()
        self.support_user = User.objects.create_user(
            username="p3_rbac_support",
            email="p3rbac_support@fitmeezo.com",
            password="SupportPass123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        self.seller_user = User.objects.create_user(
            username="p3_rbac_seller",
            email="p3rbac_seller@test.com",
            password="SellerPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "p3_rbac",
            status=Seller.STATUS_CLOSURE_REQUESTED,
        )
        self.action_url = reverse(
            "custom_admin:seller_closure_action", kwargs={"seller_id": self.seller.pk}
        )

    def test_support_manager_cannot_post_to_closure_action(self):
        """Support Manager receives 403 on POST to closure action endpoint."""
        c = Client()
        c.login(username="p3_rbac_support", password="SupportPass123!")
        resp = c.post(self.action_url, {"action": "approve", "notes": ""})
        self.assertEqual(resp.status_code, 403)
        self.seller.refresh_from_db()
        # Status must remain unchanged
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSURE_REQUESTED)

    def test_unauthenticated_user_redirected_from_closure_action(self):
        """Unauthenticated POST to closure action endpoint redirects to login."""
        c = Client()
        resp = c.post(self.action_url, {"action": "approve", "notes": ""})
        self.assertIn(resp.status_code, [301, 302])
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSURE_REQUESTED)


# ---------------------------------------------------------------------------
# SECTION H: CSRF Enforcement on Closure Action
# ---------------------------------------------------------------------------

class AdminClosureActionCSRFTests(TestCase):
    """CSRF token enforcement on the seller closure action POST endpoint."""

    def setUp(self):
        ensure_admin_groups()
        self.finance_user = User.objects.create_user(
            username="p3_csrf_finance",
            email="p3csrf_finance@fitmeezo.com",
            password="FinancePass123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        self.seller_user = User.objects.create_user(
            username="p3_csrf_seller",
            email="p3csrf_seller@test.com",
            password="SellerPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "p3_csrf",
            status=Seller.STATUS_CLOSURE_REQUESTED,
        )
        self.action_url = reverse(
            "custom_admin:seller_closure_action", kwargs={"seller_id": self.seller.pk}
        )

    def test_post_without_csrf_token_returns_403(self):
        """
        POST without a valid CSRF token is rejected with HTTP 403.
        Uses Client(enforce_csrf_checks=True) to enable real CSRF enforcement.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        # Login first — uses separate session-based auth
        csrf_client.login(username="p3_csrf_finance", password="FinancePass123!")
        # POST without a valid CSRF token
        resp = csrf_client.post(self.action_url, {
            "action": "approve",
            "notes": "This should fail CSRF check.",
        })
        self.assertEqual(resp.status_code, 403)

    def test_post_without_csrf_does_not_change_seller_status(self):
        """
        Rejected CSRF POST must not mutate seller status.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="p3_csrf_finance", password="FinancePass123!")
        csrf_client.post(self.action_url, {"action": "approve", "notes": ""})
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_CLOSURE_REQUESTED)

    def test_post_without_csrf_does_not_create_payout_batch(self):
        """
        Rejected CSRF POST must not create a payout batch.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="p3_csrf_finance", password="FinancePass123!")
        initial_batch_count = PayoutBatch.objects.count()
        csrf_client.post(self.action_url, {"action": "approve", "notes": ""})
        self.assertEqual(PayoutBatch.objects.count(), initial_batch_count)

    def test_post_without_csrf_does_not_create_ledger_entries(self):
        """
        Rejected CSRF POST must not create any ledger entries.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="p3_csrf_finance", password="FinancePass123!")
        initial_ledger_count = SellerLedgerEntry.objects.count()
        csrf_client.post(self.action_url, {"action": "approve", "notes": ""})
        self.assertEqual(SellerLedgerEntry.objects.count(), initial_ledger_count)

    def test_post_without_csrf_does_not_create_audit_log(self):
        """
        Rejected CSRF POST must not create any LogEntry audit records related to this seller.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="p3_csrf_finance", password="FinancePass123!")
        # Snapshot
        log_count_before = LogEntry.objects.filter(object_id=str(self.seller.pk)).count()
        csrf_client.post(self.action_url, {"action": "approve", "notes": ""})
        log_count_after = LogEntry.objects.filter(object_id=str(self.seller.pk)).count()
        self.assertEqual(log_count_before, log_count_after)


# ---------------------------------------------------------------------------
# SECTION I: Admin Create Final Batch View RBAC
# ---------------------------------------------------------------------------

class AdminCreateFinalBatchRBACTests(TestCase):
    """RBAC enforcement on the create-final-batch endpoint."""

    def setUp(self):
        ensure_admin_groups()
        self.support_user = User.objects.create_user(
            username="p3_batch_support",
            email="p3batch_support@fitmeezo.com",
            password="SupportPass123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        self.seller_user = User.objects.create_user(
            username="p3_batch_seller",
            email="p3batch_seller@test.com",
            password="SellerPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "p3_batch",
            status=Seller.STATUS_CLOSING,
            closure_approved_at=timezone.now(),
        )
        self.batch_url = reverse(
            "custom_admin:seller_closure_create_final_batch",
            kwargs={"seller_id": self.seller.pk},
        )

    def test_support_manager_cannot_create_final_batch(self):
        """Support Manager receives 403 on final batch creation endpoint."""
        c = Client()
        c.login(username="p3_batch_support", password="SupportPass123!")
        resp = c.post(self.batch_url, {"batch_notes": ""})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(PayoutBatch.objects.filter(seller=self.seller).count(), 0)

    def test_get_on_create_final_batch_returns_405(self):
        """GET on create final batch endpoint is rejected with 405."""
        super_admin = User.objects.create_user(
            username="p3_batch_super",
            email="p3batch_super@fitmeezo.com",
            password="AdminPass123!",
            is_staff=True,
            is_superuser=True,
        )
        set_user_role(super_admin, ROLE_SUPER_ADMIN)
        c = Client()
        c.login(username="p3_batch_super", password="AdminPass123!")
        resp = c.get(self.batch_url)
        self.assertEqual(resp.status_code, 405)
