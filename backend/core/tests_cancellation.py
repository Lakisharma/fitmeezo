"""
Fitmeezo Order Cancellation Feature - Automated Test Suite.
Verifies:
1. Customer website cancellation of eligible Pending & Processing orders.
2. Rejection of Shipped, Delivered, and already Cancelled orders.
3. Multi-tenant customer ownership isolation (rejects cancellation of another's order).
4. Atomic inventory restoration (exactly once; duplicate/concurrent attempts cannot double-restore).
5. Variant stock vs simple product stock restoration.
6. COD orders: cancelled without refund (refund_status='not_applicable').
7. Paid online orders: cancelled with refund_status='pending', payment_status preserved as 'paid' (no false completion).
8. Admin cancellation via management dashboard using the same business service and inventory safety.
9. REST API endpoint POST /api/orders/<order_number>/cancel/ (auth, permissions, status codes).
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Category, Order, OrderItem, Product, ProductVariant, SellerOrder
from core.services import cancel_order

User = get_user_model()


class OrderCancellationBaseTestCase(TestCase):
    """Base setup for order cancellation tests."""

    def setUp(self):
        self.client = Client()
        self.api_client = APIClient()

        # Users
        self.customer1 = User.objects.create_user(
            username="aarav_customer",
            email="aarav@example.com",
            password="AaravSecurePass123!",
            first_name="Aarav",
            last_name="Patel",
        )
        self.token1 = Token.objects.create(user=self.customer1)

        self.customer2 = User.objects.create_user(
            username="rohan_customer",
            email="rohan@example.com",
            password="RohanSecurePass123!",
            first_name="Rohan",
            last_name="Sharma",
        )
        self.token2 = Token.objects.create(user=self.customer2)

        self.staff_user = User.objects.create_user(
            username="staff_manager",
            email="staff@fitmeezo.in",
            password="StaffSecurePass123!",
            is_staff=True,
        )

        # Category
        self.category = Category.objects.create(name="Wearables", slug="wearables", is_active=True)

        # Simple Product (initial stock = 10)
        self.simple_product = Product.objects.create(
            name="Smart Fitness Band",
            slug="smart-fitness-band",
            sku="SM-BAND-001",
            category=self.category,
            price=Decimal("1299.00"),
            stock_quantity=10,
            is_active=True,
        )

        # Variant Product (initial stock = 15)
        self.variant_product = Product.objects.create(
            name="Sports Smartwatch",
            slug="sports-smartwatch",
            sku="SW-SPORTS-001",
            category=self.category,
            price=Decimal("2999.00"),
            stock_quantity=0,
            is_active=True,
        )
        self.variant = ProductVariant.objects.create(
            product=self.variant_product,
            option_type="Color",
            option_value="Obsidian Black",
            sku="SW-SPORTS-001-BLK",
            stock_quantity=15,
            is_active=True,
        )

    def _create_order(
        self,
        user,
        order_number="FM-TEST-001",
        order_status=Order.STATUS_PENDING,
        payment_method=Order.PAYMENT_METHOD_COD,
        payment_status=Order.PAYMENT_STATUS_UNPAID,
        simple_qty=2,
        variant_qty=1,
    ):
        """Helper to create a test order with line items."""
        order = Order.objects.create(
            order_number=order_number,
            user=user,
            shipping_full_name="Aarav Patel",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Marine Drive",
            shipping_city="Mumbai",
            shipping_state="Maharashtra",
            shipping_postal_code="400020",
            subtotal=Decimal("5597.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("5597.00"),
            status=order_status,
            payment_method=payment_method,
            payment_status=payment_status,
            inventory_restored=False,
        )

        if simple_qty > 0:
            OrderItem.objects.create(
                order=order,
                product=self.simple_product,
                variant=None,
                product_name=self.simple_product.name,
                sku=self.simple_product.sku,
                unit_price=self.simple_product.price,
                quantity=simple_qty,
                line_total=self.simple_product.price * simple_qty,
            )

        if variant_qty > 0:
            OrderItem.objects.create(
                order=order,
                product=self.variant_product,
                variant=self.variant,
                product_name=self.variant_product.name,
                variant_label=f"{self.variant.option_type}: {self.variant.option_value}",
                sku=self.variant.sku,
                unit_price=self.variant.effective_price,
                quantity=variant_qty,
                line_total=self.variant.effective_price * variant_qty,
            )

        return order


class WebsiteOrderCancellationTests(OrderCancellationBaseTestCase):
    """Tests customer order cancellation on the storefront website."""

    def test_customer_cancels_pending_cod_order_successfully(self):
        """Customer cancels their own Pending COD order: stock is restored and status is cancelled."""
        order = self._create_order(self.customer1, order_number="FM-CANCEL-001", order_status=Order.STATUS_PENDING)

        initial_prod_stock = self.simple_product.stock_quantity  # 10
        initial_var_stock = self.variant.stock_quantity  # 15

        self.client.login(username="aarav_customer", password="AaravSecurePass123!")
        resp = self.client.post(
            reverse("core:order_cancel", kwargs={"order_number": order.order_number}),
            {"reason": "Changed my mind"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse("core:order_detail", kwargs={"order_number": order.order_number}))

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.cancelled_by, "customer")
        self.assertEqual(order.cancellation_reason, "Changed my mind")
        self.assertIsNotNone(order.cancelled_at)
        self.assertTrue(order.inventory_restored)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)

        # Inventory verified: simple (+2) and variant (+1)
        self.simple_product.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(self.simple_product.stock_quantity, initial_prod_stock + 2)
        self.assertEqual(self.variant.stock_quantity, initial_var_stock + 1)

    def test_customer_cancels_processing_order_successfully(self):
        """Customer can cancel an order in Processing state."""
        order = self._create_order(self.customer1, order_number="FM-CANCEL-002", order_status=Order.STATUS_PROCESSING)

        self.client.login(username="aarav_customer", password="AaravSecurePass123!")
        resp = self.client.post(
            reverse("core:order_cancel", kwargs={"order_number": order.order_number}),
            {"reason": "Delivery takes too long"},
        )
        self.assertEqual(resp.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertTrue(order.inventory_restored)

    def test_customer_cannot_cancel_another_customers_order(self):
        """Customer 2 cannot cancel Customer 1's order (HTTP 404 Not Found, no data leakage)."""
        order = self._create_order(self.customer1, order_number="FM-PRIV-001")

        self.client.login(username="rohan_customer", password="RohanSecurePass123!")
        resp = self.client.post(
            reverse("core:order_cancel", kwargs={"order_number": order.order_number}),
            {"reason": "Malicious attempt"},
        )
        self.assertEqual(resp.status_code, 404)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING)  # Unchanged
        self.assertFalse(order.inventory_restored)

    def test_cancellation_rejected_for_shipped_order(self):
        """Shipped orders cannot be cancelled."""
        order = self._create_order(self.customer1, order_number="FM-SHIP-001", order_status=Order.STATUS_SHIPPED)

        self.client.login(username="aarav_customer", password="AaravSecurePass123!")
        resp = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_SHIPPED)
        self.assertFalse(order.inventory_restored)

    def test_cancellation_rejected_for_delivered_order(self):
        """Delivered orders cannot be cancelled."""
        order = self._create_order(self.customer1, order_number="FM-DELIV-001", order_status=Order.STATUS_DELIVERED)

        self.client.login(username="aarav_customer", password="AaravSecurePass123!")
        resp = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_DELIVERED)

    def test_cancellation_rejected_when_child_seller_order_is_shipped_or_delivered(self):
        """Parent order cancellation is blocked if any linked package has already shipped or delivered."""
        order = self._create_order(self.customer1, order_number="FM-CHILD-SHIP-001", order_status=Order.STATUS_PROCESSING)
        sub1 = SellerOrder.objects.create(
            order=order,
            is_platform_fulfilled=True,
            sub_order_number=f"{order.order_number}-PLT",
            status=SellerOrder.STATUS_SHIPPED,
        )

        initial_stock = self.simple_product.stock_quantity

        self.client.login(username="aarav_customer", password="AaravSecurePass123!")
        resp = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PROCESSING)
        self.assertFalse(order.inventory_restored)

        # Inventory must NOT be restored
        self.simple_product.refresh_from_db()
        self.assertEqual(self.simple_product.stock_quantity, initial_stock)

        # Now test when package is DELIVERED
        sub1.status = SellerOrder.STATUS_DELIVERED
        sub1.save()

        resp2 = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp2.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PROCESSING)
        self.assertFalse(order.inventory_restored)

    def test_cancellation_rejected_for_already_cancelled_order(self):
        """Repeated cancellation is rejected and does not restore stock twice."""
        order = self._create_order(self.customer1, order_number="FM-TWICE-001", order_status=Order.STATUS_PENDING)

        initial_prod_stock = self.simple_product.stock_quantity  # 10
        self.client.login(username="aarav_customer", password="AaravSecurePass123!")

        # First cancellation
        resp1 = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp1.status_code, 302)

        self.simple_product.refresh_from_db()
        self.assertEqual(self.simple_product.stock_quantity, initial_prod_stock + 2)

        # Second cancellation attempt
        resp2 = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp2.status_code, 302)

        # Inventory must NOT be restored again
        self.simple_product.refresh_from_db()
        self.assertEqual(self.simple_product.stock_quantity, initial_prod_stock + 2)

    def test_paid_razorpay_order_cancellation_sets_refund_pending(self):
        """Paid online orders are marked refund_status='pending' without claiming refund is completed."""
        order = self._create_order(
            self.customer1,
            order_number="FM-RP-PAID-001",
            order_status=Order.STATUS_PENDING,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
        )

        self.client.login(username="aarav_customer", password="AaravSecurePass123!")
        resp = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)  # Stays paid
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_PENDING)  # Flagged for review
        self.assertTrue(order.requires_refund)

    def test_unauthenticated_cancellation_redirects_to_login(self):
        """Unauthenticated POST request is redirected to login."""
        order = self._create_order(self.customer1, order_number="FM-UNAUTH-001")
        resp = self.client.post(reverse("core:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("core:account_login"), resp.url)


class AdminOrderCancellationTests(OrderCancellationBaseTestCase):
    """Tests admin cancellation via custom admin dashboard."""

    def test_admin_cancellation_restores_inventory(self):
        """Admin updating status to Cancelled triggers inventory restoration exactly once."""
        order = self._create_order(self.customer1, order_number="FM-ADMIN-CAN-001", order_status=Order.STATUS_PENDING)
        initial_prod_stock = self.simple_product.stock_quantity

        self.client.login(username="staff_manager", password="StaffSecurePass123!")
        resp = self.client.post(
            reverse("custom_admin:order_update_status", kwargs={"pk": order.pk}),
            {"status": Order.STATUS_CANCELLED},
        )
        self.assertEqual(resp.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.cancelled_by, "staff")
        self.assertTrue(order.inventory_restored)

        # Inventory restored
        self.simple_product.refresh_from_db()
        self.assertEqual(self.simple_product.stock_quantity, initial_prod_stock + 2)


class APIOrderCancellationTests(OrderCancellationBaseTestCase):
    """Tests REST API endpoint POST /api/orders/<order_number>/cancel/."""

    def setUp(self):
        super().setUp()
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Token {self.token1.key}")

    def test_api_cancel_own_pending_order_success(self):
        """Customer cancels their own order via API with Token authentication."""
        order = self._create_order(self.customer1, order_number="FM-API-CAN-001")
        initial_stock = self.simple_product.stock_quantity

        resp = self.api_client.post(
            reverse("api:order_cancel", kwargs={"order_number": order.order_number}),
            {"reason": "Changed my mind via mobile app"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["order"]["status"], "cancelled")
        self.assertEqual(data["order"]["is_cancellable"], False)
        self.assertTrue(data["order"]["inventory_restored"])

        self.simple_product.refresh_from_db()
        self.assertEqual(self.simple_product.stock_quantity, initial_stock + 2)

    def test_api_cancel_unauthenticated_fails(self):
        """Unauthenticated request to cancel endpoint returns 401 Unauthorized."""
        order = self._create_order(self.customer1, order_number="FM-API-UNAUTH-001")
        anon_client = APIClient()
        resp = anon_client.post(reverse("api:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_api_cancel_another_customer_order_returns_404(self):
        """Customer 2 cannot cancel Customer 1's order via API (returns 404)."""
        order = self._create_order(self.customer1, order_number="FM-API-PRIV-001")
        client2 = APIClient()
        client2.credentials(HTTP_AUTHORIZATION=f"Token {self.token2.key}")

        resp = client2.post(reverse("api:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_api_cancel_shipped_order_returns_400(self):
        """API returns 400 Bad Request when trying to cancel a Shipped order."""
        order = self._create_order(self.customer1, order_number="FM-API-SHIP-001", order_status=Order.STATUS_SHIPPED)

        resp = self.api_client.post(reverse("api:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()
        self.assertEqual(data["status"], "error")
        self.assertIn("cannot be cancelled", data["message"])

    def test_api_cancel_rejected_when_child_seller_order_is_shipped(self):
        """API returns 400 Bad Request when attempting to cancel an order whose child package has shipped."""
        order = self._create_order(self.customer1, order_number="FM-API-CSHIP-001", order_status=Order.STATUS_PROCESSING)
        SellerOrder.objects.create(
            order=order,
            is_platform_fulfilled=True,
            sub_order_number=f"{order.order_number}-PLT",
            status=SellerOrder.STATUS_SHIPPED,
        )

        resp = self.api_client.post(reverse("api:order_cancel", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()
        self.assertEqual(data["status"], "error")
        self.assertIn("cannot be cancelled", data["message"])
