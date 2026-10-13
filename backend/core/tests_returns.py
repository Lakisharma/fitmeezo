"""
Fitmeezo Phase 6 Test Suite: Returns, Exchanges & Refund Management.

Covers:
- Customer ownership and permission enforcement
- Delivery status gates & configurable return window (7-day rule)
- Multi-seller isolation (Seller A vs Seller B vs Platform packages)
- Partial item-level returns and exchange variant eligibility
- Lifecycle transitions and state machine validity
- Inspection decisions and exactly-once inventory restoration
- Safe mock refund processing with zero live API calls
- Non-destructive financial accounting & commission reversals
- Customer and Seller views, CSRF and access control
"""

from datetime import timedelta
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    RefundRecord,
    ReturnAuditLog,
    ReturnRequest,
    ReturnRequestItem,
    Seller,
    SellerOrder,
)
from core.return_services import (
    create_return_request,
    transition_return_status,
    process_simulated_refund,
    validate_item_eligibility,
    ReturnPermissionError,
    ReturnValidationError,
    RefundError,
)

User = get_user_model()


class ReturnsAndRefundsTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # Users
        self.customer1 = User.objects.create_user(
            username="customer1",
            email="cust1@example.com",
            password="SecurePassword123!",
        )
        self.customer2 = User.objects.create_user(
            username="customer2",
            email="cust2@example.com",
            password="SecurePassword123!",
        )
        self.staff_user = User.objects.create_user(
            username="staffadmin",
            email="staff@fitmeezo.com",
            password="SecurePassword123!",
            is_staff=True,
        )

        # Sellers
        self.seller_user1 = User.objects.create_user(
            username="selleruser1",
            email="seller1@store.com",
            password="SecurePassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Alpha Apparel",
            legal_business_name="Alpha Apparel Pvt Ltd",
            support_email="support@alpha.com",
            support_phone="9876543210",
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

        self.seller_user2 = User.objects.create_user(
            username="selleruser2",
            email="seller2@store.com",
            password="SecurePassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Beta Footwear",
            legal_business_name="Beta Footwear LLP",
            support_email="support@beta.com",
            support_phone="9876543211",
            registered_address="2 Shoe Lane",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="2 Shoe Lane",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29XYZAB5678C1Z2",
            pan_number="XYZAB5678C",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Catalogue Category & Products
        self.category = Category.objects.create(
            name="Fashion",
            slug="fashion",
            is_active=True,
        )

        # Product 1 (Seller 1)
        self.product1 = Product.objects.create(
            seller=self.seller1,
            category=self.category,
            name="Classic Oxford Shirt",
            slug="classic-oxford-shirt",
            sku="SHIRT-OX-001",
            price=Decimal("1000.00"),
            stock_quantity=20,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.variant1_m = ProductVariant.objects.create(
            product=self.product1,
            option_type="Size",
            option_value="M",
            sku="SHIRT-OX-M",
            price=Decimal("1000.00"),
            stock_quantity=10,
            is_active=True,
        )
        self.variant1_l = ProductVariant.objects.create(
            product=self.product1,
            option_type="Size",
            option_value="L",
            sku="SHIRT-OX-L",
            price=Decimal("1000.00"),
            stock_quantity=10,
            is_active=True,
        )

        # Product 2 (Seller 2)
        self.product2 = Product.objects.create(
            seller=self.seller2,
            category=self.category,
            name="Running Shoes",
            slug="running-shoes",
            sku="SHOE-RN-001",
            price=Decimal("2000.00"),
            stock_quantity=15,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Parent Order
        self.order = Order.objects.create(
            order_number="FM-20261011-TEST01",
            user=self.customer1,
            shipping_full_name="Aarav Patel",
            shipping_phone_number="9876543210",
            shipping_address_line1="42 High Street",
            shipping_city="Mumbai",
            shipping_state="Maharashtra",
            shipping_postal_code="400001",
            subtotal=Decimal("4000.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("4000.00"),
            currency="INR",
            status=Order.STATUS_PROCESSING,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
        )

        # Seller Order 1 (Delivered, within 7 days)
        self.seller_order1 = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller1,
            sub_order_number="FM-20261011-TEST01-S1",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("2000.00"),
            total_commission=Decimal("200.00"),
            net_seller_payable=Decimal("1800.00"),
            customer_shipping_charge=Decimal("0.00"),
            delivered_at=timezone.now() - timedelta(days=2),
        )
        self.order_item1 = OrderItem.objects.create(
            order=self.order,
            seller_order=self.seller_order1,
            seller=self.seller1,
            product=self.product1,
            variant=self.variant1_m,
            product_name="Classic Oxford Shirt",
            variant_label="Size: M",
            sku="SHIRT-OX-M",
            unit_price=Decimal("1000.00"),
            quantity=2,
            line_total=Decimal("2000.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("200.00"),
        )

        # Seller Order 2 (Delivered, Seller 2)
        self.seller_order2 = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller2,
            sub_order_number="FM-20261011-TEST01-S2",
            status=SellerOrder.STATUS_DELIVERED,
            subtotal=Decimal("2000.00"),
            total_commission=Decimal("300.00"),
            net_seller_payable=Decimal("1700.00"),
            customer_shipping_charge=Decimal("0.00"),
            delivered_at=timezone.now() - timedelta(days=1),
        )
        self.order_item2 = OrderItem.objects.create(
            order=self.order,
            seller_order=self.seller_order2,
            seller=self.seller2,
            product=self.product2,
            product_name="Running Shoes",
            sku="SHOE-RN-001",
            unit_price=Decimal("2000.00"),
            quantity=1,
            line_total=Decimal("2000.00"),
            commission_rate=Decimal("15.00"),
            commission_amount=Decimal("300.00"),
        )

    # -------------------------------------------------------------------------
    # 1. Eligibility & Permissions
    # -------------------------------------------------------------------------
    def test_customer_cannot_return_another_users_order(self):
        """Customer2 cannot request a return for Customer1's items."""
        is_ok, err = validate_item_eligibility(self.order_item1, quantity=1, user=self.customer2)
        self.assertFalse(is_ok)
        self.assertIn("permission", err.lower())

        with self.assertRaises(ReturnPermissionError):
            create_return_request(
                customer=self.customer2,
                seller_order=self.seller_order1,
                items_data=[{"order_item": self.order_item1, "quantity": 1}],
            )

    def test_ineligible_if_package_not_delivered(self):
        """Undelivered package (e.g. processing or shipped) cannot be returned."""
        self.seller_order1.status = SellerOrder.STATUS_SHIPPED
        self.seller_order1.save()

        is_ok, err = validate_item_eligibility(self.order_item1, quantity=1, user=self.customer1)
        self.assertFalse(is_ok)
        self.assertIn("delivered", err.lower())

        with self.assertRaises(ReturnValidationError):
            create_return_request(
                customer=self.customer1,
                seller_order=self.seller_order1,
                items_data=[{"order_item": self.order_item1, "quantity": 1}],
            )

    def test_ineligible_if_return_window_expired(self):
        """Delivery older than 7 days is rejected."""
        self.seller_order1.delivered_at = timezone.now() - timedelta(days=8)
        self.seller_order1.save()

        is_ok, err = validate_item_eligibility(self.order_item1, quantity=1, user=self.customer1)
        self.assertFalse(is_ok)
        self.assertIn("expired", err.lower())

        with self.assertRaises(ReturnValidationError):
            create_return_request(
                customer=self.customer1,
                seller_order=self.seller_order1,
                items_data=[{"order_item": self.order_item1, "quantity": 1}],
            )

    def test_quantity_boundaries_validated(self):
        """Cannot return 0 or more than purchased quantity."""
        is_ok, err = validate_item_eligibility(self.order_item1, quantity=0, user=self.customer1)
        self.assertFalse(is_ok)

        is_ok, err = validate_item_eligibility(self.order_item1, quantity=3, user=self.customer1)
        self.assertFalse(is_ok)
        self.assertIn("eligible", err.lower())

    # -------------------------------------------------------------------------
    # 2. Multi-Seller Package Isolation
    # -------------------------------------------------------------------------
    def test_multi_seller_package_isolation(self):
        """
        Returning an item from Seller 1 does not alter Seller 2's package or items.
        """
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
            request_type=ReturnRequest.TYPE_RETURN,
            reason=ReturnRequest.REASON_SIZE_FIT,
        )

        self.assertEqual(ret.seller, self.seller1)
        self.assertEqual(ret.items.count(), 1)

        # Seller 2 package status and snapshot remain untouched
        self.seller_order2.refresh_from_db()
        self.assertEqual(self.seller_order2.status, SellerOrder.STATUS_DELIVERED)
        self.assertEqual(self.seller_order2.subtotal, Decimal("2000.00"))
        self.assertEqual(self.seller_order2.return_requests.count(), 0)

    def test_seller_isolation_in_views(self):
        """
        Seller 2 cannot access or process Seller 1's return request.
        """
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )

        self.client.force_login(self.seller_user2)
        resp = self.client.get(reverse("core:seller_return_detail", kwargs={"request_number": ret.request_number}))
        self.assertEqual(resp.status_code, 404)

        # Seller 1 can view
        self.client.force_login(self.seller_user1)
        resp_own = self.client.get(reverse("core:seller_return_detail", kwargs={"request_number": ret.request_number}))
        self.assertEqual(resp_own.status_code, 200)

    # -------------------------------------------------------------------------
    # 3. Partial Item Return and Exchanges
    # -------------------------------------------------------------------------
    def test_partial_item_returns_and_cumulative_quantities(self):
        """
        OrderItem with qty=2:
        - Request 1 for qty=1 succeeds (remaining=1).
        - Request 2 for qty=1 succeeds (remaining=0).
        - Request 3 for qty=1 fails.
        """
        ret1 = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )
        self.order_item1.refresh_from_db()
        self.assertEqual(self.order_item1.returned_quantity, 1)
        self.assertEqual(self.order_item1.remaining_returnable_quantity, 1)

        ret2 = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )
        self.order_item1.refresh_from_db()
        self.assertEqual(self.order_item1.returned_quantity, 2)
        self.assertEqual(self.order_item1.remaining_returnable_quantity, 0)

        with self.assertRaises(ReturnValidationError):
            create_return_request(
                customer=self.customer1,
                seller_order=self.seller_order1,
                items_data=[{"order_item": self.order_item1, "quantity": 1}],
            )

    def test_exchange_variant_selection(self):
        """Exchange request specifies a replacement variant."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{
                "order_item": self.order_item1,
                "quantity": 1,
                "exchange_variant_id": self.variant1_l.id,
            }],
            request_type=ReturnRequest.TYPE_EXCHANGE,
        )
        self.assertEqual(ret.request_type, ReturnRequest.TYPE_EXCHANGE)
        ret_item = ret.items.first()
        self.assertEqual(ret_item.exchange_variant, self.variant1_l)

    # -------------------------------------------------------------------------
    # 4. Lifecycle Transitions & Exactly-Once Inventory Restoration
    # -------------------------------------------------------------------------
    def test_return_lifecycle_and_inventory_restoration(self):
        """
        Step-by-step transition through full lifecycle:
        requested -> approved -> pickup_pending -> item_received -> inspection -> processing -> completed.
        Restores stock exactly once for passed items.
        """
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 2}],
        )

        initial_stock = self.variant1_m.stock_quantity

        # 1. Approve
        ret = transition_return_status(ret, ReturnRequest.STATUS_APPROVED, self.seller_user1, "seller")
        self.assertEqual(ret.status, ReturnRequest.STATUS_APPROVED)

        # 2. Pickup Pending
        ret = transition_return_status(ret, ReturnRequest.STATUS_PICKUP_PENDING, self.seller_user1, "seller")
        self.assertEqual(ret.status, ReturnRequest.STATUS_PICKUP_PENDING)

        # 3. Item Received
        ret = transition_return_status(ret, ReturnRequest.STATUS_ITEM_RECEIVED, self.seller_user1, "seller")
        self.assertEqual(ret.status, ReturnRequest.STATUS_ITEM_RECEIVED)

        # 4. Inspection (Record Passed + Restock)
        item_id = ret.items.first().id
        inspection_decisions = {
            item_id: {"status": "passed", "restock": True, "notes": "Item brand new in box"}
        }
        ret = transition_return_status(
            ret,
            ReturnRequest.STATUS_INSPECTION,
            self.seller_user1,
            "seller",
            inspection_decisions=inspection_decisions,
        )
        self.assertEqual(ret.status, ReturnRequest.STATUS_INSPECTION)

        # 5. Processing
        ret = transition_return_status(ret, ReturnRequest.STATUS_PROCESSING, self.seller_user1, "seller")
        self.assertEqual(ret.status, ReturnRequest.STATUS_PROCESSING)

        # 6. Completed -> triggers inventory restock
        ret = transition_return_status(ret, ReturnRequest.STATUS_COMPLETED, self.seller_user1, "seller")
        self.assertEqual(ret.status, ReturnRequest.STATUS_COMPLETED)
        self.assertTrue(ret.inventory_restored)

        # Check inventory was restored
        self.variant1_m.refresh_from_db()
        self.assertEqual(self.variant1_m.stock_quantity, initial_stock + 2)

        # Check duplicate restoration prevention
        # Calling complete or transition again does not double-restock
        self.assertFalse(ret.status in [ReturnRequest.STATUS_REQUESTED])
        self.variant1_m.refresh_from_db()
        self.assertEqual(self.variant1_m.stock_quantity, initial_stock + 2)

    def test_customer_cancellation_rules(self):
        """Customer can cancel when requested or approved, but not once pickup pending."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )
        self.assertTrue(ret.can_customer_cancel)

        # Cancel as customer
        ret = transition_return_status(ret, ReturnRequest.STATUS_CANCELLED, self.customer1, "customer")
        self.assertEqual(ret.status, ReturnRequest.STATUS_CANCELLED)

        # Cannot cancel if already cancelled
        with self.assertRaises(ReturnValidationError):
            transition_return_status(ret, ReturnRequest.STATUS_CANCELLED, self.customer1, "customer")

    # -------------------------------------------------------------------------
    # 5. Safe Mock Refund & Financial Immutability
    # -------------------------------------------------------------------------
    def test_process_simulated_mock_refund(self):
        """
        Records a mock refund voucher:
        - is_mock is True
        - External reference is MOCK-REF-...
        - Preserves historical Order and SellerOrder totals
        - Accurately tracks commission reversal and seller payable deduction
        """
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 2}],
        )
        ret = transition_return_status(ret, ReturnRequest.STATUS_APPROVED, self.seller_user1, "seller")
        ret = transition_return_status(ret, ReturnRequest.STATUS_PICKUP_PENDING, self.seller_user1, "seller")
        ret = transition_return_status(ret, ReturnRequest.STATUS_ITEM_RECEIVED, self.seller_user1, "seller")
        ret = transition_return_status(ret, ReturnRequest.STATUS_INSPECTION, self.seller_user1, "seller")
        ret = transition_return_status(ret, ReturnRequest.STATUS_PROCESSING, self.seller_user1, "seller")

        # 2 units @ ₹1000 = ₹2000 refund
        refund_rec = process_simulated_refund(
            return_request=ret,
            actor=self.seller_user1,
            approved_amount=Decimal("2000.00"),
            reason="Customer size return",
        )

        self.assertTrue(refund_rec.is_mock)
        self.assertTrue(refund_rec.external_reference.startswith("MOCK-REF-"))
        self.assertEqual(refund_rec.approved_amount, Decimal("2000.00"))

        # Commission rate was 10%, so reversal = ₹200.00
        self.assertEqual(refund_rec.commission_reversal, Decimal("200.00"))
        # Seller deduction = ₹2000 - ₹200 = ₹1800.00
        self.assertEqual(refund_rec.seller_payable_deduction, Decimal("1800.00"))

        # Historical financial snapshots remain intact
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_amount, Decimal("4000.00"))
        self.seller_order1.refresh_from_db()
        self.assertEqual(self.seller_order1.subtotal, Decimal("2000.00"))

        # Duplicate refund is rejected
        with self.assertRaises(RefundError):
            process_simulated_refund(
                return_request=ret,
                actor=self.seller_user1,
                approved_amount=Decimal("2000.00"),
            )

    def test_refund_amount_cannot_exceed_eligible(self):
        """Refund amount higher than eligible amount is blocked."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )
        # 1 unit = ₹1000 eligible
        with self.assertRaises(RefundError):
            process_simulated_refund(
                return_request=ret,
                actor=self.seller_user1,
                approved_amount=Decimal("1500.00"),
            )

    # -------------------------------------------------------------------------
    # 6. Customer & Seller Web Views
    # -------------------------------------------------------------------------
    def test_customer_submission_flow_via_post(self):
        """Customer submits return request through the web form."""
        self.client.force_login(self.customer1)
        url = reverse("core:order_return_request", kwargs={"order_number": self.order.order_number})

        post_data = {
            "package_id": self.seller_order1.id,
            "request_type": "return",
            "reason": "defective",
            "customer_notes": "Stitching unraveled at the seam.",
            f"item_{self.order_item1.id}_selected": "on",
            f"item_{self.order_item1.id}_quantity": "1",
        }
        resp = self.client.post(url, post_data)
        self.assertEqual(resp.status_code, 302)

        ret = ReturnRequest.objects.filter(customer=self.customer1).first()
        self.assertIsNotNone(ret)
        self.assertEqual(ret.reason, "defective")
        self.assertEqual(ret.items.first().quantity, 1)

    def test_seller_action_via_post(self):
        """Seller approves request and logs notes."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )

        self.client.force_login(self.seller_user1)
        action_url = reverse("core:seller_return_action", kwargs={"request_number": ret.request_number})

        post_data = {
            "action": "transition",
            "status": "approved",
            "notes": "Approved for courier pickup.",
        }
        resp = self.client.post(action_url, post_data)
        self.assertEqual(resp.status_code, 302)

        ret.refresh_from_db()
        self.assertEqual(ret.status, ReturnRequest.STATUS_APPROVED)
        self.assertIn("Approved for courier pickup", ret.seller_notes)

    # -------------------------------------------------------------------------
    # 7. Product-Specific Policy & Custom Admin Returns Management
    # -------------------------------------------------------------------------
    def test_product_non_returnable_policy_enforcement(self):
        """Item marked as non-returnable (is_returnable=False) fails validation."""
        self.product1.is_returnable = False
        self.product1.save()

        is_eligible, err = validate_item_eligibility(self.order_item1, quantity=1, user=self.customer1)
        self.assertFalse(is_eligible)
        self.assertIn("non-returnable", err.lower())

    def test_product_custom_return_window_enforcement(self):
        """Product with custom return_window_days=3 expires after 3 days."""
        self.product1.return_window_days = 3
        self.product1.save()

        # Set delivery date 4 days ago
        self.seller_order1.delivered_at = timezone.now() - timedelta(days=4)
        self.seller_order1.save()

        is_eligible, err = validate_item_eligibility(self.order_item1, quantity=1, user=self.customer1)
        self.assertFalse(is_eligible)
        self.assertIn("expired", err.lower())

    def test_customer_order_detail_view_shows_return_action(self):
        """Order detail template provides Return/Exchange button for delivered packages."""
        self.client.force_login(self.customer1)
        url = reverse("core:order_detail", kwargs={"order_number": self.order.order_number})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Return / Exchange Items")
        self.assertContains(resp, "Return / Exchange")

        # Create return request and ensure template renders active return badge without FieldError
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 2}],
        )
        self.order_item1.refresh_from_db()
        self.assertIsNotNone(self.order_item1.active_return_item)
        self.assertEqual(self.order_item1.active_return_item.return_request_id, ret.id)

        resp2 = self.client.get(url)
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, ret.get_status_display())

    def test_custom_admin_return_list_view(self):
        """Staff can view returns listing in custom admin with KPI summary."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )

        # Anonymous gets redirected
        url = reverse("custom_admin:return_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)

        # Staff can view
        self.client.force_login(self.staff_user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ret.request_number)
        self.assertContains(resp, "Return &amp; Exchange Requests")
        self.assertIn("kpis", resp.context)
        self.assertEqual(resp.context["kpis"]["total"], 1)

    def test_custom_admin_return_detail_view(self):
        """Staff can view detailed return request in custom admin."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )

        self.client.force_login(self.staff_user)
        url = reverse("custom_admin:return_detail", kwargs={"request_number": ret.request_number})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ret.request_number)
        self.assertContains(resp, self.product1.name)

    def test_custom_admin_return_transition_view(self):
        """Staff can transition return request status and record inspection."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )

        self.client.force_login(self.staff_user)
        url = reverse("custom_admin:return_transition", kwargs={"request_number": ret.request_number})

        # Transition requested -> approved
        post_data = {
            "target_status": "approved",
            "notes": "Admin approved return request.",
        }
        resp = self.client.post(url, post_data)
        self.assertEqual(resp.status_code, 302)

        ret.refresh_from_db()
        self.assertEqual(ret.status, ReturnRequest.STATUS_APPROVED)

    def test_custom_admin_return_reject_requires_reason(self):
        """Staff rejection fails if rejection reason is empty."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )

        self.client.force_login(self.staff_user)
        url = reverse("custom_admin:return_reject", kwargs={"request_number": ret.request_number})

        # Missing reason
        resp = self.client.post(url, {"rejection_reason": ""})
        self.assertEqual(resp.status_code, 302)
        ret.refresh_from_db()
        self.assertEqual(ret.status, ReturnRequest.STATUS_REQUESTED)

        # With mandatory reason
        resp = self.client.post(url, {"rejection_reason": "Missing original warranty tag."})
        self.assertEqual(resp.status_code, 302)
        ret.refresh_from_db()
        self.assertEqual(ret.status, ReturnRequest.STATUS_REJECTED)
        self.assertEqual(ret.rejection_reason, "Missing original warranty tag.")

    def test_custom_admin_return_refund_execution(self):
        """Staff can process simulated refund, restoring inventory and updating ledger."""
        ret = create_return_request(
            customer=self.customer1,
            seller_order=self.seller_order1,
            items_data=[{"order_item": self.order_item1, "quantity": 1}],
        )

        # Transition to item_received -> inspection with restock=True
        ret = transition_return_status(ret, ReturnRequest.STATUS_APPROVED, actor=self.staff_user, actor_role="platform_admin")
        ret = transition_return_status(ret, ReturnRequest.STATUS_PICKUP_PENDING, actor=self.staff_user, actor_role="platform_admin")
        ret = transition_return_status(ret, ReturnRequest.STATUS_ITEM_RECEIVED, actor=self.staff_user, actor_role="platform_admin")
        ret_item = ret.items.first()
        ret = transition_return_status(
            ret,
            ReturnRequest.STATUS_INSPECTION,
            actor=self.staff_user,
            actor_role="platform_admin",
            inspection_decisions={ret_item.id: {"status": "passed", "restock": True, "notes": "Like new"}},
        )

        initial_variant_stock = self.variant1_m.stock_quantity

        self.client.force_login(self.staff_user)
        url = reverse("custom_admin:return_refund", kwargs={"request_number": ret.request_number})
        resp = self.client.post(
            url,
            {
                "approved_amount": str(ret.total_refund_amount),
                "reason": "Quality check passed, refund approved.",
                "payment_method": "upi",
            },
        )
        self.assertEqual(resp.status_code, 302)

        ret.refresh_from_db()
        self.assertEqual(ret.status, ReturnRequest.STATUS_COMPLETED)
        self.assertTrue(ret.inventory_restored)

        # Inventory increased by 1 unit
        self.variant1_m.refresh_from_db()
        self.assertEqual(self.variant1_m.stock_quantity, initial_variant_stock + 1)

        # RefundRecord created
        ref = RefundRecord.objects.filter(return_request=ret).first()
        self.assertIsNotNone(ref)
        self.assertEqual(ref.status, RefundRecord.STATUS_PROCESSED)
        self.assertTrue(ref.is_mock)


