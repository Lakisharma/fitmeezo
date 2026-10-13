"""
Fitmeezo Phase 4 — Multi-Seller Cart, Checkout & Parent/Child Order Integration Tests.

Verifies:
1. Multi-seller cart creates 1 parent Order and distinct SellerOrder packages.
2. Mixed cart (Seller + Platform) creates exactly 1 seller package and 1 platform package.
3. Exactly one parent Order per checkout (no duplicate parent orders).
4. Exactly one SellerOrder package per distinct seller and 1 platform package when platform items are present.
5. OrderItem linkages: items link to parent Order, seller, and corresponding SellerOrder.
6. Commission snapshotting: snapshot commission_rate and commission_amount, immune to subsequent seller rate changes.
7. Checkout idempotency: repeated request with same idempotency token returns existing order without duplicate stock deductions.
8. Unapproved/pending moderation products rejected at checkout.
9. Inactive or unapproved merchant products rejected at checkout.
10. Insufficient inventory rejected cleanly with no partial order creation.
11. COD checkout creates correct packages without marking order as paid.
12. Razorpay order initialization with verified server amount, and signature verification marks paid.
13. Razorpay payment failure synchronizes child packages to CANCELLED and restores stock once.
14. Order cancellation never restores stock twice.
15. Shipped or delivered packages cannot be cancelled.
16. Customer-facing API serializers do not expose internal financial metrics (commissions, net payables).
17. Cross-seller package isolation: one seller cannot access another seller's packages.
18. Historical orders without SellerOrder packages remain readable on web detail and API.
"""

from decimal import Decimal
import uuid
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    Address,
    Category,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
    SellerOrder,
)
from core.services import (
    cancel_order,
    create_order_from_cart,
    CheckoutValidationError,
)

User = get_user_model()


class MultiSellerCheckoutTests(TestCase):
    """Comprehensive test suite for Phase 4 multi-seller checkout and parent/child order integration."""

    def setUp(self):
        # 1. Platform category
        self.category = Category.objects.create(name="Athletic Wear", slug="athletic-wear")

        # 2. Customer user & address
        self.customer = User.objects.create_user(
            username="customer_p4@example.com",
            email="customer_p4@example.com",
            password="Password123!",
            first_name="Anita",
            last_name="Roy",
        )
        self.shipping_address = Address.objects.create(
            user=self.customer,
            full_name="Anita Roy",
            phone_number="9876543210",
            address_line1="123 Brigade Road",
            address_line2="Apt 4B",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560025",
            address_type="home",
            is_default=True,
        )

        # 3. Seller 1 (Approved, 10% commission)
        self.seller1_user = User.objects.create_user(
            username="seller1_p4@example.com",
            email="seller1_p4@example.com",
            password="Password123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller1_user,
            store_name="Alpha Activewear",
            legal_business_name="Alpha Activewear Pvt Ltd",
            support_email="support@alpha.in",
            support_phone="9876500001",
            registered_address="1 Industrial Estate",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="1 Industrial Estate",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z1",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # 4. Seller 2 (Approved, 15% commission)
        self.seller2_user = User.objects.create_user(
            username="seller2_p4@example.com",
            email="seller2_p4@example.com",
            password="Password123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller2_user,
            store_name="Beta Sportswear",
            legal_business_name="Beta Sportswear LLP",
            support_email="support@beta.in",
            support_phone="9876500002",
            registered_address="2 Industrial Estate",
            city="Mumbai",
            state="Maharashtra",
            postal_code="400001",
            pickup_address="2 Industrial Estate",
            pickup_city="Mumbai",
            pickup_state="Maharashtra",
            pickup_postal_code="400001",
            gstin="27ABCDE5678F1Z2",
            pan_number="ABCDE5678F",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # 5. Products
        # Seller 1 product
        self.prod_seller1 = Product.objects.create(
            name="Alpha Running Shorts",
            slug="alpha-running-shorts",
            sku="ALP-RSH-001",
            category=self.category,
            seller=self.seller1,
            price=Decimal("1000.00"),
            stock_quantity=20,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Seller 2 product
        self.prod_seller2 = Product.objects.create(
            name="Beta Compression Tee",
            slug="beta-compression-tee",
            sku="BET-CPT-001",
            category=self.category,
            seller=self.seller2,
            price=Decimal("1200.00"),
            stock_quantity=15,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Platform product (seller=None)
        self.prod_platform = Product.objects.create(
            name="Fitmeezo Signature Gym Bag",
            slug="fitmeezo-gym-bag",
            sku="FM-BAG-001",
            category=self.category,
            seller=None,
            price=Decimal("800.00"),
            stock_quantity=30,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Auth token for API testing
        self.customer_token = Token.objects.create(user=self.customer)
        self.api_client = APIClient()
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Token {self.customer_token.key}")

    # --------------------------------------------------------------------------
    # 1, 3, 4, 5. Multi-Seller Cart Checkout (Seller 1 + Seller 2)
    # --------------------------------------------------------------------------
    def test_multi_seller_checkout_creates_one_parent_and_distinct_packages(self):
        """
        Checkout with items from two distinct sellers creates exactly 1 parent Order,
        exactly 2 SellerOrder packages, and correctly links OrderItems.
        """
        self.client.login(username="customer_p4@example.com", password="Password123!")

        # Add 2 units of Seller 1 item (2 * 1000 = 2000.00)
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 2})
        # Add 1 unit of Seller 2 item (1 * 1200 = 1200.00)
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller2.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-multi-seller-001",
        }
        response = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(response.status_code, 302)

        # 1. Exactly one parent Order
        parent_orders = Order.objects.filter(idempotency_key="token-multi-seller-001")
        self.assertEqual(parent_orders.count(), 1)
        parent_order = parent_orders.first()

        # Total: 2000 + 1200 = 3200 (subtotal >= 499 free shipping)
        self.assertEqual(parent_order.subtotal, Decimal("3200.00"))
        self.assertEqual(parent_order.shipping_fee, Decimal("0.00"))
        self.assertEqual(parent_order.total_amount, Decimal("3200.00"))
        self.assertEqual(parent_order.payment_method, Order.PAYMENT_METHOD_COD)
        self.assertEqual(parent_order.payment_status, Order.PAYMENT_STATUS_UNPAID)

        # 2. Exactly 2 child SellerOrder packages (1 per distinct seller, 0 platform)
        child_packages = parent_order.seller_orders.all()
        self.assertEqual(child_packages.count(), 2)

        pkg_seller1 = parent_order.seller_orders.get(seller=self.seller1)
        pkg_seller2 = parent_order.seller_orders.get(seller=self.seller2)

        # Seller 1 package checks
        self.assertEqual(pkg_seller1.sub_order_number, f"{parent_order.order_number}-S{self.seller1.id}")
        self.assertFalse(pkg_seller1.is_platform_fulfilled)
        self.assertEqual(pkg_seller1.subtotal, Decimal("2000.00"))
        self.assertEqual(pkg_seller1.total_commission, Decimal("200.00"))  # 10% of 2000
        self.assertEqual(pkg_seller1.net_seller_payable, Decimal("1800.00"))

        # Seller 2 package checks
        self.assertEqual(pkg_seller2.sub_order_number, f"{parent_order.order_number}-S{self.seller2.id}")
        self.assertFalse(pkg_seller2.is_platform_fulfilled)
        self.assertEqual(pkg_seller2.subtotal, Decimal("1200.00"))
        self.assertEqual(pkg_seller2.total_commission, Decimal("180.00"))  # 15% of 1200
        self.assertEqual(pkg_seller2.net_seller_payable, Decimal("1020.00"))

        # OrderItem linkages
        self.assertEqual(parent_order.items.count(), 2)
        item1 = parent_order.items.get(product=self.prod_seller1)
        self.assertEqual(item1.seller_order, pkg_seller1)
        self.assertEqual(item1.seller, self.seller1)
        self.assertEqual(item1.commission_rate, Decimal("10.00"))
        self.assertEqual(item1.commission_amount, Decimal("200.00"))

        item2 = parent_order.items.get(product=self.prod_seller2)
        self.assertEqual(item2.seller_order, pkg_seller2)
        self.assertEqual(item2.seller, self.seller2)
        self.assertEqual(item2.commission_rate, Decimal("15.00"))
        self.assertEqual(item2.commission_amount, Decimal("180.00"))

        # Inventory deduction
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 18)  # 20 - 2
        self.prod_seller2.refresh_from_db()
        self.assertEqual(self.prod_seller2.stock_quantity, 14)  # 15 - 1

    # --------------------------------------------------------------------------
    # 2, 4. Mixed Cart (Seller + Platform Direct Merchandise)
    # --------------------------------------------------------------------------
    def test_mixed_seller_and_platform_cart_creates_distinct_packages(self):
        """
        Checkout containing 1 third-party seller product and 1 platform-owned product
        creates exactly 1 seller package and 1 platform package (-PLT).
        """
        self.client.login(username="customer_p4@example.com", password="Password123!")

        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_platform.pk, "quantity": 2})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-mixed-p4-001",
        }
        response = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(idempotency_key="token-mixed-p4-001")
        self.assertEqual(order.seller_orders.count(), 2)

        seller_pkg = order.seller_orders.get(seller=self.seller1)
        platform_pkg = order.seller_orders.get(is_platform_fulfilled=True)

        # Platform package assertions
        self.assertIsNone(platform_pkg.seller)
        self.assertTrue(platform_pkg.is_platform_fulfilled)
        self.assertEqual(platform_pkg.sub_order_number, f"{order.order_number}-PLT")
        self.assertEqual(platform_pkg.subtotal, Decimal("1600.00"))  # 2 * 800
        self.assertEqual(platform_pkg.total_commission, Decimal("0.00"))
        self.assertEqual(platform_pkg.net_seller_payable, Decimal("1600.00"))

        # Platform item assertions
        plt_item = platform_pkg.items.first()
        self.assertEqual(plt_item.product, self.prod_platform)
        self.assertIsNone(plt_item.seller)
        self.assertEqual(plt_item.commission_rate, Decimal("0.00"))
        self.assertEqual(plt_item.commission_amount, Decimal("0.00"))

    # --------------------------------------------------------------------------
    # 6. Commission Snapshot Reliability (Historical Immobility)
    # --------------------------------------------------------------------------
    def test_historical_commission_snapshots_remain_unchanged(self):
        """Modifying seller commission rate does not mutate past order item snapshots."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-comm-snap-001",
        }
        self.client.post(reverse("core:checkout"), payload)

        order = Order.objects.get(idempotency_key="token-comm-snap-001")
        item = order.items.first()
        self.assertEqual(item.commission_rate, Decimal("10.00"))
        self.assertEqual(item.commission_amount, Decimal("100.00"))

        # Change seller's commission rate from 10% to 25%
        self.seller1.commission_rate = Decimal("25.00")
        self.seller1.save(update_fields=["commission_rate", "updated_at"])

        # Historical item remains 10% and 100.00
        item.refresh_from_db()
        self.assertEqual(item.commission_rate, Decimal("10.00"))
        self.assertEqual(item.commission_amount, Decimal("100.00"))

        pkg = order.seller_orders.first()
        pkg.refresh_from_db()
        self.assertEqual(pkg.total_commission, Decimal("100.00"))
        self.assertEqual(pkg.net_seller_payable, Decimal("900.00"))

    # --------------------------------------------------------------------------
    # 7. Checkout Idempotency (Repeat Submission Safeguard)
    # --------------------------------------------------------------------------
    def test_repeated_checkout_requests_do_not_create_duplicate_orders_or_deduct_stock(self):
        """Repeated checkout with the same idempotency token returns existing order without duplicate stock deductions."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 2})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-idempotent-repeat-001",
        }

        # First request
        resp1 = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(resp1.status_code, 302)

        order1 = Order.objects.get(idempotency_key="token-idempotent-repeat-001")
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 18)  # 20 - 2 = 18

        # Second repeat request with same token
        resp2 = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(resp2.status_code, 302)

        # Confirm no duplicate order was created
        self.assertEqual(Order.objects.filter(idempotency_key="token-idempotent-repeat-001").count(), 1)
        self.assertEqual(SellerOrder.objects.filter(order=order1).count(), 1)

        # Stock quantity was NOT decremented again!
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 18)

    # --------------------------------------------------------------------------
    # 8. Unapproved Product Rejection
    # --------------------------------------------------------------------------
    def test_unapproved_product_is_rejected_at_checkout(self):
        """Product that is pending moderation or rejected cannot be purchased."""
        unapproved_prod = Product.objects.create(
            name="Unapproved Yoga Pants",
            slug="unapproved-yoga-pants",
            sku="UNAP-YP-001",
            category=self.category,
            seller=self.seller1,
            price=Decimal("1500.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=False,
            approval_status=Product.APPROVAL_PENDING,
        )

        self.client.login(username="customer_p4@example.com", password="Password123!")
        # Add to cart
        self.client.post(reverse("core:cart_add"), {"product_id": unapproved_prod.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-unapproved-prod-001",
        }
        resp = self.client.post(reverse("core:checkout"), payload)
        # Should redirect to cart with error
        self.assertRedirects(resp, reverse("core:cart_detail"))

        # No order created
        self.assertFalse(Order.objects.filter(idempotency_key="token-unapproved-prod-001").exists())

    # --------------------------------------------------------------------------
    # 9. Inactive / Unapproved Seller Product Rejection
    # --------------------------------------------------------------------------
    def test_inactive_or_unapproved_seller_product_is_rejected(self):
        """Product belonging to a suspended or inactive seller cannot be purchased."""
        suspended_user = User.objects.create_user(
            username="suspended_seller@example.com",
            email="suspended_seller@example.com",
            password="Password123!",
        )
        suspended_seller = Seller.objects.create(
            user=suspended_user,
            store_name="Suspended Merchant",
            legal_business_name="Suspended Merchant LLC",
            support_email="support@suspended.in",
            support_phone="9876599999",
            registered_address="9 Bad Street",
            city="Delhi",
            state="Delhi",
            postal_code="110001",
            pickup_address="9 Bad Street",
            pickup_city="Delhi",
            pickup_state="Delhi",
            pickup_postal_code="110001",
            gstin="07ABCDE9999F1Z9",
            pan_number="ABCDE9999F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_SUSPENDED,
            is_active=False,
        )
        suspended_prod = Product.objects.create(
            name="Suspended Merchant Shoes",
            slug="suspended-merchant-shoes",
            sku="SUS-SHOE-001",
            category=self.category,
            seller=suspended_seller,
            price=Decimal("2000.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": suspended_prod.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-suspended-seller-001",
        }
        resp = self.client.post(reverse("core:checkout"), payload)
        self.assertRedirects(resp, reverse("core:cart_detail"))
        self.assertFalse(Order.objects.filter(idempotency_key="token-suspended-seller-001").exists())

    # --------------------------------------------------------------------------
    # 10. Insufficient Inventory Handled Safely
    # --------------------------------------------------------------------------
    def test_insufficient_inventory_handled_safely(self):
        """Checkout fails safely when requested quantity exceeds available stock."""
        self.prod_seller1.stock_quantity = 2
        self.prod_seller1.save(update_fields=["stock_quantity"])

        self.client.login(username="customer_p4@example.com", password="Password123!")
        # Add 5 units (only 2 in stock)
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 5})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-insufficient-inv-001",
        }
        resp = self.client.post(reverse("core:checkout"), payload)
        self.assertRedirects(resp, reverse("core:cart_detail"))

        # Verify no order created and stock remains intact
        self.assertFalse(Order.objects.filter(idempotency_key="token-insufficient-inv-001").exists())
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 2)

    # --------------------------------------------------------------------------
    # 11. COD Checkout Compatibility
    # --------------------------------------------------------------------------
    def test_cod_checkout_creates_packages_without_marking_paid(self):
        """COD checkout creates parent order and packages in UNPAID status."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-cod-compat-001",
        }
        resp = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(resp.status_code, 302)

        order = Order.objects.get(idempotency_key="token-cod-compat-001")
        self.assertEqual(order.payment_method, Order.PAYMENT_METHOD_COD)
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)
        self.assertIsNone(order.paid_at)

        pkg = order.seller_orders.first()
        self.assertEqual(pkg.status, SellerOrder.STATUS_PENDING)

    # --------------------------------------------------------------------------
    # 12. Razorpay Order Creation and Signature Verification
    # --------------------------------------------------------------------------
    @patch("razorpay.Client")
    def test_razorpay_checkout_flow_and_signature_verification(self, mock_razorpay_cls):
        """Razorpay checkout creates gateway order with verified total, and signature verification marks PAID."""
        mock_client = MagicMock()
        mock_razorpay_cls.return_value = mock_client
        mock_client.order.create.return_value = {"id": "order_mock_rp_12345"}
        mock_client.utility.verify_payment_signature.return_value = True

        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        with self.settings(RAZORPAY_ENABLED=True, RAZORPAY_KEY_ID="rzp_test_key", RAZORPAY_KEY_SECRET="rzp_secret"):
            payload = {
                "address_mode": "existing",
                "address_id": self.shipping_address.pk,
                "selected_address": self.shipping_address.pk,
                "payment_method": "razorpay",
                "idempotency_token": "token-rzp-001",
            }
            resp = self.client.post(reverse("core:checkout"), payload)
            self.assertEqual(resp.status_code, 302)

            order = Order.objects.get(idempotency_key="token-rzp-001")
            self.assertEqual(order.payment_method, Order.PAYMENT_METHOD_RAZORPAY)
            self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)
            self.assertEqual(order.razorpay_order_id, "order_mock_rp_12345")

            # Verify signature callback
            verify_payload = {
                "order_number": order.order_number,
                "razorpay_order_id": "order_mock_rp_12345",
                "razorpay_payment_id": "pay_mock_999",
                "razorpay_signature": "mock_signature_hash",
            }
            verify_resp = self.client.post(reverse("core:razorpay_payment_verify"), verify_payload)
            self.assertEqual(verify_resp.status_code, 302)

            order.refresh_from_db()
            self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
            self.assertIsNotNone(order.paid_at)
            self.assertEqual(order.razorpay_payment_id, "pay_mock_999")

    # --------------------------------------------------------------------------
    # 13. Payment Failure Synchronizes Child Packages & Restores Stock
    # --------------------------------------------------------------------------
    def test_razorpay_payment_failure_synchronizes_child_packages_and_restores_stock(self):
        """Payment failure updates parent to CANCELLED/FAILED, cancels child packages, and restores stock once."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 2})

        with patch("razorpay.Client") as mock_rp:
            mock_client = MagicMock()
            mock_rp.return_value = mock_client
            mock_client.order.create.return_value = {"id": "order_mock_fail_1"}

            with self.settings(RAZORPAY_ENABLED=True, RAZORPAY_KEY_ID="test", RAZORPAY_KEY_SECRET="secret"):
                self.client.post(reverse("core:checkout"), {
                    "address_mode": "existing",
                    "address_id": self.shipping_address.pk,
                    "payment_method": "razorpay",
                    "idempotency_token": "token-fail-sync-001",
                })

        order = Order.objects.get(idempotency_key="token-fail-sync-001")
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 18)

        # Trigger payment failure endpoint
        fail_payload = {
            "order_number": order.order_number,
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Payment was declined by customer bank",
        }
        fail_resp = self.client.post(reverse("core:razorpay_payment_failed"), fail_payload)
        self.assertEqual(fail_resp.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_FAILED)
        self.assertTrue(order.inventory_restored)

        # Check child package cancelled
        pkg = order.seller_orders.first()
        self.assertEqual(pkg.status, SellerOrder.STATUS_CANCELLED)

        # Inventory restored exactly once: 18 + 2 = 20
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 20)

    # --------------------------------------------------------------------------
    # 14. Order Cancellation Never Restores Stock Twice
    # --------------------------------------------------------------------------
    def test_cancellation_service_never_restores_stock_twice(self):
        """Invoking cancel_order on an already-restored order does not double-count inventory."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 3})

        self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-cancel-twice-001",
        })

        order = Order.objects.get(idempotency_key="token-cancel-twice-001")
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 17)  # 20 - 3

        # Cancel order
        success, msg, cancelled_order = cancel_order(order, user=self.customer, reason="Changed mind")
        self.assertTrue(success)
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 20)  # Restored to 20

        # Attempt to cancel again
        success2, msg2, _ = cancel_order(cancelled_order, user=self.customer)
        self.assertFalse(success2)

        # Stock quantity remains 20!
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, 20)

    # --------------------------------------------------------------------------
    # 15. Shipped / Delivered Packages Cannot Be Unsafely Cancelled
    # --------------------------------------------------------------------------
    def test_shipped_packages_prevent_unsafe_order_cancellation(self):
        """An order with any shipped package cannot be cancelled by the customer."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-shipped-block-001",
        })

        order = Order.objects.get(idempotency_key="token-shipped-block-001")
        pkg = order.seller_orders.first()
        pkg.status = SellerOrder.STATUS_SHIPPED
        pkg.save(update_fields=["status"])

        # Attempt cancellation
        success, msg, _ = cancel_order(order, user=self.customer, reason="Customer cancel attempt")
        self.assertFalse(success)
        self.assertIn("dispatched or delivered", msg)

        order.refresh_from_db()
        self.assertNotEqual(order.status, Order.STATUS_CANCELLED)

    # --------------------------------------------------------------------------
    # 16. Customer API Responses Do Not Expose Internal Financials
    # --------------------------------------------------------------------------
    def test_customer_api_order_detail_hides_seller_commissions_and_net_payables(self):
        """Customer-facing order detail API excludes commission rates, total commission, and net seller payable."""
        # Create order via API
        self.api_client.post(reverse("api:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 2})

        payload = {
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-api-cust-privacy-001",
        }
        resp = self.api_client.post(reverse("api:checkout_place_order"), payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        order_data = resp.json()["order"]
        self.assertEqual(len(order_data["seller_orders"]), 1)
        sub_order_data = order_data["seller_orders"][0]

        # Verify sensitive financial fields are NOT exposed
        self.assertNotIn("commission_rate", sub_order_data)
        self.assertNotIn("total_commission", sub_order_data)
        self.assertNotIn("net_seller_payable", sub_order_data)
        self.assertNotIn("actual_shipping_cost", sub_order_data)
        self.assertNotIn("shipping_allocated_to_seller", sub_order_data)
        self.assertNotIn("shipping_margin", sub_order_data)

        # Verify line items do NOT expose commission
        for it in order_data["items"]:
            self.assertNotIn("commission_rate", it)
            self.assertNotIn("commission_amount", it)

    # --------------------------------------------------------------------------
    # 17. Seller Data Isolation: One Seller Cannot Access Another's Package
    # --------------------------------------------------------------------------
    def test_seller_cannot_view_or_modify_another_sellers_package(self):
        """A seller querying packages or updating fulfillment cannot access other merchants' packages."""
        # Create order with items from Seller 1 and Seller 2
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller2.pk, "quantity": 1})

        self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-iso-seller-001",
        })

        order = Order.objects.get(idempotency_key="token-iso-seller-001")
        pkg1 = order.seller_orders.get(seller=self.seller1)
        pkg2 = order.seller_orders.get(seller=self.seller2)

        # Log in as Seller 1
        seller1_client = Client()
        seller1_client.login(username="seller1_p4@example.com", password="Password123!")

        # Seller 1 can access their own package
        resp_own = seller1_client.get(reverse("core:seller_order_detail", kwargs={"pk": pkg1.pk}))
        self.assertEqual(resp_own.status_code, 200)

        # Seller 1 attempting to view Seller 2's package returns 404 (strictly isolated)
        resp_other = seller1_client.get(reverse("core:seller_order_detail", kwargs={"pk": pkg2.pk}))
        self.assertEqual(resp_other.status_code, 404)

    # --------------------------------------------------------------------------
    # 18. Historical Orders Without SellerOrder Packages Remain Readable
    # --------------------------------------------------------------------------
    def test_historical_orders_without_seller_orders_remain_readable(self):
        """Historical orders with legacy OrderItems (seller_order=None) render properly without errors."""
        legacy_order = Order.objects.create(
            order_number="FM-HISTORICAL-001",
            user=self.customer,
            shipping_full_name="Anita Roy",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Old Road",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1000.00"),
            shipping_fee=Decimal("49.00"),
            total_amount=Decimal("1049.00"),
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_PENDING,
        )
        OrderItem.objects.create(
            order=legacy_order,
            product=self.prod_seller1,
            product_name=self.prod_seller1.name,
            sku=self.prod_seller1.sku,
            unit_price=Decimal("1000.00"),
            quantity=1,
            line_total=Decimal("1000.00"),
            seller=None,
            seller_order=None,  # Legacy item without package link
            commission_rate=Decimal("0.00"),
            commission_amount=Decimal("0.00"),
        )

        # 1. Customer web detail view renders 200 OK
        self.client.login(username="customer_p4@example.com", password="Password123!")
        web_resp = self.client.get(reverse("core:order_detail", kwargs={"order_number": legacy_order.order_number}))
        self.assertEqual(web_resp.status_code, 200)
        self.assertContains(web_resp, "FM-HISTORICAL-001")
        self.assertContains(web_resp, "Alpha Running Shorts")

        # 2. Customer API detail view returns 200 OK
        api_resp = self.api_client.get(reverse("api:order_detail", kwargs={"order_number": legacy_order.order_number}))
        self.assertEqual(api_resp.status_code, status.HTTP_200_OK)
        data = api_resp.json()["order"]
        self.assertEqual(data["order_number"], "FM-HISTORICAL-001")
        self.assertEqual(len(data["seller_orders"]), 0)  # Empty packages handled gracefully
        self.assertEqual(len(data["items"]), 1)
        self.assertIsNone(data["items"][0]["seller_order_id"])

    # --------------------------------------------------------------------------
    # 19. Exact Mathematical Audit: Subtotals, Lines, Packages, and Parent
    # --------------------------------------------------------------------------
    def test_mixed_cart_exact_financial_totals_and_package_associations(self):
        """
        Rigorous financial check:
        - Parent subtotal == sum of OrderItem line totals.
        - Sum of package subtotals == parent subtotal.
        - Each package subtotal == sum of its own items' line totals.
        - Each package commission == sum of its items' commission amounts.
        - Net seller payable == subtotal - total_commission.
        - Platform items have 0% commission and are not treated as vendor revenue.
        - Every OrderItem is accounted for in exactly one package with no omissions or duplicates.
        """
        # Create products with fractional amounts to test Decimal rounding
        p_seller1 = Product.objects.create(
            name="Alpha Grip Socks",
            slug="alpha-grip-socks",
            sku="ALP-SOX-001",
            category=self.category,
            seller=self.seller1,
            price=Decimal("999.50"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        p_seller2 = Product.objects.create(
            name="Beta Wrist Straps",
            slug="beta-wrist-straps",
            sku="BET-WST-001",
            category=self.category,
            seller=self.seller2,
            price=Decimal("499.75"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        p_platform = Product.objects.create(
            name="Fitmeezo Shaker Bottle",
            slug="fitmeezo-shaker-bottle",
            sku="FM-SHK-001",
            category=self.category,
            seller=None,
            price=Decimal("750.25"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        self.client.login(username="customer_p4@example.com", password="Password123!")
        # 2 units of p_seller1 = 1999.00
        self.client.post(reverse("core:cart_add"), {"product_id": p_seller1.pk, "quantity": 2})
        # 3 units of p_seller2 = 1499.25
        self.client.post(reverse("core:cart_add"), {"product_id": p_seller2.pk, "quantity": 3})
        # 1 unit of p_platform = 750.25
        self.client.post(reverse("core:cart_add"), {"product_id": p_platform.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-math-audit-001",
        }
        self.client.post(reverse("core:checkout"), payload)

        order = Order.objects.get(idempotency_key="token-math-audit-001")

        # 1. Parent subtotal calculation
        expected_subtotal = Decimal("1999.00") + Decimal("1499.25") + Decimal("750.25")
        self.assertEqual(expected_subtotal, Decimal("4248.50"))
        self.assertEqual(order.subtotal, Decimal("4248.50"))
        self.assertEqual(order.shipping_fee, Decimal("0.00"))  # >= 499 free shipping
        self.assertEqual(order.total_amount, Decimal("4248.50"))

        # 2. Sum of line totals equals parent subtotal
        items = list(order.items.all())
        self.assertEqual(len(items), 3)
        sum_line_totals = sum(item.line_total for item in items)
        self.assertEqual(sum_line_totals, order.subtotal)

        # 3. Exactly 3 packages (2 sellers + 1 platform)
        packages = list(order.seller_orders.all())
        self.assertEqual(len(packages), 3)

        # Sum of package subtotals equals parent subtotal
        sum_pkg_subtotals = sum(pkg.subtotal for pkg in packages)
        self.assertEqual(sum_pkg_subtotals, order.subtotal)

        # 4. Package 1: Seller 1 (10% commission)
        pkg_s1 = order.seller_orders.get(seller=self.seller1)
        self.assertEqual(pkg_s1.subtotal, Decimal("1999.00"))
        self.assertEqual(pkg_s1.total_commission, Decimal("199.90"))
        self.assertEqual(pkg_s1.net_seller_payable, Decimal("1799.10"))
        self.assertEqual(pkg_s1.total_commission + pkg_s1.net_seller_payable, pkg_s1.subtotal)

        # 5. Package 2: Seller 2 (15% commission)
        # 1499.25 * 0.15 = 224.8875 -> 224.89
        pkg_s2 = order.seller_orders.get(seller=self.seller2)
        self.assertEqual(pkg_s2.subtotal, Decimal("1499.25"))
        self.assertEqual(pkg_s2.total_commission, Decimal("224.89"))
        self.assertEqual(pkg_s2.net_seller_payable, Decimal("1274.36"))
        self.assertEqual(pkg_s2.total_commission + pkg_s2.net_seller_payable, pkg_s2.subtotal)

        # 6. Package 3: Platform merchandise (0% commission)
        pkg_plt = order.seller_orders.get(is_platform_fulfilled=True)
        self.assertIsNone(pkg_plt.seller)
        self.assertEqual(pkg_plt.subtotal, Decimal("750.25"))
        self.assertEqual(pkg_plt.total_commission, Decimal("0.00"))
        self.assertEqual(pkg_plt.net_seller_payable, Decimal("750.25"))

        # 7. Package item uniqueness: every item belongs to exactly one package
        accounted_items = set()
        for pkg in packages:
            pkg_items = list(pkg.items.all())
            self.assertEqual(pkg.subtotal, sum(i.line_total for i in pkg_items))
            self.assertEqual(pkg.total_commission, sum(i.commission_amount for i in pkg_items))
            for i in pkg_items:
                self.assertNotIn(i.id, accounted_items)
                accounted_items.add(i.id)
                self.assertEqual(i.order_id, order.id)
                self.assertEqual(i.seller_order_id, pkg.id)

        self.assertEqual(len(accounted_items), len(items))

    # --------------------------------------------------------------------------
    # 20. Razorpay Amount Sourced Exclusively From Persisted Parent Total
    # --------------------------------------------------------------------------
    @patch("razorpay.Client")
    def test_razorpay_amount_sourced_exclusively_from_persisted_parent_order_total(self, mock_rp):
        """Gateway amount in paise is calculated strictly from persisted parent order total."""
        mock_client = MagicMock()
        mock_rp.return_value = mock_client
        mock_client.order.create.return_value = {"id": "order_mock_rp_strict"}

        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        with self.settings(RAZORPAY_ENABLED=True, RAZORPAY_KEY_ID="test", RAZORPAY_KEY_SECRET="sec"):
            self.client.post(reverse("core:checkout"), {
                "address_mode": "existing",
                "address_id": self.shipping_address.pk,
                "payment_method": "razorpay",
                "idempotency_token": "token-rp-strict-total",
            })

            order = Order.objects.get(idempotency_key="token-rp-strict-total")
            expected_paise = int((order.total_amount * 100).to_integral_value())

            # Verify mock was called with exact paise matching order.total_amount
            mock_client.order.create.assert_called_once()
            call_kwargs = mock_client.order.create.call_args[0][0]
            self.assertEqual(call_kwargs["amount"], expected_paise)
            self.assertEqual(call_kwargs["receipt"], order.order_number)

            # Verify payment page context matches
            page_resp = self.client.get(reverse("core:razorpay_payment_page", kwargs={"order_number": order.order_number}))
            self.assertEqual(page_resp.context["amount_in_paise"], expected_paise)

    # --------------------------------------------------------------------------
    # 21. Razorpay Callback Security & Mismatch Rejection
    # --------------------------------------------------------------------------
    @patch("razorpay.Client")
    def test_razorpay_invalid_signature_and_mismatched_order_rejected(self, mock_rp):
        """Invalid signatures, mismatched order IDs, and wrong payment methods are strictly rejected."""
        mock_client = MagicMock()
        mock_rp.return_value = mock_client
        mock_client.utility.verify_payment_signature.side_effect = Exception("Signature verification failed")

        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        with patch("razorpay.Client.order") as mock_order:
            mock_order_instance = MagicMock()
            mock_client.order = mock_order_instance
            mock_order_instance.create.return_value = {"id": "order_rp_legit_001"}

            with self.settings(RAZORPAY_ENABLED=True, RAZORPAY_KEY_ID="test", RAZORPAY_KEY_SECRET="sec"):
                self.client.post(reverse("core:checkout"), {
                    "address_mode": "existing",
                    "address_id": self.shipping_address.pk,
                    "payment_method": "razorpay",
                    "idempotency_token": "token-rp-sec-audit",
                })

        order = Order.objects.get(idempotency_key="token-rp-sec-audit")

        # 1. Invalid signature rejected
        verify_resp1 = self.client.post(reverse("core:razorpay_payment_verify"), {
            "order_number": order.order_number,
            "razorpay_order_id": "order_rp_legit_001",
            "razorpay_payment_id": "pay_fake_001",
            "razorpay_signature": "tampered_signature",
        })
        self.assertEqual(verify_resp1.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)
        self.assertIsNone(order.paid_at)

        # 2. Mismatched order ID rejected without verifying signature
        verify_resp2 = self.client.post(reverse("core:razorpay_payment_verify"), {
            "order_number": order.order_number,
            "razorpay_order_id": "order_rp_WRONG_999",
            "razorpay_payment_id": "pay_fake_002",
            "razorpay_signature": "some_signature",
        })
        self.assertEqual(verify_resp2.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)

    # --------------------------------------------------------------------------
    # 22. Razorpay Callback on COD Order Rejected
    # --------------------------------------------------------------------------
    def test_razorpay_callback_on_cod_order_rejected(self):
        """Attempting to trigger Razorpay verify callback on a COD order is blocked."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-cod-no-rp-callback",
        })

        cod_order = Order.objects.get(idempotency_key="token-cod-no-rp-callback")

        # Verify callback on COD order rejected
        resp_v = self.client.post(reverse("core:razorpay_payment_verify"), {
            "order_number": cod_order.order_number,
            "razorpay_order_id": "order_injected",
            "razorpay_payment_id": "pay_injected",
            "razorpay_signature": "sig_injected",
        })
        self.assertEqual(resp_v.status_code, 302)
        cod_order.refresh_from_db()
        self.assertEqual(cod_order.payment_status, Order.PAYMENT_STATUS_UNPAID)

        # Failed callback on an already PAID order does not alter paid status or duplicate inventory
        cod_order.payment_status = Order.PAYMENT_STATUS_PAID
        cod_order.status = Order.STATUS_PROCESSING
        cod_order.save(update_fields=["payment_status", "status"])

        resp_f = self.client.post(reverse("core:razorpay_payment_failed"), {
            "order_number": cod_order.order_number,
        })
        self.assertEqual(resp_f.status_code, 302)
        cod_order.refresh_from_db()
        self.assertEqual(cod_order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertNotEqual(cod_order.status, Order.STATUS_CANCELLED)

    # --------------------------------------------------------------------------
    # 23. Failed Transaction Rollback
    # --------------------------------------------------------------------------
    def test_failed_checkout_transaction_rolls_back_atomically(self):
        """If an error occurs midway through checkout, no partial records or stock deductions remain."""
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 2})

        initial_stock = self.prod_seller1.stock_quantity

        # Mock SellerOrder.save to raise an unexpected database exception
        with patch.object(SellerOrder, "save", side_effect=Exception("Simulated database failure")):
            resp = self.client.post(reverse("core:checkout"), {
                "address_mode": "existing",
                "address_id": self.shipping_address.pk,
                "payment_method": "cod",
                "idempotency_token": "token-rollback-audit",
            })
            self.assertEqual(resp.status_code, 302)

        # Verify parent Order was rolled back completely
        self.assertFalse(Order.objects.filter(idempotency_key="token-rollback-audit").exists())
        self.assertFalse(SellerOrder.objects.filter(sub_order_number__startswith="FM-").exists())
        self.assertFalse(OrderItem.objects.filter(product=self.prod_seller1, quantity=2).exists())

        # Stock was NOT deducted
        self.prod_seller1.refresh_from_db()
        self.assertEqual(self.prod_seller1.stock_quantity, initial_stock)

    # --------------------------------------------------------------------------
    # 24. Customer Order Ownership Isolation
    # --------------------------------------------------------------------------
    def test_customer_cannot_access_another_customers_order(self):
        """Customer B cannot view Customer A's order on the website or via REST API."""
        # Customer A order
        self.client.login(username="customer_p4@example.com", password="Password123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})
        self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-cust-iso-001",
        })
        order = Order.objects.get(idempotency_key="token-cust-iso-001")

        # Create Customer B
        customer_b = User.objects.create_user(
            username="customer_b_p4@example.com",
            email="customer_b_p4@example.com",
            password="Password123!",
        )
        client_b = Client()
        client_b.login(username="customer_b_p4@example.com", password="Password123!")

        # Customer B web access -> 404
        web_resp = client_b.get(reverse("core:order_detail", kwargs={"order_number": order.order_number}))
        self.assertEqual(web_resp.status_code, 404)

        # Customer B API access -> 404
        token_b = Token.objects.create(user=customer_b)
        api_client_b = APIClient()
        api_client_b.credentials(HTTP_AUTHORIZATION=f"Token {token_b.key}")
        api_resp = api_client_b.get(reverse("api:order_detail", kwargs={"order_number": order.order_number}))
        self.assertEqual(api_resp.status_code, status.HTTP_404_NOT_FOUND)
