"""
Tests for Phase 5 — Shipping Financial Allocation & Model Enhancements.

Validates:
1. Pro-rata allocation across two seller packages by merchandise subtotal.
2. Pro-rata allocation across seller and platform packages.
3. Exact ₹49 allocation with rounding remainder absorbed deterministically by the final package.
4. Free shipping allocation of ₹0.
5. Single-package allocation.
6. Checkout allocation totals equal the parent shipping fee.
7. API checkout uses the same allocation service.
8. Commission snapshots remain unchanged (computed strictly on merchandise line-items).
9. Unknown courier cost is not deducted from seller payable.
10. Shiprocket fields accept empty values and do not fabricate tracking data.
11. Historical orders remain readable.
12. Pickup location details (seller facility vs platform warehouse).
"""

from decimal import Decimal
import uuid
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
from core.serializers import (
    CustomerSellerOrderSerializer,
    OrderDetailSerializer,
    SellerOrderSerializer,
)
from core.services import (
    DEFAULT_STANDARD_SHIPPING_FEE,
    calculate_shipping_allocations,
    create_order_from_cart,
)

User = get_user_model()


class ShippingAllocationUnitTests(TestCase):
    """Unit tests for the calculate_shipping_allocations helper function."""

    def test_01_pro_rata_allocation_two_packages(self):
        """
        Prompt Example:
        - Parent shipping fee: ₹49.00
        - Package A subtotal: ₹300.00
        - Package B subtotal: ₹200.00
        - Total subtotal: ₹500.00
        Expected:
        - Package A: ₹29.40 (300/500 * 49)
        - Package B: ₹19.60 (200/500 * 49)
        """
        subtotals = [Decimal("300.00"), Decimal("200.00")]
        allocations = calculate_shipping_allocations(
            package_subtotals=subtotals,
            parent_shipping_fee=Decimal("49.00"),
        )
        self.assertEqual(len(allocations), 2)
        self.assertEqual(allocations[0], Decimal("29.40"))
        self.assertEqual(allocations[1], Decimal("19.60"))
        self.assertEqual(sum(allocations), Decimal("49.00"))

    def test_02_pro_rata_allocation_with_platform_package(self):
        """Pro-rata allocation across a seller package and a platform package."""
        subtotals = [Decimal("150.00"), Decimal("150.00")]
        allocations = calculate_shipping_allocations(
            package_subtotals=subtotals,
            parent_shipping_fee=Decimal("49.00"),
        )
        self.assertEqual(len(allocations), 2)
        self.assertEqual(allocations[0], Decimal("24.50"))
        self.assertEqual(allocations[1], Decimal("24.50"))
        self.assertEqual(sum(allocations), Decimal("49.00"))

    def test_03_exact_49_allocation_with_rounding_remainder(self):
        """
        Three equal packages of ₹100 each with ₹49 shipping fee:
        - 100/300 * 49 = 16.333... -> 16.33
        - 100/300 * 49 = 16.333... -> 16.33
        - Final package deterministically takes 49.00 - (16.33 + 16.33) = 16.34
        - Total sum is exactly ₹49.00.
        """
        subtotals = [Decimal("100.00"), Decimal("100.00"), Decimal("100.00")]
        allocations = calculate_shipping_allocations(
            package_subtotals=subtotals,
            parent_shipping_fee=Decimal("49.00"),
        )
        self.assertEqual(len(allocations), 3)
        self.assertEqual(allocations[0], Decimal("16.33"))
        self.assertEqual(allocations[1], Decimal("16.33"))
        self.assertEqual(allocations[2], Decimal("16.34"))
        self.assertEqual(sum(allocations), Decimal("49.00"))

    def test_04_free_shipping_allocation_zero(self):
        """When parent shipping fee is ₹0 (free shipping), all packages receive ₹0.00."""
        subtotals = [Decimal("600.00"), Decimal("400.00")]
        allocations = calculate_shipping_allocations(
            package_subtotals=subtotals,
            parent_shipping_fee=Decimal("0.00"),
        )
        self.assertEqual(allocations, [Decimal("0.00"), Decimal("0.00")])
        self.assertEqual(sum(allocations), Decimal("0.00"))

    def test_05_single_package_allocation(self):
        """When there is only one package, it receives 100% of the parent shipping fee."""
        subtotals = [Decimal("300.00")]
        allocations = calculate_shipping_allocations(
            package_subtotals=subtotals,
            parent_shipping_fee=Decimal("49.00"),
        )
        self.assertEqual(allocations, [Decimal("49.00")])
        self.assertEqual(sum(allocations), Decimal("49.00"))

    def test_06_empty_packages_or_zero_subtotal_edge_cases(self):
        """Handles empty package lists and zero merchandise subtotals safely."""
        self.assertEqual(calculate_shipping_allocations([], Decimal("49.00")), [])

        # Zero subtotals evenly divided with remainder
        subtotals = [Decimal("0.00"), Decimal("0.00")]
        allocations = calculate_shipping_allocations(subtotals, Decimal("49.00"))
        self.assertEqual(allocations[0], Decimal("24.50"))
        self.assertEqual(allocations[1], Decimal("24.50"))
        self.assertEqual(sum(allocations), Decimal("49.00"))


class ShippingAllocationIntegrationTests(TestCase):
    """Integration tests for checkout shipping fee allocation and accounting model."""

    def setUp(self):
        self.category = Category.objects.create(name="Apparel", slug="apparel")

        # Customer
        self.customer = User.objects.create_user(
            username="customer_ship@example.com",
            email="customer_ship@example.com",
            password="Password123!",
            first_name="Pooja",
            last_name="Sharma",
        )
        self.address = Address.objects.create(
            user=self.customer,
            full_name="Pooja Sharma",
            phone_number="9876543210",
            address_line1="45 Richmond Circle",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560025",
            address_type="home",
            is_default=True,
        )

        # Seller 1 (10% commission)
        self.seller1_user = User.objects.create_user(
            username="seller1_ship@example.com",
            email="seller1_ship@example.com",
            password="Password123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller1_user,
            store_name="Vogue Studio",
            legal_business_name="Vogue Studio Pvt Ltd",
            support_email="support@vogue.in",
            support_phone="9876500001",
            registered_address="10 Fashion Street",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Warehouse A, Peenya",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560058",
            gstin="29ABCDE1234F1Z1",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Seller 2 (15% commission)
        self.seller2_user = User.objects.create_user(
            username="seller2_ship@example.com",
            email="seller2_ship@example.com",
            password="Password123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller2_user,
            store_name="Urban Stitch",
            legal_business_name="Urban Stitch LLP",
            support_email="support@urbanstitch.in",
            support_phone="9876500002",
            registered_address="20 Textile Hub",
            city="Surat",
            state="Gujarat",
            postal_code="395002",
            pickup_address="Godown 4, Ring Road",
            pickup_city="Surat",
            pickup_state="Gujarat",
            pickup_postal_code="395002",
            gstin="24ABCDE5678F1Z2",
            pan_number="ABCDE5678F",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Products priced below free shipping threshold (₹499)
        # Seller 1: ₹180.00
        self.prod_seller1 = Product.objects.create(
            name="Cotton Socks Pack",
            slug="cotton-socks-pack",
            sku="VOG-SCK-001",
            category=self.category,
            seller=self.seller1,
            price=Decimal("180.00"),
            compare_at_price=Decimal("299.00"),
            stock_quantity=50,
            is_active=True,
            approval_status=Product.APPROVAL_APPROVED,
            is_approved=True,
        )

        # Seller 2: ₹120.00
        self.prod_seller2 = Product.objects.create(
            name="Wristband",
            slug="wristband",
            sku="URB-WRB-001",
            category=self.category,
            seller=self.seller2,
            price=Decimal("120.00"),
            compare_at_price=Decimal("199.00"),
            stock_quantity=50,
            is_active=True,
            approval_status=Product.APPROVAL_APPROVED,
            is_approved=True,
        )

        # Platform Product: ₹100.00
        self.prod_platform = Product.objects.create(
            name="Fitmeezo Direct Water Bottle",
            slug="direct-water-bottle",
            sku="PLT-BTL-001",
            category=self.category,
            seller=None,
            price=Decimal("100.00"),
            compare_at_price=Decimal("150.00"),
            stock_quantity=50,
            is_active=True,
            approval_status=Product.APPROVAL_APPROVED,
            is_approved=True,
        )

        # Clients
        self.client = Client()
        self.client.login(username="customer_ship@example.com", password="Password123!")

        self.api_client = APIClient()
        self.token, _ = Token.objects.get_or_create(user=self.customer)
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def test_07_checkout_multiseller_shipping_allocation(self):
        """
        Cart with Seller 1 (₹180) and Seller 2 (₹120):
        - Subtotal: ₹300 (< ₹499 free shipping threshold)
        - Shipping fee: ₹49.00
        - Package 1: 180/300 * 49 = ₹29.40
        - Package 2: 120/300 * 49 = ₹19.60
        - Sum of allocations: ₹49.00
        - shipping_margin: ₹29.40 and ₹19.60 (since actual shipping cost is ₹0.00)
        """
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller2.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.address.pk,
            "selected_address": self.address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-ship-alloc-001",
        }
        response = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(idempotency_key="token-ship-alloc-001")
        self.assertEqual(order.subtotal, Decimal("300.00"))
        self.assertEqual(order.shipping_fee, Decimal("49.00"))
        self.assertEqual(order.total_amount, Decimal("349.00"))

        # Verify child packages
        pkg1 = order.seller_orders.get(seller=self.seller1)
        pkg2 = order.seller_orders.get(seller=self.seller2)

        self.assertEqual(pkg1.subtotal, Decimal("180.00"))
        self.assertEqual(pkg1.customer_shipping_charge, Decimal("29.40"))
        self.assertEqual(pkg1.shipping_margin, Decimal("29.40"))
        self.assertEqual(pkg1.actual_shipping_cost, Decimal("0.00"))
        self.assertEqual(pkg1.shipping_allocated_to_seller, Decimal("0.00"))

        self.assertEqual(pkg2.subtotal, Decimal("120.00"))
        self.assertEqual(pkg2.customer_shipping_charge, Decimal("19.60"))
        self.assertEqual(pkg2.shipping_margin, Decimal("19.60"))
        self.assertEqual(pkg2.actual_shipping_cost, Decimal("0.00"))
        self.assertEqual(pkg2.shipping_allocated_to_seller, Decimal("0.00"))

        # Rule: Exact sum equals parent shipping fee
        self.assertEqual(
            pkg1.customer_shipping_charge + pkg2.customer_shipping_charge,
            order.shipping_fee,
        )

    def test_08_checkout_seller_and_platform_shipping_allocation(self):
        """
        Cart with Seller 1 (₹180) and Platform (₹100):
        - Subtotal: ₹280 (< ₹499)
        - Shipping fee: ₹49.00
        - Seller 1: 180/280 * 49 = 31.50
        - Platform: 49.00 - 31.50 = 17.50
        - Sum: ₹49.00
        """
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_platform.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.address.pk,
            "selected_address": self.address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-ship-alloc-plat",
        }
        response = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(idempotency_key="token-ship-alloc-plat")
        self.assertEqual(order.shipping_fee, Decimal("49.00"))

        seller_pkg = order.seller_orders.get(seller=self.seller1)
        plat_pkg = order.seller_orders.get(is_platform_fulfilled=True)

        self.assertEqual(seller_pkg.customer_shipping_charge, Decimal("31.50"))
        self.assertEqual(plat_pkg.customer_shipping_charge, Decimal("17.50"))
        self.assertEqual(
            seller_pkg.customer_shipping_charge + plat_pkg.customer_shipping_charge,
            order.shipping_fee,
        )

    def test_09_api_checkout_uses_same_shipping_allocation_service(self):
        """API checkout calls create_order_from_cart and allocates shipping pro-rata."""
        # Add items via API cart
        self.api_client.post(
            reverse("api:cart_add"),
            {"product_id": self.prod_seller1.pk, "quantity": 1},
            format="json",
        )
        self.api_client.post(
            reverse("api:cart_add"),
            {"product_id": self.prod_seller2.pk, "quantity": 1},
            format="json",
        )

        payload = {
            "address_id": self.address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-api-ship-alloc",
        }
        response = self.api_client.post(
            reverse("api:checkout_place_order"),
            payload,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = Order.objects.get(idempotency_key="token-api-ship-alloc")
        self.assertEqual(order.shipping_fee, Decimal("49.00"))

        pkgs = order.seller_orders.all()
        self.assertEqual(pkgs.count(), 2)
        total_allocated = sum(p.customer_shipping_charge for p in pkgs)
        self.assertEqual(total_allocated, order.shipping_fee)

    def test_10_commission_snapshots_remain_unaffected_by_shipping(self):
        """
        Marketplace commission snapshots on OrderItem and SellerOrder must be
        computed strictly on merchandise line totals and NEVER on customer shipping fees.
        """
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.address.pk,
            "selected_address": self.address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-comm-pure",
        }
        response = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(idempotency_key="token-comm-pure")
        pkg = order.seller_orders.get(seller=self.seller1)

        # Merchandise: ₹180, Commission 10% = ₹18.00
        # Shipping fee: ₹49.00
        self.assertEqual(pkg.subtotal, Decimal("180.00"))
        self.assertEqual(pkg.customer_shipping_charge, Decimal("49.00"))
        self.assertEqual(pkg.total_commission, Decimal("18.00"))  # Exactly 10% of 180, NOT 10% of (180+49)
        self.assertEqual(pkg.net_seller_payable, Decimal("162.00"))  # 180 - 18

        item = order.items.first()
        self.assertEqual(item.commission_amount, Decimal("18.00"))

    def test_11_unknown_courier_cost_not_deducted_from_seller_payable(self):
        """
        Safety Rule: Unknown courier costs must NOT be deducted.
        At checkout, actual_shipping_cost is ₹0.00 and shipping_allocated_to_seller is ₹0.00.
        net_seller_payable and calculate_estimated_seller_payable() both reflect gross payable.
        Only when courier cost is reconciled and allocated to seller does the accounting formula deduct it.
        """
        self.client.post(reverse("core:cart_add"), {"product_id": self.prod_seller1.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.address.pk,
            "selected_address": self.address.pk,
            "payment_method": "cod",
            "idempotency_token": "token-payable-safe",
        }
        response = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(idempotency_key="token-payable-safe")
        pkg = order.seller_orders.get(seller=self.seller1)

        # Subtotal: 180, Commission: 18 -> Base payable = 162.00
        self.assertEqual(pkg.net_seller_payable, Decimal("162.00"))
        self.assertEqual(pkg.actual_shipping_cost, Decimal("0.00"))
        self.assertEqual(pkg.shipping_allocated_to_seller, Decimal("0.00"))
        self.assertEqual(pkg.calculate_estimated_seller_payable(), Decimal("162.00"))

        # Reconcile courier expense: actual cost ₹40.00 allocated to seller
        pkg.actual_shipping_cost = Decimal("40.00")
        pkg.shipping_allocated_to_seller = Decimal("40.00")
        pkg.shipping_margin = pkg.customer_shipping_charge - pkg.actual_shipping_cost  # 49.00 - 40.00 = 9.00
        pkg.save()

        # Estimated payable with reconciled logistics deduction: 162.00 - 40.00 = 122.00
        self.assertEqual(pkg.calculate_estimated_seller_payable(), Decimal("122.00"))
        self.assertEqual(pkg.shipping_margin, Decimal("9.00"))

    def test_12_shiprocket_fields_accept_empty_values_and_clean(self):
        """
        Shiprocket integration fields must accept empty strings by default
        and pass full_clean() without requiring real API calls or fabricated data.
        """
        order = Order.objects.create(
            order_number=f"FM-{uuid.uuid4().hex[:6].upper()}",
            user=self.customer,
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 Richmond Circle",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560025",
            shipping_address_type="home",
            subtotal=Decimal("180.00"),
            shipping_fee=Decimal("49.00"),
            total_amount=Decimal("229.00"),
        )
        so = SellerOrder(
            order=order,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number=f"{order.order_number}-S{self.seller1.id}",
            subtotal=Decimal("180.00"),
            total_commission=Decimal("18.00"),
            net_seller_payable=Decimal("162.00"),
            customer_shipping_charge=Decimal("49.00"),
        )
        # Verify clean() succeeds with empty Shiprocket defaults
        so.full_clean()
        so.save()

        self.assertEqual(so.shiprocket_order_id, "")
        self.assertEqual(so.shiprocket_shipment_id, "")
        self.assertEqual(so.shiprocket_awb_code, "")
        self.assertEqual(so.tracking_url, "")
        self.assertEqual(so.awb_code, "")

        # Test mark_shipped with tracking and Shiprocket identifiers
        so.mark_shipped(
            courier_name="Delhivery",
            tracking_number="DEL-999001",
            tracking_url="https://track.shiprocket.co/DEL-999001",
            shiprocket_awb_code="DEL-999001",
            shiprocket_order_id="SR-100234",
            shiprocket_shipment_id="SR-SHP-5001",
        )
        so.refresh_from_db()
        self.assertEqual(so.status, SellerOrder.STATUS_SHIPPED)
        self.assertEqual(so.courier_name, "Delhivery")
        self.assertEqual(so.tracking_number, "DEL-999001")
        self.assertEqual(so.tracking_url, "https://track.shiprocket.co/DEL-999001")
        self.assertEqual(so.shiprocket_order_id, "SR-100234")
        self.assertEqual(so.shiprocket_shipment_id, "SR-SHP-5001")
        self.assertEqual(so.shiprocket_awb_code, "DEL-999001")
        self.assertEqual(so.awb_code, "DEL-999001")

    def test_13_pickup_location_details(self):
        """
        Approved Business Rule 3:
        - Third-party seller package returns seller's registered warehouse location.
        - Platform-owned inventory returns the platform's configured warehouse.
        """
        order = Order.objects.create(
            order_number=f"FM-{uuid.uuid4().hex[:6].upper()}",
            user=self.customer,
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 Richmond Circle",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560025",
            shipping_address_type="home",
            subtotal=Decimal("200.00"),
            shipping_fee=Decimal("49.00"),
            total_amount=Decimal("249.00"),
        )
        seller_pkg = SellerOrder.objects.create(
            order=order,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number=f"{order.order_number}-S{self.seller1.id}",
            subtotal=Decimal("100.00"),
        )
        plat_pkg = SellerOrder.objects.create(
            order=order,
            seller=None,
            is_platform_fulfilled=True,
            sub_order_number=f"{order.order_number}-PLT",
            subtotal=Decimal("100.00"),
        )

        seller_loc = seller_pkg.get_pickup_location_details()
        self.assertEqual(seller_loc["source"], "seller")
        self.assertEqual(seller_loc["name"], "Vogue Studio")
        self.assertEqual(seller_loc["address"], "Warehouse A, Peenya")
        self.assertEqual(seller_loc["city"], "Bengaluru")
        self.assertEqual(seller_loc["state"], "Karnataka")
        self.assertEqual(seller_loc["postal_code"], "560058")

        plat_loc = plat_pkg.get_pickup_location_details()
        self.assertEqual(plat_loc["source"], "platform")
        self.assertIn("Fitmeezo", plat_loc["name"])
        self.assertTrue(bool(plat_loc["address"]))

    def test_14_refundable_shipping_and_cancellation_safety(self):
        """
        Approved Business Rules 4 & 5:
        - Each package snapshots its allocated customer shipping charge.
        - refundable_shipping_amount provides the maximum refundable customer shipping charge.
        - Cancelled packages have eligible payable of ₹0.00.
        """
        order = Order.objects.create(
            order_number=f"FM-{uuid.uuid4().hex[:6].upper()}",
            user=self.customer,
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 Richmond Circle",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560025",
            shipping_address_type="home",
            subtotal=Decimal("180.00"),
            shipping_fee=Decimal("49.00"),
            total_amount=Decimal("229.00"),
        )
        pkg = SellerOrder.objects.create(
            order=order,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number=f"{order.order_number}-S{self.seller1.id}",
            subtotal=Decimal("180.00"),
            total_commission=Decimal("18.00"),
            net_seller_payable=Decimal("162.00"),
            customer_shipping_charge=Decimal("49.00"),
        )
        self.assertEqual(pkg.refundable_shipping_amount, Decimal("49.00"))

        # Cancel package
        pkg.cancel_package()
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, SellerOrder.STATUS_CANCELLED)
        self.assertIsNotNone(pkg.cancelled_at)
        self.assertEqual(pkg.calculate_estimated_seller_payable(), Decimal("0.00"))

    def test_15_historical_orders_readable_and_serialized(self):
        """
        Historical orders without Shiprocket fields remain readable in models and serializers.
        """
        order = Order.objects.create(
            order_number="FM-HIST-20261011-001",
            user=self.customer,
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 Richmond Circle",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560025",
            shipping_address_type="home",
            subtotal=Decimal("500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("500.00"),
        )
        pkg = SellerOrder.objects.create(
            order=order,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number=f"{order.order_number}-S{self.seller1.id}",
            subtotal=Decimal("500.00"),
            total_commission=Decimal("50.00"),
            net_seller_payable=Decimal("450.00"),
        )

        # Ensure serializers work cleanly
        seller_data = SellerOrderSerializer(pkg).data
        self.assertEqual(seller_data["sub_order_number"], pkg.sub_order_number)
        self.assertEqual(seller_data["shiprocket_order_id"], "")
        self.assertEqual(seller_data["tracking_url"], "")

        customer_data = CustomerSellerOrderSerializer(pkg).data
        self.assertEqual(customer_data["sub_order_number"], pkg.sub_order_number)
        self.assertNotIn("net_seller_payable", customer_data)
        self.assertNotIn("shiprocket_order_id", customer_data)
        self.assertEqual(customer_data["tracking_url"], "")
