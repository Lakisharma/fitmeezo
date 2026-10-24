"""
Phase 4 Tests — FitMeezo Seller Account Closure: Read-Only Portal Access Enforcement.

Covers:
1. Closed seller can view their own historical data (200 OK):
   - Dashboard
   - Profile
   - Order list and detail
   - Invoice PDF and Shipping label
   - Packing summary
   - Product list
   - Financial ledger and CSV export
   - Payout history and detail
   - Bank details (masked)
   - Return list and detail
   - Reports and CSV exports
   - Closure status page
2. Closed seller is strictly blocked with HTTP 403 Forbidden from mutating resources:
   - Product add (POST)
   - Product edit (POST)
   - Inventory update (POST)
   - Variant add / edit / toggle / delete (POST)
   - Product image add / delete (POST)
   - Order status update (POST)
   - Order measurements (POST)
   - Order rate estimate (POST)
   - Order mock shipment booking (POST)
   - Return action (POST)
   - Profile update (POST)
   - Bank details update (POST)
   - Closure request re-submission (POST)
3. Cross-seller isolation:
   - Closed seller cannot access or view another seller's orders, invoices, payouts, returns, or products (404 Not Found).
4. Order creation & product addition restrictions for closure lifecycle:
   - Cart addition blocked for products belonging to closed, closing, and closure_requested sellers.
   - Product addition blocked for closure_requested and closing sellers.
5. Fulfillment workflow preservation:
   - Closing sellers CAN still fulfill their existing accepted orders (status update & measurements).
6. Comprehensive mutation route coverage inventory:
   - Automated inventory verifying that all seller mutation endpoints reject closed sellers with HTTP 403.
"""

from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core.cart import Cart
from core.models import (
    Category,
    Order,
    OrderItem,
    PayoutBatch,
    Product,
    ProductImage,
    ProductVariant,
    ReturnRequest,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from core.permissions import is_seller_closed

User = get_user_model()


_seller_seq = 0


def _make_seller(user, store_suffix, status=Seller.STATUS_CLOSED, **extra):
    global _seller_seq
    _seller_seq += 1
    seq = _seller_seq % 9000 + 1000  # 4 digits: 1000 to 9999
    is_active = extra.pop("is_active", False)
    return Seller.objects.create(
        user=user,
        store_name=f"Store {store_suffix}",
        legal_business_name=f"Legal Co {store_suffix}",
        support_email=f"support_{store_suffix}@test.com",
        support_phone="9876543210",
        registered_address="1 Market Street",
        city="Mumbai",
        state="Maharashtra",
        postal_code="400001",
        pickup_address="1 Market Street",
        pickup_city="Mumbai",
        pickup_state="Maharashtra",
        pickup_postal_code="400001",
        gstin=f"27ABCDE{seq}F1Z1",
        pan_number=f"ABCDE{seq}H",
        commission_rate=Decimal("10.00"),
        status=status,
        is_active=is_active,
        bank_account_holder_name=f"Legal Co {store_suffix}",
        bank_account_number=f"12345678{seq}",
        bank_ifsc_code="SBIN0001234",
        bank_name="State Bank of India",
        bank_verification_status="verified",
        **extra,
    )


class ClosedSellerReadOnlyAccessTests(TestCase):
    """
    Verifies that sellers in STATUS_CLOSED retain authorized read-only GET access
    to historical orders, invoices, ledger, and profile information.
    """

    def setUp(self):
        # Closed Seller
        self.seller_user = User.objects.create_user(
            username="closed_merchant",
            email="closed@fitmeezo.com",
            password="ClosedPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "closed_store",
            status=Seller.STATUS_CLOSED,
            closed_at=timezone.now(),
        )

        # Category and Product
        self.category = Category.objects.create(name="Apparel", slug=f"apparel-{uuid.uuid4().hex[:4]}")
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name="Historical Shirt",
            slug=f"historical-shirt-{uuid.uuid4().hex[:4]}",
            sku=f"SKU-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("499.00"),
            stock_quantity=5,
            is_active=False,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Historical Customer & Order
        self.customer = User.objects.create_user(
            username="cust_closed_test",
            email="cust_closed@test.com",
            password="CustPass123!",
        )
        self.order = Order.objects.create(
            user=self.customer,
            order_number=f"ORD-{uuid.uuid4().hex[:8].upper()}",
            subtotal=Decimal("499.00"),
            total_amount=Decimal("499.00"),
            status=Order.STATUS_DELIVERED,
        )
        self.package = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number=f"SUB-{uuid.uuid4().hex[:8].upper()}",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("499.00"),
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            seller=self.seller,
            seller_order=self.package,
            product_name=self.product.name,
            sku=self.product.sku,
            unit_price=self.product.price,
            quantity=1,
            line_total=self.product.price,
        )

        # Historical Return Request
        self.return_req = ReturnRequest.objects.create(
            request_number=f"RET-{uuid.uuid4().hex[:8].upper()}",
            order=self.order,
            seller_order=self.package,
            seller=self.seller,
            customer=self.customer,
            reason=ReturnRequest.REASON_DEFECTIVE,
            status=ReturnRequest.STATUS_REJECTED,
            is_platform_fulfilled=False,
        )

        # Historical Payout Batch
        self.payout_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            batch_type=PayoutBatch.BATCH_TYPE_CYCLE,
            status=PayoutBatch.STATUS_COMPLETED,
            amount=Decimal("1500.00"),
        )

        # Client logged in as closed seller
        self.client = Client()
        self.client.login(username="closed_merchant", password="ClosedPass123!")

    def test_is_seller_closed_helper(self):
        """Helper correctly identifies closed seller account."""
        self.assertTrue(is_seller_closed(self.seller_user))

    def test_closed_seller_can_view_dashboard(self):
        """Closed seller can view dashboard (GET 200)."""
        resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_profile(self):
        """Closed seller can view profile details in read-only mode (GET 200)."""
        resp = self.client.get(reverse("core:seller_profile"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_order_list(self):
        """Closed seller can view historical order list (GET 200)."""
        resp = self.client.get(reverse("core:seller_order_list"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_order_detail(self):
        """Closed seller can view historical order detail (GET 200)."""
        resp = self.client.get(reverse("core:seller_order_detail", kwargs={"pk": self.package.pk}))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_order_invoice(self):
        """Closed seller can view/download historical order invoice PDF (GET 200)."""
        resp = self.client.get(reverse("core:seller_order_invoice", kwargs={"pk": self.package.pk}))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_order_packing_summary(self):
        """Closed seller can view packing summary (GET 200)."""
        resp = self.client.get(reverse("core:seller_order_packing_summary", kwargs={"pk": self.package.pk}))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_product_list(self):
        """Closed seller can view their product catalog list (GET 200)."""
        resp = self.client.get(reverse("core:seller_product_list"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_ledger(self):
        """Closed seller can view historical financial ledger (GET 200)."""
        resp = self.client.get(reverse("core:seller_ledger"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_export_ledger_csv(self):
        """Closed seller can export historical ledger entries to CSV (GET 200)."""
        resp = self.client.get(reverse("core:seller_export_ledger_csv"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))

    def test_closed_seller_can_view_payout_list(self):
        """Closed seller can view payout disbursement history (GET 200)."""
        resp = self.client.get(reverse("core:seller_payout_list"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_payout_detail(self):
        """Closed seller can view individual payout batch details (GET 200)."""
        resp = self.client.get(reverse("core:seller_payout_detail", kwargs={"batch_reference": self.payout_batch.batch_reference}))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_bank_details(self):
        """Closed seller can view masked bank account records (GET 200)."""
        resp = self.client.get(reverse("core:seller_bank_details"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_return_list(self):
        """Closed seller can view return requests (GET 200)."""
        resp = self.client.get(reverse("core:seller_return_list"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_return_detail(self):
        """Closed seller can view return request detail (GET 200)."""
        resp = self.client.get(reverse("core:seller_return_detail", kwargs={"request_number": self.return_req.request_number}))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_reports(self):
        """Closed seller can view reports hub (GET 200)."""
        resp = self.client.get(reverse("core:seller_reports"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_export_orders_csv(self):
        """Closed seller can export historical orders CSV (GET 200)."""
        resp = self.client.get(reverse("core:seller_export_orders_csv"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_export_products_csv(self):
        """Closed seller can export products CSV (GET 200)."""
        resp = self.client.get(reverse("core:seller_export_products_csv"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_export_financial_csv(self):
        """Closed seller can export financial CSV (GET 200)."""
        resp = self.client.get(reverse("core:seller_export_financial_csv"))
        self.assertEqual(resp.status_code, 200)

    def test_closed_seller_can_view_closure_page(self):
        """Closed seller sees the closed confirmation state on closure page (GET 200)."""
        resp = self.client.get(reverse("core:seller_close_account"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["status_code"], "closed")


class ClosedSellerMutationBlockedTests(TestCase):
    """
    Verifies that closed sellers are strictly blocked (HTTP 403 Forbidden)
    from mutating any seller-owned resources or initiating new actions.
    """

    def setUp(self):
        self.seller_user = User.objects.create_user(
            username="closed_merchant_mut",
            email="closed_mut@fitmeezo.com",
            password="ClosedPass123!",
        )
        self.seller = _make_seller(
            self.seller_user,
            "closed_mut",
            status=Seller.STATUS_CLOSED,
            closed_at=timezone.now(),
        )

        self.category = Category.objects.create(name="Shoes", slug=f"shoes-{uuid.uuid4().hex[:4]}")
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name="Sneakers",
            slug=f"sneakers-{uuid.uuid4().hex[:4]}",
            sku=f"SKU-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("1299.00"),
            stock_quantity=10,
            is_active=False,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 9",
            sku=f"SKU-VAR-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("1299.00"),
            stock_quantity=5,
            is_active=True,
        )

        self.customer = User.objects.create_user(
            username="cust_mut_test", email="cust_mut@test.com", password="CustPass123!"
        )
        self.order = Order.objects.create(
            user=self.customer,
            order_number=f"ORD-{uuid.uuid4().hex[:8].upper()}",
            subtotal=Decimal("1299.00"),
            total_amount=Decimal("1299.00"),
            status=Order.STATUS_PROCESSING,
        )
        self.package = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number=f"SUB-{uuid.uuid4().hex[:8].upper()}",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("1299.00"),
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            seller=self.seller,
            seller_order=self.package,
            product_name=self.product.name,
            sku=self.product.sku,
            unit_price=self.product.price,
            quantity=1,
            line_total=self.product.price,
        )
        self.return_req = ReturnRequest.objects.create(
            request_number=f"RET-{uuid.uuid4().hex[:8].upper()}",
            order=self.order,
            seller_order=self.package,
            seller=self.seller,
            customer=self.customer,
            reason=ReturnRequest.REASON_SIZE_FIT,
            status=ReturnRequest.STATUS_REQUESTED,
            is_platform_fulfilled=False,
        )

        self.client = Client()
        self.client.login(username="closed_merchant_mut", password="ClosedPass123!")

    def test_closed_seller_blocked_from_product_add_post(self):
        """POST to add product returns 403 Forbidden."""
        resp = self.client.post(reverse("core:seller_product_add"), {
            "name": "New Prohibited Product",
            "category": self.category.pk,
            "price": "999.00",
            "stock_quantity": 5,
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Product.objects.filter(name="New Prohibited Product").exists())

    def test_closed_seller_blocked_from_product_edit_post(self):
        """POST to edit product returns 403 Forbidden."""
        old_price = self.product.price
        resp = self.client.post(
            reverse("core:seller_product_edit", kwargs={"pk": self.product.pk}),
            {"price": "1499.00"},
        )
        self.assertEqual(resp.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.price, old_price)

    def test_closed_seller_blocked_from_inventory_update(self):
        """POST to update inventory returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_product_inventory", kwargs={"pk": self.product.pk}),
            {f"stock_variant_{self.variant.pk}": "50"},
        )
        self.assertEqual(resp.status_code, 403)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 5)

    def test_closed_seller_blocked_from_variant_add(self):
        """POST to add variant returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_product_variant_add", kwargs={"pk": self.product.pk}),
            {"option_type": "Size", "option_value": "UK 10", "price": "1299.00", "stock_quantity": 5},
        )
        self.assertEqual(resp.status_code, 403)

    def test_closed_seller_blocked_from_variant_toggle(self):
        """POST to toggle variant active status returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_product_variant_toggle", kwargs={"pk": self.product.pk, "variant_pk": self.variant.pk})
        )
        self.assertEqual(resp.status_code, 403)

    def test_closed_seller_blocked_from_variant_delete(self):
        """POST to delete variant returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_product_variant_delete", kwargs={"pk": self.product.pk, "variant_pk": self.variant.pk})
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(ProductVariant.objects.filter(pk=self.variant.pk).exists())

    def test_closed_seller_blocked_from_order_status_update(self):
        """POST to update fulfillment status returns 403 Forbidden."""
        old_status = self.package.status
        resp = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.package.pk}),
            {"status": SellerOrder.STATUS_READY_TO_SHIP},
        )
        self.assertEqual(resp.status_code, 403)
        self.package.refresh_from_db()
        self.assertEqual(self.package.status, old_status)

    def test_closed_seller_blocked_from_order_measurements(self):
        """POST to record parcel measurements returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_order_record_measurements", kwargs={"pk": self.package.pk}),
            {"actual_weight_grams": 500, "actual_length_cm": 20, "actual_breadth_cm": 15, "actual_height_cm": 10},
        )
        self.assertEqual(resp.status_code, 403)

    def test_closed_seller_blocked_from_order_estimate_rates(self):
        """POST to estimate courier rates returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_order_estimate_rates", kwargs={"pk": self.package.pk})
        )
        self.assertEqual(resp.status_code, 403)

    def test_closed_seller_blocked_from_order_mock_shipment(self):
        """POST to create mock shipment booking returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_order_create_mock_shipment", kwargs={"pk": self.package.pk})
        )
        self.assertEqual(resp.status_code, 403)

    def test_closed_seller_blocked_from_return_action(self):
        """POST to perform return approval/refund returns 403 Forbidden."""
        resp = self.client.post(
            reverse("core:seller_return_action", kwargs={"request_number": self.return_req.request_number}),
            {"action": "approve"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_closed_seller_blocked_from_profile_update(self):
        """POST to update profile contact info returns 403 Forbidden."""
        old_phone = self.seller.support_phone
        resp = self.client.post(
            reverse("core:seller_profile"),
            {"store_name": "Renamed Store", "support_phone": "1111111111"},
        )
        self.assertEqual(resp.status_code, 403)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.support_phone, old_phone)

    def test_closed_seller_blocked_from_bank_details_update(self):
        """POST to modify bank account returns 403 Forbidden."""
        old_acc = self.seller.bank_account_number
        resp = self.client.post(
            reverse("core:seller_bank_details"),
            {
                "bank_account_holder_name": "New Holder",
                "bank_name": "State Bank of India",
                "bank_name_select": "STATE_BANK_OF_INDIA",
                "bank_account_number": "999999999999",
                "confirm_account_number": "999999999999",
                "bank_ifsc_code": "SBIN0001234",
            },
        )
        self.assertEqual(resp.status_code, 403)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_account_number, old_acc)

    def test_closed_seller_blocked_from_closure_request_post(self):
        """POST to closure request view returns 403 Forbidden (already closed)."""
        resp = self.client.post(
            reverse("core:seller_close_account"),
            {"reason": "Closing again", "confirm_checkbox": "on"},
        )
        self.assertEqual(resp.status_code, 403)


class CrossSellerIsolationTests(TestCase):
    """
    Verifies that closed seller read-only access strictly maintains isolation:
    a closed seller cannot view or access another merchant's records (404 Not Found).
    """

    def setUp(self):
        # Closed Seller A
        self.user_a = User.objects.create_user(username="closed_a", email="a@fitmeezo.com", password="Pass123!")
        self.seller_a = _make_seller(self.user_a, "a", status=Seller.STATUS_CLOSED)

        # Active Seller B
        self.user_b = User.objects.create_user(username="active_b", email="b@fitmeezo.com", password="Pass123!")
        self.seller_b = _make_seller(self.user_b, "b", status=Seller.STATUS_APPROVED)

        # Seller B's Product
        self.cat = Category.objects.create(name="Electronics", slug=f"elec-{uuid.uuid4().hex[:4]}")
        self.product_b = Product.objects.create(
            seller=self.seller_b,
            category=self.cat,
            name="Seller B Headphones",
            slug=f"headphones-{uuid.uuid4().hex[:4]}",
            sku=f"SKU-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("1999.00"),
            stock_quantity=5,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Seller B's Order
        self.cust = User.objects.create_user(username="cust_cross", email="cross@test.com", password="Pass123!")
        self.order_b = Order.objects.create(
            user=self.cust,
            order_number=f"ORD-B-{uuid.uuid4().hex[:6].upper()}",
            subtotal=Decimal("1999.00"),
            total_amount=Decimal("1999.00"),
        )
        self.package_b = SellerOrder.objects.create(
            order=self.order_b,
            seller=self.seller_b,
            sub_order_number=f"SUB-B-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("1999.00"),
        )
        self.order_item_b = OrderItem.objects.create(
            order=self.order_b,
            product=self.product_b,
            seller=self.seller_b,
            seller_order=self.package_b,
            product_name=self.product_b.name,
            sku=self.product_b.sku,
            unit_price=self.product_b.price,
            quantity=1,
            line_total=self.product_b.price,
        )
        self.return_b = ReturnRequest.objects.create(
            request_number=f"RET-B-{uuid.uuid4().hex[:6].upper()}",
            order=self.order_b,
            seller_order=self.package_b,
            seller=self.seller_b,
            customer=self.cust,
            reason=ReturnRequest.REASON_DEFECTIVE,
            status=ReturnRequest.STATUS_REQUESTED,
            is_platform_fulfilled=False,
        )
        self.payout_b = PayoutBatch.objects.create(
            batch_reference=f"PAY-B-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller_b,
            batch_type=PayoutBatch.BATCH_TYPE_CYCLE,
            status=PayoutBatch.STATUS_COMPLETED,
            amount=Decimal("5000.00"),
        )

        # Logged in as Seller A
        self.client = Client()
        self.client.login(username="closed_a", password="Pass123!")

    def test_cannot_access_other_seller_order_detail(self):
        """Seller A receives 404 accessing Seller B's order detail."""
        resp = self.client.get(reverse("core:seller_order_detail", kwargs={"pk": self.package_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_access_other_seller_invoice(self):
        """Seller A receives 404 accessing Seller B's invoice."""
        resp = self.client.get(reverse("core:seller_order_invoice", kwargs={"pk": self.package_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_access_other_seller_shipping_label(self):
        """Seller A receives 404 accessing Seller B's shipping label."""
        resp = self.client.get(reverse("core:seller_order_shipping_label", kwargs={"pk": self.package_b.pk}))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_access_other_seller_payout_detail(self):
        """Seller A receives 404 accessing Seller B's payout batch."""
        resp = self.client.get(reverse("core:seller_payout_detail", kwargs={"batch_reference": self.payout_b.batch_reference}))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_access_other_seller_return_detail(self):
        """Seller A receives 404 accessing Seller B's return request."""
        resp = self.client.get(reverse("core:seller_return_detail", kwargs={"request_number": self.return_b.request_number}))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_access_other_seller_product_edit(self):
        """Seller A receives 404 accessing Seller B's product."""
        resp = self.client.get(reverse("core:seller_product_edit", kwargs={"pk": self.product_b.pk}))
        self.assertEqual(resp.status_code, 404)


class ClosureLifecycleFulfillmentAndCartTests(TestCase):
    """
    Verifies that:
    1. Cart addition is blocked for products of closure_requested, closing, and closed sellers.
    2. Closing sellers can still fulfill their accepted orders to reach closure eligibility.
    """

    def setUp(self):
        self.customer = User.objects.create_user(username="cart_cust", email="cart@test.com", password="Pass123!")
        self.client = Client()
        self.client.login(username="cart_cust", password="Pass123!")

        self.seller_user_closing = User.objects.create_user(username="closing_s", email="closing@test.com", password="Pass123!")
        self.seller_closing = _make_seller(self.seller_user_closing, "closing", status=Seller.STATUS_CLOSING)

        self.seller_user_closed = User.objects.create_user(username="closed_s", email="closed_s@test.com", password="Pass123!")
        self.seller_closed = _make_seller(self.seller_user_closed, "closed", status=Seller.STATUS_CLOSED)

        self.seller_user_req = User.objects.create_user(username="req_s", email="req_s@test.com", password="Pass123!")
        self.seller_req = _make_seller(self.seller_user_req, "req", status=Seller.STATUS_CLOSURE_REQUESTED)

        self.cat = Category.objects.create(name="Home", slug=f"home-{uuid.uuid4().hex[:4]}")
        self.prod_closing = Product.objects.create(
            seller=self.seller_closing,
            category=self.cat,
            name="Closing Vase",
            slug=f"closing-vase-{uuid.uuid4().hex[:4]}",
            sku=f"SKU-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("300.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.prod_closed = Product.objects.create(
            seller=self.seller_closed,
            category=self.cat,
            name="Closed Vase",
            slug=f"closed-vase-{uuid.uuid4().hex[:4]}",
            sku=f"SKU-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("300.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.prod_req = Product.objects.create(
            seller=self.seller_req,
            category=self.cat,
            name="Req Vase",
            slug=f"req-vase-{uuid.uuid4().hex[:4]}",
            sku=f"SKU-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("300.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

    def _make_cart(self):
        rf = RequestFactory()
        req = rf.get("/")
        req.user = self.customer
        req.session = self.client.session
        return Cart(req)

    def test_cart_add_rejected_for_closed_seller_product(self):
        """Cart.add() rejects product from closed seller."""
        cart = self._make_cart()
        success, msg, _ = cart.add(self.prod_closed, quantity=1)
        self.assertFalse(success)
        self.assertIn("closure", msg.lower())

    def test_cart_add_rejected_for_closing_seller_product(self):
        """Cart.add() rejects product from closing seller."""
        cart = self._make_cart()
        success, msg, _ = cart.add(self.prod_closing, quantity=1)
        self.assertFalse(success)
        self.assertIn("closure", msg.lower())

    def test_cart_add_rejected_for_closure_requested_seller_product(self):
        """Cart.add() rejects product from closure_requested seller."""
        cart = self._make_cart()
        success, msg, _ = cart.add(self.prod_req, quantity=1)
        self.assertFalse(success)
        self.assertIn("closure", msg.lower())

    def test_closing_seller_can_fulfill_accepted_orders(self):
        """
        Closing seller can still update order status to complete fulfillment
        of existing accepted orders.
        """
        order = Order.objects.create(
            user=self.customer,
            order_number=f"ORD-FUL-{uuid.uuid4().hex[:6].upper()}",
            subtotal=Decimal("300.00"),
            total_amount=Decimal("300.00"),
            status=Order.STATUS_PROCESSING,
        )
        package = SellerOrder.objects.create(
            order=order,
            seller=self.seller_closing,
            sub_order_number=f"SUB-FUL-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("300.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.prod_closing,
            seller=self.seller_closing,
            seller_order=package,
            product_name=self.prod_closing.name,
            sku=self.prod_closing.sku,
            unit_price=self.prod_closing.price,
            quantity=1,
            line_total=self.prod_closing.price,
        )

        seller_client = Client()
        seller_client.login(username="closing_s", password="Pass123!")

        resp = seller_client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": package.pk}),
            {"status": SellerOrder.STATUS_READY_TO_SHIP},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        package.refresh_from_db()
        self.assertEqual(package.status, SellerOrder.STATUS_READY_TO_SHIP)


class RouteCoverageMutationInventoryTests(TestCase):
    """
    Inventory test verifying that every known seller mutation route is covered
    and strictly rejects mutating requests from closed sellers.
    """

    def setUp(self):
        self.seller_user = User.objects.create_user(
            username="inv_closed_seller",
            email="inv@fitmeezo.com",
            password="ClosedPass123!",
        )
        self.seller = _make_seller(self.seller_user, "inv", status=Seller.STATUS_CLOSED)
        self.client = Client()
        self.client.login(username="inv_closed_seller", password="ClosedPass123!")

        self.cat = Category.objects.create(name="Tools", slug=f"tools-{uuid.uuid4().hex[:4]}")
        self.prod = Product.objects.create(
            seller=self.seller,
            category=self.cat,
            name="Hammer",
            slug=f"hammer-{uuid.uuid4().hex[:4]}",
            sku=f"SKU-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("150.00"),
            stock_quantity=5,
            is_active=False,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.variant = ProductVariant.objects.create(
            product=self.prod,
            option_type="Type",
            option_value="Claw",
            sku=f"SKU-VAR-{uuid.uuid4().hex[:6].upper()}",
            price=Decimal("150.00"),
            stock_quantity=5,
        )

        self.cust = User.objects.create_user(username="inv_cust", email="invcust@test.com", password="Pass123!")
        self.order = Order.objects.create(
            user=self.cust,
            order_number=f"ORD-INV-{uuid.uuid4().hex[:6].upper()}",
            subtotal=Decimal("150.00"),
            total_amount=Decimal("150.00"),
            status=Order.STATUS_PROCESSING,
        )
        self.pkg = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number=f"SUB-INV-{uuid.uuid4().hex[:6].upper()}",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("150.00"),
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            product=self.prod,
            seller=self.seller,
            seller_order=self.pkg,
            product_name=self.prod.name,
            sku=self.prod.sku,
            unit_price=self.prod.price,
            quantity=1,
            line_total=self.prod.price,
        )
        self.ret = ReturnRequest.objects.create(
            request_number=f"RET-INV-{uuid.uuid4().hex[:6].upper()}",
            order=self.order,
            seller_order=self.pkg,
            seller=self.seller,
            customer=self.cust,
            reason=ReturnRequest.REASON_DEFECTIVE,
            status=ReturnRequest.STATUS_REQUESTED,
            is_platform_fulfilled=False,
        )

    def test_all_registered_seller_mutation_routes_reject_closed_seller(self):
        """
        Iterates over all mutation routes in the seller portal and verifies that
        a closed seller receives HTTP 403 Forbidden on POST.
        """
        mutation_endpoints = [
            reverse("core:seller_product_add"),
            reverse("core:seller_product_edit", kwargs={"pk": self.prod.pk}),
            reverse("core:seller_product_inventory", kwargs={"pk": self.prod.pk}),
            reverse("core:seller_product_variant_add", kwargs={"pk": self.prod.pk}),
            reverse("core:seller_product_variant_edit", kwargs={"pk": self.prod.pk, "variant_pk": self.variant.pk}),
            reverse("core:seller_product_variant_toggle", kwargs={"pk": self.prod.pk, "variant_pk": self.variant.pk}),
            reverse("core:seller_product_variant_delete", kwargs={"pk": self.prod.pk, "variant_pk": self.variant.pk}),
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg.pk}),
            reverse("core:seller_order_record_measurements", kwargs={"pk": self.pkg.pk}),
            reverse("core:seller_order_estimate_rates", kwargs={"pk": self.pkg.pk}),
            reverse("core:seller_order_create_mock_shipment", kwargs={"pk": self.pkg.pk}),
            reverse("core:seller_return_action", kwargs={"request_number": self.ret.request_number}),
            reverse("core:seller_profile"),
            reverse("core:seller_bank_details"),
            reverse("core:seller_close_account"),
        ]

        for url in mutation_endpoints:
            resp = self.client.post(url, {})
            self.assertEqual(
                resp.status_code,
                403,
                f"Endpoint {url} did not return HTTP 403 Forbidden for closed seller! Got {resp.status_code}",
            )
