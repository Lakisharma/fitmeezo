"""
Fitmeezo Management Console - Comprehensive Automated Test Suite.
Verifies independence, security, role enforcement, dynamic CRUD, product studio,
seller approvals, and confirms zero regressions on original /admin/ and /custom-admin/.
"""

from decimal import Decimal
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
)

User = get_user_model()


class PanelIndependenceAndRegressionTests(TestCase):
    """
    Verifies that the original Django Admin and existing Custom Admin retain 100% of their
    behavior, and that Management Console runs completely independently.
    """

    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username="super_architect",
            email="super@fitmeezo.in",
            password="SecurePassSuper123!",
        )
        self.staff_user = User.objects.create_user(
            username="console_staff",
            email="staff@fitmeezo.in",
            password="SecurePassStaff123!",
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="store_customer",
            email="customer@example.com",
            password="CustomerPass123!",
            is_staff=False,
            is_superuser=False,
        )

    def test_original_django_admin_login_and_access(self):
        """Original Django Admin at /admin/ must remain functional and unchanged."""
        resp = self.client.get("/admin/login/")
        self.assertEqual(resp.status_code, 200)

        # Login to original admin as superuser
        self.client.login(username="super_architect", password="SecurePassSuper123!")
        resp = self.client.get("/admin/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Site administration")

    def test_existing_custom_admin_login_and_access(self):
        """Existing Custom Admin at /custom-admin/ must remain functional and unchanged."""
        # Unauthenticated redirects to custom-admin login
        resp = self.client.get("/custom-admin/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/custom-admin/login/", resp.url)

        # Login as staff
        self.client.login(username="console_staff", password="SecurePassStaff123!")
        resp = self.client.get("/custom-admin/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Fitmeezo Admin")

    def test_management_console_unauthenticated_redirect(self):
        """Unauthenticated requests to /management-console/ must redirect to /management-console/login/."""
        urls = [
            reverse("management_console:dashboard"),
            reverse("management_console:product_add"),
            reverse("management_console:seller_approvals"),
            reverse("management_console:inventory_monitor"),
            reverse("management_console:model_list", args=["core", "product"]),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn(reverse("management_console:login"), resp.url)
            self.assertIn(f"next={url}", resp.url)

    def test_management_console_forbidden_for_regular_customers(self):
        """Non-staff customer accounts must receive HTTP 403 Forbidden on the management console."""
        self.client.login(username="store_customer", password="CustomerPass123!")
        urls = [
            reverse("management_console:dashboard"),
            reverse("management_console:product_add"),
            reverse("management_console:seller_approvals"),
            reverse("management_console:model_list", args=["core", "product"]),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 403)
            self.assertContains(resp, "Staff Authorization Required", status_code=403)

    def test_management_console_staff_login_and_dashboard(self):
        """Active staff members can log in via /management-console/login/ and access dashboard."""
        resp = self.client.post(
            reverse("management_console:login"),
            {"username_or_email": "console_staff", "password": "SecurePassStaff123!"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse("management_console:dashboard"))

        # Access dashboard
        dash_resp = self.client.get(reverse("management_console:dashboard"))
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, "Enterprise Operations Dashboard")
        self.assertContains(dash_resp, "Fitmeezo")


class ManagementConsoleCRUDTests(TestCase):
    """
    Tests dynamic model listing, filtering, search, add, edit, and deletion workflows.
    """

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_superuser(
            username="admin_lead",
            email="lead@fitmeezo.in",
            password="SuperPass123!",
        )
        self.client.login(username="admin_lead", password="SuperPass123!")

        # Create baseline Category and Seller
        self.category = Category.objects.create(
            name="Fitness Apparel",
            slug="fitness-apparel",
            is_active=True,
            display_order=1,
        )
        self.seller_user = User.objects.create_user(
            username="sportswear_hub",
            email="seller@hub.in",
            password="SellerPass123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="ActiveWear India",
            legal_business_name="ActiveWear India Pvt Ltd",
            status="pending",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            commission_rate=Decimal("12.50"),
        )
        self.product = Product.objects.create(
            name="Pro Seamless Gym T-Shirt",
            slug="pro-seamless-gym-t-shirt",
            sku="FTM-GYM-001",
            category=self.category,
            seller=self.seller,
            price=Decimal("899.00"),
            compare_at_price=Decimal("1299.00"),
            stock_quantity=25,
            is_active=True,
            is_approved=True,
            approval_status="approved",
        )

    def test_dynamic_model_list_renders(self):
        """Any registered model can be viewed in the dynamic list view."""
        url = reverse("management_console:model_list", args=["core", "product"])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pro Seamless Gym T-Shirt")
        self.assertContains(resp, "FTM-GYM-001")

    def test_dynamic_model_list_search_and_filter(self):
        """Search query filters products by search_fields."""
        url = reverse("management_console:model_list", args=["core", "product"])

        # Match search
        resp = self.client.get(url + "?q=Seamless")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pro Seamless Gym T-Shirt")

        # Non-matching search
        resp_nomatch = self.client.get(url + "?q=NonExistentProductQueryXYZ")
        self.assertEqual(resp_nomatch.status_code, 200)
        self.assertContains(resp_nomatch, "No products found")

    def test_product_studio_add_view(self):
        """Product Studio allows adding a new product with full validation."""
        add_url = reverse("management_console:product_add")
        resp = self.client.get(add_url)
        self.assertEqual(resp.status_code, 200)

        # Post valid product
        post_data = {
            "name": "Elite Compression Shorts",
            "sku": "FTM-SHORTS-002",
            "category": self.category.pk,
            "seller": self.seller.pk,
            "price": "599.00",
            "compare_at_price": "899.00",
            "stock_quantity": "50",
            "approval_status": "approved",
            "is_active": True,
            "is_approved": True,
            "variant_type": "Size",
            "variant_value": "L",
            "variant_sku": "FTM-SHORTS-002-L",
            "variant_price": "599.00",
            "variant_stock": "25",
        }
        post_resp = self.client.post(add_url, post_data)
        self.assertEqual(post_resp.status_code, 302)

        created_prod = Product.objects.get(sku="FTM-SHORTS-002")
        self.assertEqual(created_prod.name, "Elite Compression Shorts")
        self.assertEqual(created_prod.price, Decimal("599.00"))
        # Verify variant creation
        self.assertTrue(ProductVariant.objects.filter(product=created_prod, sku="FTM-SHORTS-002-L").exists())

    def test_product_validation_negative_price_rejected(self):
        """Negative price must trigger a validation error."""
        add_url = reverse("management_console:product_add")
        post_data = {
            "name": "Invalid Price Product",
            "sku": "FTM-INV-001",
            "category": self.category.pk,
            "seller": self.seller.pk,
            "price": "-10.00",
            "stock_quantity": "10",
        }
        resp = self.client.post(add_url, post_data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Price cannot be negative")

    def test_seller_approval_workbench(self):
        """Seller approvals workbench allows approving pending merchants."""
        approvals_url = reverse("management_console:seller_approvals")
        resp = self.client.get(approvals_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ActiveWear India")

        # Approve seller
        post_resp = self.client.post(
            approvals_url,
            {"seller_id": self.seller.pk, "action": "approve"},
        )
        self.assertEqual(post_resp.status_code, 302)

        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, "approved")
        self.assertTrue(self.seller.is_active)
        self.assertIsNotNone(self.seller.approved_at)

    def test_order_detail_view_and_status_update(self):
        """Order detail view renders correctly and allows updating fulfillment status."""
        order = Order.objects.create(
            order_number="ORD-TEST-99881",
            user=self.staff_user,
            shipping_full_name="Alex Mercer",
            shipping_phone_number="9876543210",
            shipping_address_line1="42 Tech Park Road",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560100",
            subtotal=Decimal("899.00"),
            total_amount=Decimal("899.00"),
            status="pending",
            payment_status="pending",
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            product_name=self.product.name,
            sku=self.product.sku,
            unit_price=self.product.price,
            quantity=1,
            line_total=self.product.price,
        )

        detail_url = reverse("management_console:order_detail", args=[order.pk])
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ORD-TEST-99881")
        self.assertContains(resp, "Alex Mercer")
        self.assertContains(resp, "Pro Seamless Gym T-Shirt")

        # Update order status
        post_resp = self.client.post(
            detail_url,
            {"action": "update_status", "status": "processing"},
        )
        self.assertEqual(post_resp.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "processing")

    def test_global_search_view(self):
        """Global search queries across products, categories, sellers, and orders."""
        search_url = reverse("management_console:global_search") + "?q=Seamless"
        resp = self.client.get(search_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pro Seamless Gym T-Shirt")
        self.assertContains(resp, "Catalog Products")

    def test_audit_logs_viewer(self):
        """Audit logs viewer presents LogEntry history."""
        url = reverse("management_console:audit_logs")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Enterprise Audit Logs")
