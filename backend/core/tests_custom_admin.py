"""
Fitmeezo Custom Admin Dashboard - Automated Test Suite.
Tests authentication, permissions, dashboard statistics, product CRUD,
category safety controls, and order fulfillment workflows.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Category, Order, OrderItem, Product, ProductImage, ProductVariant, Seller

User = get_user_model()


class CustomAdminAuthenticationTests(TestCase):
    """
    Tests staff-only authentication and permission protections for custom admin.
    """

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="staff_admin",
            email="staff@fitmeezo.in",
            password="SecureStaffPass123!",
            is_staff=True,
        )
        self.customer_user = User.objects.create_user(
            username="customer_john",
            email="john@example.com",
            password="CustomerPass123!",
            is_staff=False,
            is_superuser=False,
        )

    def test_unauthenticated_user_redirected_to_admin_login(self):
        """Unauthenticated requests must be redirected to /custom-admin/login/ with ?next= parameter."""
        urls_to_test = [
            reverse("custom_admin:dashboard"),
            reverse("custom_admin:product_list"),
            reverse("custom_admin:category_list"),
            reverse("custom_admin:order_list"),
        ]
        for url in urls_to_test:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn(reverse("custom_admin:login"), resp.url)
            self.assertIn(f"next={url}", resp.url)

    def test_customer_user_forbidden_from_admin_dashboard(self):
        """Authenticated non-staff customers must be denied access (HTTP 403 Forbidden)."""
        self.client.login(username="customer_john", password="CustomerPass123!")
        urls_to_test = [
            reverse("custom_admin:dashboard"),
            reverse("custom_admin:product_list"),
            reverse("custom_admin:category_list"),
            reverse("custom_admin:order_list"),
        ]
        for url in urls_to_test:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 403)

    def test_staff_login_successful(self):
        """Authorized staff can sign in through the custom admin login view."""
        resp = self.client.post(
            reverse("custom_admin:login"),
            {
                "username_or_email": "staff_admin",
                "password": "SecureStaffPass123!",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse("custom_admin:dashboard"))

        # Verify access granted to dashboard
        dash_resp = self.client.get(reverse("custom_admin:dashboard"))
        self.assertEqual(dash_resp.status_code, 200)

    def test_customer_credentials_rejected_on_admin_login(self):
        """Customer credentials fail validation on the staff login form."""
        resp = self.client.post(
            reverse("custom_admin:login"),
            {
                "username_or_email": "customer_john",
                "password": "CustomerPass123!",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFormError(
            resp.context["form"],
            None,
            "Access denied: This account does not possess staff or administrative permissions.",
        )

    def test_staff_logout(self):
        """Staff user can safely log out via POST request."""
        self.client.login(username="staff_admin", password="SecureStaffPass123!")
        resp = self.client.post(reverse("custom_admin:logout"))
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse("custom_admin:login"))

        # Verify session cleared
        dash_resp = self.client.get(reverse("custom_admin:dashboard"))
        self.assertEqual(dash_resp.status_code, 302)


class CustomAdminDashboardStatsTests(TestCase):
    """
    Tests live statistics and metric calculations on the dashboard overview.
    """

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="admin_user",
            email="admin@fitmeezo.in",
            password="AdminPassword123!",
            is_staff=True,
        )
        self.client.login(username="admin_user", password="AdminPassword123!")

        # Create test customer
        self.customer = User.objects.create_user(
            username="test_buyer",
            email="buyer@test.com",
            password="BuyerPassword123!",
        )

        # Create categories
        self.cat1 = Category.objects.create(name="Footwear", slug="footwear", is_active=True)
        self.cat2 = Category.objects.create(name="Apparel", slug="apparel", is_active=True)

        # Standalone product (stock = 15)
        self.prod_standalone = Product.objects.create(
            name="Classic Running Shoes",
            slug="classic-running-shoes",
            sku="SH-RUN-001",
            category=self.cat1,
            price=Decimal("1999.00"),
            stock_quantity=15,
            is_active=True,
        )

        # Product with variants (variant 1: 5, variant 2: 8, total = 13)
        self.prod_variants = Product.objects.create(
            name="Training Tee",
            slug="training-tee",
            sku="TEE-TRN-001",
            category=self.cat2,
            price=Decimal("799.00"),
            stock_quantity=0,
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.prod_variants,
            option_type="Size",
            option_value="M",
            sku="TEE-TRN-001-M",
            stock_quantity=5,
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.prod_variants,
            option_type="Size",
            option_value="L",
            sku="TEE-TRN-001-L",
            stock_quantity=8,
            is_active=True,
        )

        # Orders
        self.order1 = Order.objects.create(
            order_number="FM-TEST-001",
            user=self.customer,
            shipping_full_name="Buyer One",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Test St",
            shipping_city="Mumbai",
            shipping_state="Maharashtra",
            shipping_postal_code="400001",
            subtotal=Decimal("1999.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1999.00"),
            status=Order.STATUS_PENDING,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )
        self.order2 = Order.objects.create(
            order_number="FM-TEST-002",
            user=self.customer,
            shipping_full_name="Buyer One",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Test St",
            shipping_city="Mumbai",
            shipping_state="Maharashtra",
            shipping_postal_code="400001",
            subtotal=Decimal("799.00"),
            shipping_fee=Decimal("50.00"),
            total_amount=Decimal("849.00"),
            status=Order.STATUS_PROCESSING,
            payment_status=Order.PAYMENT_STATUS_PAID,
        )

    def test_dashboard_metrics_calculation(self):
        """Dashboard renders exact counts and calculations from the database."""
        resp = self.client.get(reverse("custom_admin:dashboard"))
        self.assertEqual(resp.status_code, 200)

        # Metrics check
        self.assertEqual(resp.context["total_products"], 2)
        self.assertEqual(resp.context["total_categories"], 2)
        self.assertEqual(resp.context["total_customers"], 1)  # Only non-staff customer
        self.assertEqual(resp.context["total_orders"], 2)
        self.assertEqual(resp.context["pending_orders"], 1)
        self.assertEqual(resp.context["processing_orders"], 1)

        # Total available inventory: standalone (15) + variants (5 + 8) = 28
        self.assertEqual(resp.context["total_inventory"], 28)

        # Total revenue: 1999.00 + 849.00 = 2848.00
        self.assertEqual(resp.context["total_revenue"], Decimal("2848.00"))


class CustomAdminProductCrudTests(TestCase):
    """
    Tests product listing, filters, creation, modification, and deactivation.
    """

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="product_manager",
            email="pm@fitmeezo.in",
            password="PMPassword123!",
            is_staff=True,
        )
        self.client.login(username="product_manager", password="PMPassword123!")

        self.category = Category.objects.create(name="Gadgets", slug="gadgets", is_active=True)
        self.product = Product.objects.create(
            name="Smart Watch X1",
            slug="smart-watch-x1",
            sku="WTCH-X1-001",
            category=self.category,
            price=Decimal("2999.00"),
            compare_at_price=Decimal("3499.00"),
            stock_quantity=20,
            is_active=True,
        )

    def test_product_list_search_and_filter(self):
        """Product list allows searching by SKU/name and filtering by category/status."""
        resp = self.client.get(reverse("custom_admin:product_list"), {"q": "WTCH"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Smart Watch X1")

        # Non-matching search
        resp_empty = self.client.get(reverse("custom_admin:product_list"), {"q": "NonExistent"})
        self.assertEqual(resp_empty.status_code, 200)
        self.assertNotContains(resp_empty, "Smart Watch X1")

    def test_product_creation_valid(self):
        """Staff can create a new product via the admin form."""
        payload = {
            "name": "Wireless Earbuds Pro",
            "slug": "",  # Auto-slugify
            "sku": "EAR-PRO-001",
            "category": self.category.pk,
            "short_description": "Crystal clear sound with ANC",
            "description": "Full specifications...",
            "price": "1499.00",
            "compare_at_price": "1999.00",
            "stock_quantity": "50",
            "is_active": "on",
            "is_featured": "on",
        }
        resp = self.client.post(reverse("custom_admin:product_add"), payload)
        self.assertEqual(resp.status_code, 302)

        created = Product.objects.get(sku="EAR-PRO-001")
        self.assertEqual(created.name, "Wireless Earbuds Pro")
        self.assertEqual(created.slug, "wireless-earbuds-pro")
        self.assertEqual(created.price, Decimal("1499.00"))
        self.assertTrue(created.is_featured)

    def test_product_creation_invalid_pricing_rejected(self):
        """Original compare-at price cannot be less than selling price."""
        payload = {
            "name": "Invalid Price Item",
            "sku": "INV-PRC-001",
            "category": self.category.pk,
            "price": "2000.00",
            "compare_at_price": "1500.00",  # Invalid: compare-at < price
            "stock_quantity": "10",
        }
        resp = self.client.post(reverse("custom_admin:product_add"), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertFormError(
            resp.context["form"],
            "compare_at_price",
            "Original (Compare-at) price cannot be lower than the selling price.",
        )

    def test_product_safe_deactivation_toggle(self):
        """Staff can safely toggle active/inactive status without deleting data."""
        self.assertTrue(self.product.is_active)
        resp = self.client.post(reverse("custom_admin:product_toggle_status", kwargs={"pk": self.product.pk}))
        self.assertEqual(resp.status_code, 302)

        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)

        # Reactivate
        self.client.post(reverse("custom_admin:product_toggle_status", kwargs={"pk": self.product.pk}))
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)

    def test_product_variant_addition_and_deletion(self):
        """Staff can add and delete product variants."""
        # Add variant
        variant_payload = {
            "option_type": "Color",
            "option_value": "Space Gray",
            "sku": "WTCH-X1-GRY",
            "stock_quantity": "12",
            "is_active": "on",
        }
        resp = self.client.post(
            reverse("custom_admin:product_variant_add", kwargs={"pk": self.product.pk}),
            variant_payload,
        )
        self.assertEqual(resp.status_code, 302)

        variant = ProductVariant.objects.get(sku="WTCH-X1-GRY")
        self.assertEqual(variant.product, self.product)
        self.assertEqual(variant.stock_quantity, 12)

        # Delete variant
        del_resp = self.client.post(
            reverse(
                "custom_admin:product_variant_delete",
                kwargs={"pk": self.product.pk, "variant_pk": variant.pk},
            )
        )
        self.assertEqual(del_resp.status_code, 302)
        self.assertFalse(ProductVariant.objects.filter(sku="WTCH-X1-GRY").exists())


class CustomAdminCategorySafetyTests(TestCase):
    """
    Tests category management and safe deletion rules.
    """

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="cat_admin",
            email="cat@fitmeezo.in",
            password="CatPassword123!",
            is_staff=True,
        )
        self.client.login(username="cat_admin", password="CatPassword123!")

        self.category_with_prods = Category.objects.create(name="Electronics", slug="electronics")
        self.empty_category = Category.objects.create(name="Clearance", slug="clearance")

        Product.objects.create(
            name="Laptop Stand",
            sku="ACC-LAP-001",
            category=self.category_with_prods,
            price=Decimal("899.00"),
            stock_quantity=10,
        )

    def test_category_with_products_cannot_be_deleted(self):
        """Categories containing products must be protected against deletion."""
        resp = self.client.post(
            reverse("custom_admin:category_delete", kwargs={"pk": self.category_with_prods.pk})
        )
        self.assertEqual(resp.status_code, 302)
        # Category must still exist
        self.assertTrue(Category.objects.filter(pk=self.category_with_prods.pk).exists())

    def test_empty_category_can_be_safely_deleted(self):
        """Categories with zero products can be safely removed."""
        resp = self.client.post(
            reverse("custom_admin:category_delete", kwargs={"pk": self.empty_category.pk})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Category.objects.filter(pk=self.empty_category.pk).exists())


class CustomAdminOrderFulfillmentTests(TestCase):
    """
    Tests order management, status transitions, and payment status constraints.
    """

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="order_staff",
            email="orders@fitmeezo.in",
            password="OrderPassword123!",
            is_staff=True,
        )
        self.client.login(username="order_staff", password="OrderPassword123!")

        self.customer = User.objects.create_user(
            username="order_customer",
            email="cust@test.com",
            password="CustPassword123!",
        )

        self.cod_order = Order.objects.create(
            order_number="FM-COD-1001",
            user=self.customer,
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="12 High Street",
            shipping_city="Delhi",
            shipping_state="Delhi",
            shipping_postal_code="110001",
            subtotal=Decimal("1200.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1200.00"),
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_PENDING,
        )

        self.online_order = Order.objects.create(
            order_number="FM-RP-2001",
            user=self.customer,
            shipping_full_name="Rohit Verma",
            shipping_phone_number="9123456780",
            shipping_address_line1="45 Tech Boulevard",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("3500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("3500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_PENDING,
            razorpay_order_id="order_test_12345",
            razorpay_payment_id="",  # No verified payment ID
        )

    def test_valid_order_status_progression(self):
        """Order status progresses correctly: pending -> processing -> shipped -> delivered."""
        # 1. pending -> processing
        resp1 = self.client.post(
            reverse("custom_admin:order_update_status", kwargs={"pk": self.cod_order.pk}),
            {"status": Order.STATUS_PROCESSING},
        )
        self.assertEqual(resp1.status_code, 302)
        self.cod_order.refresh_from_db()
        self.assertEqual(self.cod_order.status, Order.STATUS_PROCESSING)

        # 2. processing -> shipped
        resp2 = self.client.post(
            reverse("custom_admin:order_update_status", kwargs={"pk": self.cod_order.pk}),
            {"status": Order.STATUS_SHIPPED},
        )
        self.assertEqual(resp2.status_code, 302)
        self.cod_order.refresh_from_db()
        self.assertEqual(self.cod_order.status, Order.STATUS_SHIPPED)

        # 3. shipped -> delivered
        resp3 = self.client.post(
            reverse("custom_admin:order_update_status", kwargs={"pk": self.cod_order.pk}),
            {"status": Order.STATUS_DELIVERED},
        )
        self.assertEqual(resp3.status_code, 302)
        self.cod_order.refresh_from_db()
        self.assertEqual(self.cod_order.status, Order.STATUS_DELIVERED)

    def test_invalid_order_status_transition_rejected(self):
        """Invalid jumps (e.g. pending -> delivered) must be rejected."""
        resp = self.client.post(
            reverse("custom_admin:order_update_status", kwargs={"pk": self.cod_order.pk}),
            {"status": Order.STATUS_DELIVERED},  # Invalid jump from pending!
        )
        self.assertEqual(resp.status_code, 302)
        self.cod_order.refresh_from_db()
        # Status must remain pending
        self.assertEqual(self.cod_order.status, Order.STATUS_PENDING)

    def test_terminal_order_state_cannot_change(self):
        """Cancelled or Delivered orders cannot change status."""
        self.cod_order.status = Order.STATUS_CANCELLED
        self.cod_order.save(update_fields=["status"])

        resp = self.client.post(
            reverse("custom_admin:order_update_status", kwargs={"pk": self.cod_order.pk}),
            {"status": Order.STATUS_PROCESSING},
        )
        self.assertEqual(resp.status_code, 302)
        self.cod_order.refresh_from_db()
        self.assertEqual(self.cod_order.status, Order.STATUS_CANCELLED)

    def test_cod_payment_marked_as_paid(self):
        """COD order can be updated to paid upon cash collection."""
        resp = self.client.post(
            reverse("custom_admin:order_update_payment", kwargs={"pk": self.cod_order.pk}),
            {"payment_status": Order.PAYMENT_STATUS_PAID},
        )
        self.assertEqual(resp.status_code, 302)
        self.cod_order.refresh_from_db()
        self.assertEqual(self.cod_order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertIsNotNone(self.cod_order.paid_at)

    def test_online_payment_cannot_be_manually_marked_paid_without_payment_id(self):
        """Online Razorpay order cannot be set to paid without verified gateway payment id."""
        resp = self.client.post(
            reverse("custom_admin:order_update_payment", kwargs={"pk": self.online_order.pk}),
            {"payment_status": Order.PAYMENT_STATUS_PAID},
        )
        self.assertEqual(resp.status_code, 302)
        self.online_order.refresh_from_db()
        # Must still be unpaid
        self.assertEqual(self.online_order.payment_status, Order.PAYMENT_STATUS_UNPAID)


class CustomAdminBankVerificationTests(TestCase):
    """
    Tests platform admin bank verification features in the custom admin panel:
    - Visibility on the custom admin dashboard (/custom-admin/)
    - 'Review Bank Details' link and review page (/custom-admin/sellers/<seller_id>/bank-verify/)
    - Display of seller identity, bank name, account holder, masked account number, IFSC, account type, status, and rejection reason
    - Approve action updating status, timestamp, reviewer
    - Reject action requiring reason and updating status
    - Unauthorized access protections (HTTP 403 for ordinary sellers, redirect for unauthenticated)
    """

    def setUp(self):
        self.client = Client()
        self.staff_admin = User.objects.create_user(
            username="platform_admin",
            email="admin@fitmeezo.in",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        self.seller_user = User.objects.create_user(
            username="seller_user",
            email="seller@example.com",
            password="SellerPassword123!",
            is_staff=False,
            is_superuser=False,
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Metro Retail Hub",
            legal_business_name="Metro Retail Ventures Private Limited",
            gstin="29ABCDE1234F1Z5",
            status="approved",
            bank_account_holder_name="Metro Retail Ventures Private Limited",
            bank_account_number="987654321098",
            bank_ifsc_code="HDFC0000128",
            bank_name="HDFC Bank",
            bank_branch="Koramangala Branch",
            bank_account_type="current",
            bank_verification_status="pending",
        )

    def test_dashboard_shows_pending_bank_verifications_section_and_review_button(self):
        """Dashboard must display 'Pending Bank Verifications' section and 'Review Bank Details' button."""
        self.client.force_login(self.staff_admin)
        resp = self.client.get(reverse("custom_admin:dashboard"))
        self.assertEqual(resp.status_code, 200)

        # Dashboard section and heading
        self.assertContains(resp, "Pending Bank Verifications")
        self.assertContains(resp, "id=\"pending-bank-verifications-section\"")

        # Seller info in dashboard table
        self.assertContains(resp, "Metro Retail Hub")
        self.assertContains(resp, "Metro Retail Ventures Private Limited")
        self.assertContains(resp, "••••••••1098")
        self.assertContains(resp, "HDFC0000128")
        self.assertContains(resp, "HDFC Bank")

        # Review Bank Details button
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller.id})
        self.assertContains(resp, f'href="{verify_url}"')
        self.assertContains(resp, "Review Bank Details")

    def test_review_page_displays_all_required_seller_and_bank_details(self):
        """Review page shows seller identity, bank name, account holder, masked account, IFSC, account type, status."""
        self.client.force_login(self.staff_admin)
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller.id})
        resp = self.client.get(verify_url)
        self.assertEqual(resp.status_code, 200)

        self.assertContains(resp, "Metro Retail Hub")
        self.assertContains(resp, "Metro Retail Ventures Private Limited")
        self.assertContains(resp, "29ABCDE1234F1Z5")
        self.assertContains(resp, "HDFC Bank")
        self.assertContains(resp, "••••••••1098")
        self.assertContains(resp, "HDFC0000128")
        self.assertContains(resp, "Current Account")
        self.assertContains(resp, "Pending Manual Review")
        self.assertContains(resp, "Approve &amp; Verify Bank Account")
        self.assertContains(resp, "Reject Bank Account")

    def test_admin_approve_action(self):
        """Admin can approve bank details; status, reviewer, and timestamp update."""
        self.client.force_login(self.staff_admin)
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller.id})

        resp = self.client.post(verify_url, {"action": "approve"})
        self.assertEqual(resp.status_code, 302)

        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_verification_status, "verified")
        self.assertEqual(self.seller.bank_verified_by, self.staff_admin)
        self.assertIsNotNone(self.seller.bank_verified_at)
        self.assertEqual(self.seller.bank_rejection_reason, "")
        self.assertTrue(self.seller.is_bank_payout_eligible)

    def test_admin_reject_action_requires_reason(self):
        """Rejecting without a reason fails; rejecting with reason updates status and rejection reason."""
        self.client.force_login(self.staff_admin)
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller.id})

        # 1. Reject without reason is rejected and renders error
        resp_no_reason = self.client.post(verify_url, {"action": "reject", "rejection_reason": ""})
        self.assertEqual(resp_no_reason.status_code, 200)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_verification_status, "pending")

        # 2. Reject with reason succeeds
        rejection_msg = "Account holder name does not match GST registration legal name."
        resp_reject = self.client.post(verify_url, {"action": "reject", "rejection_reason": rejection_msg})
        self.assertEqual(resp_reject.status_code, 302)

        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_verification_status, "rejected")
        self.assertEqual(self.seller.bank_rejection_reason, rejection_msg)
        self.assertEqual(self.seller.bank_verified_by, self.staff_admin)
        self.assertIsNone(self.seller.bank_verified_at)
        self.assertFalse(self.seller.is_bank_payout_eligible)

        # 3. Existing rejection reason shown on review page
        resp_review = self.client.get(verify_url)
        self.assertEqual(resp_review.status_code, 200)
        self.assertContains(resp_review, rejection_msg)

    def test_ordinary_seller_receives_403_forbidden(self):
        """Ordinary sellers must receive HTTP 403 Forbidden and cannot approve their own account."""
        self.client.force_login(self.seller_user)
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller.id})

        # GET is forbidden
        resp_get = self.client.get(verify_url)
        self.assertEqual(resp_get.status_code, 403)

        # POST approve is forbidden
        resp_post = self.client.post(verify_url, {"action": "approve"})
        self.assertEqual(resp_post.status_code, 403)

        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_verification_status, "pending")
        self.assertFalse(self.seller.is_bank_payout_eligible)

    def test_unauthenticated_user_redirected_to_admin_login(self):
        """Unauthenticated requests to bank verify are redirected to custom admin login."""
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller.id})
        resp = self.client.get(verify_url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("custom_admin:login"), resp.url)

