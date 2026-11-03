"""
Tests for Fitmeezo Amazon/Flipkart-Style Packing Slip and Shipping Label Workflow.

Validates:
1. Admin and Seller Order Detail "Print Packing Slip" button presence.
2. Packing Slip content: Order ID, Order Date, Customer Name, Complete Shipping Address,
   Phone, Product Name, SKU, Size/Variant, Quantity, and Seller Details.
3. Print-friendly A4 and thermal (4x6) formats (HTML and ReportLab PDF).
4. Seller marking package as Packed / Ready for Pickup.
5. "Ship with Shiprocket" action availability following ready_to_ship status.
6. AWB, Courier Name, and Tracking URL saved upon booking.
7. "Print Shipping Label" button visibility strictly gated on booking completion,
   with transparent MOCK status indicators in SHIPROCKET_MOCK_MODE=True.
8. Tracking Status and Pickup Status visible in Seller and Admin dashboards.
9. Customer tracking availability.
10. Strict separation between Order, Payment, and Fulfillment statuses.
11. Multi-seller package isolation (cross-seller access rejected).
12. Permissions and role-based access control.
"""

from decimal import Decimal
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
    SellerOrder,
    ShipmentBooking,
)
from core.packing_slip import (
    generate_amazon_style_packing_slip_pdf,
    generate_courier_shipping_label_pdf,
)

User = get_user_model()


class PackingSlipAndShippingLabelWorkflowTests(TestCase):
    """Integration and unit tests for the multi-seller packing slip and shipping label workflow."""

    def setUp(self):
        self.client = Client()

        # 1. Admin / Staff User
        self.admin_user = User.objects.create_superuser(
            username="admin_user",
            email="admin@fitmeezo.com",
            password="AdminPassword123!",
        )

        # 2. Customer User
        self.customer_user = User.objects.create_user(
            username="customer_user",
            email="customer@example.com",
            password="CustomerPassword123!",
            first_name="Pooja",
            last_name="Sharma",
        )

        # 3. Seller A
        self.seller_a_user = User.objects.create_user(
            username="seller_a",
            email="seller_a@example.com",
            password="SellerPassword123!",
            first_name="Vikram",
            last_name="Singh",
        )
        self.seller_a = Seller.objects.create(
            user=self.seller_a_user,
            store_name="Royal Heritage Textiles",
            legal_business_name="Royal Heritage Apparels Pvt Ltd",
            gstin="27ABCDE1234F1Z5",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            registered_mobile="9876543210",
            pickup_contact_name="Vikram Singh",
            pickup_contact_phone="9876543210",
            support_phone="9876543210",
            pickup_address="Plot 102, Phase 3, GIDC Industrial Estate",
            pickup_city="Mumbai",
            pickup_state="Maharashtra",
            pickup_postal_code="400001",
            pickup_location_verified=True,
            pickup_location_nickname="Royal_Mumbai_WH",
        )

        # 4. Seller B
        self.seller_b_user = User.objects.create_user(
            username="seller_b",
            email="seller_b@example.com",
            password="SellerPassword123!",
            first_name="Ananya",
            last_name="Deshmukh",
        )
        self.seller_b = Seller.objects.create(
            user=self.seller_b_user,
            store_name="Deshmukh Handlooms",
            legal_business_name="Deshmukh Creations LLP",
            gstin="29XYZAB5678C1Z2",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            registered_mobile="9123456789",
            pickup_contact_name="Ananya Deshmukh",
            pickup_contact_phone="9123456789",
            support_phone="9123456789",
            pickup_address="45 Handloom Complex, Cotton Market",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            pickup_location_verified=True,
            pickup_location_nickname="Deshmukh_BLR_WH",
        )

        # Category and Products
        self.category = Category.objects.create(name="Apparel", slug="apparel")
        self.product_a = Product.objects.create(
            category=self.category,
            seller=self.seller_a,
            name="Banarasi Silk Saree",
            slug="banarasi-silk-saree",
            sku="BAN-SAR-BASE-01",
            price=Decimal("2999.00"),
            stock_quantity=10,
            weight_grams=Decimal("850.00"),
            length_cm=Decimal("30.00"),
            breadth_cm=Decimal("20.00"),
            height_cm=Decimal("5.00"),
        )
        self.variant_a = ProductVariant.objects.create(
            product=self.product_a,
            sku="BAN-SAR-RED-001",
            option_type="Size",
            option_value="Free Size",
            price=Decimal("2999.00"),
            stock_quantity=10,
        )

        self.product_b = Product.objects.create(
            category=self.category,
            seller=self.seller_b,
            name="Cotton Kurta Set",
            slug="cotton-kurta-set",
            sku="COT-KUR-BASE-01",
            price=Decimal("1499.00"),
            stock_quantity=15,
            weight_grams=Decimal("450.00"),
            length_cm=Decimal("25.00"),
            breadth_cm=Decimal("18.00"),
            height_cm=Decimal("4.00"),
        )
        self.variant_b = ProductVariant.objects.create(
            product=self.product_b,
            sku="COT-KUR-BLU-M",
            option_type="Size",
            option_value="M",
            price=Decimal("1499.00"),
            stock_quantity=15,
        )

        # 5. Multi-Seller Customer Order
        self.order = Order.objects.create(
            user=self.customer_user,
            order_number="FM-20261031-9876",
            status=Order.STATUS_PENDING,
            payment_status=Order.PAYMENT_STATUS_PAID,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            subtotal=Decimal("4498.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("4498.00"),
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="Flat 402, Sunshine Enclave, Sector 14",
            shipping_address_line2="Opposite Central Park",
            shipping_landmark="Near Metro Station",
            shipping_city="Gurugram",
            shipping_state="Haryana",
            shipping_postal_code="122001",
        )

        # Package A (Seller A)
        self.package_a = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller_a,
            sub_order_number="FM-20261031-9876-S1",
            status=SellerOrder.STATUS_PENDING,
            subtotal=Decimal("2999.00"),
            total_commission=Decimal("299.90"),
            net_seller_payable=Decimal("2699.10"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
            actual_weight_grams=Decimal("850.00"),
            actual_length_cm=Decimal("30.00"),
            actual_breadth_cm=Decimal("20.00"),
            actual_height_cm=Decimal("5.00"),
        )
        self.item_a = OrderItem.objects.create(
            order=self.order,
            seller_order=self.package_a,
            seller=self.seller_a,
            product=self.product_a,
            variant=self.variant_a,
            product_name="Banarasi Silk Saree",
            variant_label="Crimson Red / Free Size",
            sku="BAN-SAR-RED-001",
            unit_price=Decimal("2999.00"),
            quantity=1,
            line_total=Decimal("2999.00"),
        )

        # Package B (Seller B)
        self.package_b = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller_b,
            sub_order_number="FM-20261031-9876-S2",
            status=SellerOrder.STATUS_PENDING,
            subtotal=Decimal("1499.00"),
            total_commission=Decimal("149.90"),
            net_seller_payable=Decimal("1349.10"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
            actual_weight_grams=Decimal("450.00"),
            actual_length_cm=Decimal("25.00"),
            actual_breadth_cm=Decimal("18.00"),
            actual_height_cm=Decimal("4.00"),
        )
        self.item_b = OrderItem.objects.create(
            order=self.order,
            seller_order=self.package_b,
            seller=self.seller_b,
            product=self.product_b,
            variant=self.variant_b,
            product_name="Cotton Kurta Set",
            variant_label="Indigo Blue / M",
            sku="COT-KUR-BLU-M",
            unit_price=Decimal("1499.00"),
            quantity=1,
            line_total=Decimal("1499.00"),
        )

    def test_01_admin_and_seller_order_detail_print_packing_slip_button(self):
        """Requirement 1: Admin and Seller Order Detail must feature 'Print Packing Slip' buttons."""
        # 1. Seller Order Detail
        self.client.force_login(self.seller_a_user)
        resp_seller = self.client.get(reverse("core:seller_order_detail", kwargs={"pk": self.package_a.pk}))
        self.assertEqual(resp_seller.status_code, 200)
        self.assertContains(resp_seller, "Print Packing Slip")
        slip_url = reverse("core:seller_order_packing_slip", kwargs={"pk": self.package_a.pk})
        self.assertContains(resp_seller, slip_url)

        # 2. Admin Order Detail
        self.client.force_login(self.admin_user)
        resp_admin = self.client.get(reverse("custom_admin:order_detail", kwargs={"pk": self.order.pk}))
        self.assertEqual(resp_admin.status_code, 200)
        self.assertContains(resp_admin, "Print Packing Slip")
        admin_slip_url = reverse("custom_admin:order_package_packing_slip", kwargs={"order_id": self.order.pk, "pk": self.package_a.pk})
        self.assertContains(resp_admin, admin_slip_url)

    def test_02_packing_slip_content_unmasked_amazon_flipkart_fields(self):
        """Requirement 2: Packing Slip must show Order ID, Date, Customer Name, Complete Address, Phone, SKU, Variant, Qty, and Seller Details."""
        self.client.force_login(self.seller_a_user)
        url = reverse("core:seller_order_packing_slip", kwargs={"pk": self.package_a.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        # 1. Order ID & Date
        self.assertIn("FM-20261031-9876", content)
        self.assertIn("FM-20261031-9876-S1", content)

        # 2. Customer Name, Complete Unmasked Address & Phone
        self.assertIn("Pooja Sharma", content)
        self.assertIn("Flat 402, Sunshine Enclave, Sector 14", content)
        self.assertIn("Opposite Central Park", content)
        self.assertIn("Near Metro Station", content)
        self.assertIn("Gurugram", content)
        self.assertIn("Haryana", content)
        self.assertIn("122001", content)
        self.assertIn("9876543210", content)

        # 3. Product Name, SKU, Variant, Quantity
        self.assertIn("Banarasi Silk Saree", content)
        self.assertIn("BAN-SAR-RED-001", content)
        self.assertIn("Crimson Red / Free Size", content)

        # 4. Seller Details
        self.assertIn("Royal Heritage Textiles", content)
        self.assertIn("Royal Heritage Apparels Pvt Ltd", content)
        self.assertIn("Plot 102, Phase 3, GIDC Industrial Estate", content)
        self.assertIn("Mumbai", content)
        self.assertIn("27ABCDE1234F1Z5", content)

    def test_03_packing_slip_print_friendly_a4_and_thermal(self):
        """Requirement 3: Packing Slip is print-friendly for both A4 and Thermal (4x6) formats."""
        self.client.force_login(self.seller_a_user)
        url = reverse("core:seller_order_packing_slip", kwargs={"pk": self.package_a.pk})

        # 1. HTML view supports thermal mode and A4
        resp_html = self.client.get(url)
        self.assertEqual(resp_html.status_code, 200)
        self.assertContains(resp_html, "@media print")
        self.assertContains(resp_html, "window.print()")
        self.assertContains(resp_html, "btn-layout-thermal")
        self.assertContains(resp_html, "btn-layout-a4")

        # 2. ReportLab PDF A4 Generation
        pdf_a4 = generate_amazon_style_packing_slip_pdf(self.package_a, page_size="a4")
        self.assertTrue(pdf_a4.startswith(b"%PDF-1.4"))
        self.assertIn(b"Pooja Sharma", pdf_a4)
        self.assertIn(b"Flat 402, Sunshine Enclave", pdf_a4)
        self.assertIn(b"9876543210", pdf_a4)

        # 3. ReportLab PDF 4x6 Thermal Generation
        pdf_thermal = generate_amazon_style_packing_slip_pdf(self.package_a, page_size="4x6")
        self.assertTrue(pdf_thermal.startswith(b"%PDF-1.4"))
        self.assertIn(b"Royal Heritage Textiles", pdf_thermal)

        # 4. Direct PDF endpoint
        resp_pdf = self.client.get(f"{url}?format=pdf&size=4x6")
        self.assertEqual(resp_pdf.status_code, 200)
        self.assertEqual(resp_pdf["Content-Type"], "application/pdf")
        self.assertTrue(resp_pdf.content.startswith(b"%PDF-1.4"))

    def test_04_seller_marks_package_packed_ready_for_pickup(self):
        """Requirement 4: Seller can mark package as Packed / Ready for Pickup."""
        self.client.force_login(self.seller_a_user)
        update_url = reverse("core:seller_order_update_status", kwargs={"pk": self.package_a.pk})

        # 1. Move from pending to ready_to_ship (Packed / Ready for Pickup)
        resp = self.client.post(update_url, data={"status": SellerOrder.STATUS_READY_TO_SHIP})
        self.assertEqual(resp.status_code, 302)

        self.package_a.refresh_from_db()
        self.assertEqual(self.package_a.status, SellerOrder.STATUS_READY_TO_SHIP)

    def test_05_ship_with_shiprocket_action_availability(self):
        """Requirement 5: 'Ship with Shiprocket' is available after marking Ready to Ship."""
        self.client.force_login(self.seller_a_user)
        detail_url = reverse("core:seller_order_detail", kwargs={"pk": self.package_a.pk})

        # When status is PENDING: Ship with Shiprocket is ineligible / not displayed
        resp_pending = self.client.get(detail_url)
        self.assertNotContains(resp_pending, 'id="btn-dispatch-shiprocket"')

        # Mark package as Ready to Ship
        self.package_a.status = SellerOrder.STATUS_READY_TO_SHIP
        self.package_a.save(update_fields=["status"])

        # Now when status is READY TO SHIP: Ship with Shiprocket is active and visible
        resp_ready = self.client.get(detail_url)
        self.assertContains(resp_ready, 'id="btn-dispatch-shiprocket"')
        self.assertContains(resp_ready, "Ship with Shiprocket")

    @override_settings(SHIPROCKET_MOCK_MODE=True)
    def test_06_shiprocket_booking_saves_awb_courier_and_tracking_url(self):
        """Requirement 6: Booking saves AWB, Courier Name, and Tracking URL on package and ShipmentBooking."""
        from core.shiprocket import create_or_dispatch_shipment

        self.package_a.status = SellerOrder.STATUS_READY_TO_SHIP
        self.package_a.save(update_fields=["status"])

        booking = create_or_dispatch_shipment(
            package_id=self.package_a.pk,
            idempotency_key="test-booking-key-456",
            user=self.seller_a_user,
        )

        self.assertIsNotNone(booking)
        self.assertTrue(booking.awb_code.startswith("MOCK-AWB-"))
        self.assertIn("Delhivery", booking.courier_name)
        self.assertTrue(booking.tracking_url.startswith("https://shiprocket.co/tracking/"))

        self.package_a.refresh_from_db()
        self.assertEqual(self.package_a.tracking_number, booking.awb_code)
        self.assertEqual(self.package_a.courier_name, booking.courier_name)
        self.assertEqual(self.package_a.tracking_url, booking.tracking_url)

    def test_07_shipping_label_button_only_after_booking_and_mock_mode_safety(self):
        """Requirement 7: 'Print Shipping Label' appears ONLY after booking; Mock mode clearly states MOCK."""
        self.client.force_login(self.seller_a_user)
        detail_url = reverse("core:seller_order_detail", kwargs={"pk": self.package_a.pk})

        # 1. Before booking: NO shipping label button
        resp_before = self.client.get(detail_url)
        self.assertNotContains(resp_before, 'id="seller-download-shipping-label-btn"')

        # 2. Create mock booking
        booking = ShipmentBooking.objects.create(
            package=self.package_a,
            idempotency_key="test-sim-key-789",
            is_mock=True,
            status="booked",
            shipment_id="MOCK-SHP-001",
            awb_code="MOCK-AWB-998877",
            courier_name="Delhivery Surface (Simulated)",
            chargeable_weight_kg=Decimal("0.850"),
            estimated_rate=Decimal("80.00"),
            tracking_status="MANIFEST_GENERATED",
            tracking_url="https://shiprocket.co/tracking/MOCK-AWB-998877",
        )

        # 3. After booking: Shipping label button appears
        resp_after = self.client.get(detail_url)
        self.assertContains(resp_after, 'id="seller-download-shipping-label-btn"')
        self.assertContains(resp_after, "Print Shipping Label (4x6)")

        # 4. Mock Mode label PDF verifies clear MOCK simulation indication, no fake realistic barcodes
        label_pdf = generate_courier_shipping_label_pdf(self.package_a, page_size="4x6")
        self.assertTrue(label_pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"SIMULATED DISPATCH", label_pdf)
        self.assertIn(b"NOT A CARRIER LABEL", label_pdf)
        self.assertIn(b"NO CARRIER AWB GENERATED", label_pdf)

    def test_08_tracking_status_and_pickup_status_in_dashboards(self):
        """Requirement 8: Tracking Status and Pickup Status appear in Seller and Admin dashboards."""
        booking = ShipmentBooking.objects.create(
            package=self.package_a,
            idempotency_key="test-dash-key-111",
            is_mock=True,
            status="booked",
            shipment_id="MOCK-SHP-DASH-1",
            awb_code="MOCK-AWB-DASH-1",
            courier_name="Delhivery Surface",
            pickup_status=ShipmentBooking.PICKUP_SCHEDULED,
            pickup_scheduled_date=timezone.now() + timezone.timedelta(days=1),
            tracking_status="IN TRANSIT",
            tracking_url="https://shiprocket.co/tracking/MOCK-AWB-DASH-1",
        )

        # 1. Seller Dashboard
        self.client.force_login(self.seller_a_user)
        resp_seller_dash = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(resp_seller_dash.status_code, 200)
        self.assertContains(resp_seller_dash, "Pickup: Scheduled")
        self.assertContains(resp_seller_dash, "IN TRANSIT")

        # 2. Seller Orders List
        resp_seller_list = self.client.get(reverse("core:seller_order_list"))
        self.assertEqual(resp_seller_list.status_code, 200)
        self.assertContains(resp_seller_list, "Pickup: Scheduled")
        self.assertContains(resp_seller_list, "IN TRANSIT")

        # 3. Admin Order Detail
        self.client.force_login(self.admin_user)
        resp_admin_detail = self.client.get(reverse("custom_admin:order_detail", kwargs={"pk": self.order.pk}))
        self.assertEqual(resp_admin_detail.status_code, 200)
        self.assertContains(resp_admin_detail, "Scheduled")
        self.assertContains(resp_admin_detail, "IN TRANSIT")

    def test_09_customer_shipment_tracking_available(self):
        """Requirement 9: Customer has access to package shipment tracking."""
        self.package_a.tracking_number = "MOCK-AWB-TRACK-99"
        self.package_a.courier_name = "Blue Dart Express"
        self.package_a.tracking_url = "https://shiprocket.co/tracking/MOCK-AWB-TRACK-99"
        self.package_a.save()

        ShipmentBooking.objects.create(
            package=self.package_a,
            idempotency_key="test-cust-track-key",
            is_mock=True,
            status="booked",
            shipment_id="MOCK-SHP-CUST-1",
            awb_code="MOCK-AWB-TRACK-99",
            courier_name="Blue Dart Express",
            tracking_status="IN TRANSIT",
            tracking_url="https://shiprocket.co/tracking/MOCK-AWB-TRACK-99",
        )

        # 1. Customer Order Detail
        self.client.force_login(self.customer_user)
        resp_cust = self.client.get(reverse("core:order_detail", kwargs={"order_number": self.order.order_number}))
        self.assertEqual(resp_cust.status_code, 200)
        self.assertContains(resp_cust, "Blue Dart Express")
        self.assertContains(resp_cust, "MOCK-AWB-TRACK-99")
        self.assertContains(resp_cust, "Track Live")

        # 2. Customer Track Order Portal
        resp_track = self.client.get(
            reverse("core:order_track"),
            data={"order_number": self.order.order_number, "verification": "customer@example.com"},
        )
        self.assertEqual(resp_track.status_code, 200)
        self.assertContains(resp_track, "MOCK-AWB-TRACK-99")
        self.assertContains(resp_track, "Track Live")

    def test_10_payment_unpaid_does_not_show_paid_or_misrepresented(self):
        """Requirement 10: Payment Unpaid does not show paid/delivered falsely. Statuses are decoupled."""
        # Create an unpaid COD order
        cod_order = Order.objects.create(
            user=self.customer_user,
            order_number="FM-COD-112233",
            status=Order.STATUS_PENDING,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            payment_method=Order.PAYMENT_METHOD_COD,
            subtotal=Decimal("999.00"),
            total_amount=Decimal("999.00"),
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Park Street",
            shipping_city="Gurugram",
            shipping_state="Haryana",
            shipping_postal_code="122001",
        )

        self.client.force_login(self.customer_user)
        resp_detail = self.client.get(reverse("core:order_detail", kwargs={"order_number": cod_order.order_number}))
        self.assertEqual(resp_detail.status_code, 200)

        # Must NOT show Paid badge
        self.assertNotContains(resp_detail, '<span class="badge bg-success text-white"><i class="bi bi-check-circle-fill me-1"></i> Paid</span>')
        # Must show Pay on Delivery (COD) badge
        self.assertContains(resp_detail, "Pay on Delivery (COD)")

        # Create an unpaid Razorpay online order
        online_unpaid_order = Order.objects.create(
            user=self.customer_user,
            order_number="FM-ONLINE-UNPAID",
            status=Order.STATUS_PENDING,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            subtotal=Decimal("999.00"),
            total_amount=Decimal("999.00"),
            shipping_full_name="Pooja Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Park Street",
            shipping_city="Gurugram",
            shipping_state="Haryana",
            shipping_postal_code="122001",
        )
        resp_online = self.client.get(reverse("core:order_detail", kwargs={"order_number": online_unpaid_order.order_number}))
        self.assertEqual(resp_online.status_code, 200)
        # Must NOT show (COD) for online payment
        self.assertNotContains(resp_online, "Payment Unpaid (COD)")
        self.assertContains(resp_online, "Payment Pending")

    def test_11_multiseller_package_isolation(self):
        """Requirement 11: In Multi-seller orders, each seller can ONLY access their own package slip and label."""
        # Seller A tries to access Seller B's packing slip -> 404
        self.client.force_login(self.seller_a_user)
        resp_b_slip = self.client.get(reverse("core:seller_order_packing_slip", kwargs={"pk": self.package_b.pk}))
        self.assertEqual(resp_b_slip.status_code, 404)

        # Seller A tries to access Seller B's shipping label -> 404
        resp_b_label = self.client.get(reverse("core:seller_order_shipping_label", kwargs={"pk": self.package_b.pk}))
        self.assertEqual(resp_b_label.status_code, 404)

        # Seller B tries to access Seller A's packing slip -> 404
        self.client.force_login(self.seller_b_user)
        resp_a_slip = self.client.get(reverse("core:seller_order_packing_slip", kwargs={"pk": self.package_a.pk}))
        self.assertEqual(resp_a_slip.status_code, 404)

        # But Seller A CAN access their own package slip
        self.client.force_login(self.seller_a_user)
        resp_a_own = self.client.get(reverse("core:seller_order_packing_slip", kwargs={"pk": self.package_a.pk}))
        self.assertEqual(resp_a_own.status_code, 200)

    def test_12_permissions_and_access_control(self):
        """Requirement 12: Proper permissions enforced on packing slips and labels."""
        slip_url = reverse("core:seller_order_packing_slip", kwargs={"pk": self.package_a.pk})
        label_url = reverse("core:seller_order_shipping_label", kwargs={"pk": self.package_a.pk})

        # 1. Anonymous user redirected to login
        self.client.logout()
        resp_anon_slip = self.client.get(slip_url)
        self.assertEqual(resp_anon_slip.status_code, 302)
        self.assertIn("login", resp_anon_slip.url)

        resp_anon_label = self.client.get(label_url)
        self.assertEqual(resp_anon_label.status_code, 302)
        self.assertIn("login", resp_anon_label.url)

        # 2. Non-seller regular customer denied (403)
        self.client.force_login(self.customer_user)
        resp_cust_slip = self.client.get(slip_url)
        self.assertEqual(resp_cust_slip.status_code, 403)

        resp_cust_label = self.client.get(label_url)
        self.assertEqual(resp_cust_label.status_code, 403)

        # 3. Staff / Superuser has full authorized access
        self.client.force_login(self.admin_user)
        resp_admin_slip = self.client.get(slip_url)
        self.assertEqual(resp_admin_slip.status_code, 200)

        resp_admin_label = self.client.get(label_url)
        self.assertEqual(resp_admin_label.status_code, 200)

    @override_settings(SHIPROCKET_MOCK_MODE=True)
    def test_13_admin_package_fulfillment_actions_and_status_reconciliation(self):
        """Verify Admin Order Detail displays package actions and status updates reconcile parent order."""
        self.client.force_login(self.admin_user)
        detail_url = reverse("custom_admin:order_detail", kwargs={"pk": self.order.pk})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)

        # Multi-seller table should contain packages
        self.assertContains(resp, self.package_a.sub_order_number)
        self.assertContains(resp, self.package_b.sub_order_number)

        # 1. Admin accepts/processes Package A
        update_url = reverse(
            "custom_admin:order_package_update_status",
            kwargs={"order_id": self.order.pk, "pk": self.package_a.pk},
        )
        post_resp = self.client.post(update_url, data={"action": "process"})
        self.assertEqual(post_resp.status_code, 302)

        self.package_a.refresh_from_db()
        self.assertEqual(self.package_a.status, SellerOrder.STATUS_PROCESSING)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PROCESSING)

        # 2. Admin marks Package A as Packed
        pack_resp = self.client.post(update_url, data={"action": "packed"})
        self.assertEqual(pack_resp.status_code, 302)
        self.package_a.refresh_from_db()
        self.assertEqual(self.package_a.status, SellerOrder.STATUS_READY_TO_SHIP)
        self.assertEqual(self.package_a.package_dimension_status, SellerOrder.PACKAGE_DIMENSION_VERIFIED)

    @override_settings(SHIPROCKET_MOCK_MODE=True)
    def test_14_admin_package_dispatch_and_schedule_pickup(self):
        """Verify Admin can dispatch shipment via Shiprocket mock and schedule carrier pickup."""
        self.client.force_login(self.admin_user)

        self.package_a.status = SellerOrder.STATUS_READY_TO_SHIP
        self.package_a.save(update_fields=["status"])

        # 1. Admin dispatches shipment
        dispatch_url = reverse(
            "custom_admin:order_package_dispatch_shipment",
            kwargs={"order_id": self.order.pk, "pk": self.package_a.pk},
        )
        disp_resp = self.client.post(dispatch_url)
        self.assertEqual(disp_resp.status_code, 302)

        self.package_a.refresh_from_db()
        self.assertTrue(self.package_a.is_booked)
        booking = self.package_a.shipment_booking
        self.assertTrue(booking.awb_code.startswith("MOCK-AWB-"))

        # 2. Admin schedules pickup
        pickup_url = reverse(
            "custom_admin:order_package_schedule_pickup",
            kwargs={"order_id": self.order.pk, "pk": self.package_a.pk},
        )
        pickup_resp = self.client.post(pickup_url)
        self.assertEqual(pickup_resp.status_code, 302)

        booking.refresh_from_db()
        self.assertEqual(booking.pickup_status, ShipmentBooking.PICKUP_SCHEDULED)
        self.assertIsNotNone(booking.pickup_token_number)

        # 3. Admin accesses shipping label
        label_url = reverse(
            "custom_admin:order_package_shipping_label",
            kwargs={"order_id": self.order.pk, "pk": self.package_a.pk},
        )
        label_resp = self.client.get(label_url)
        self.assertEqual(label_resp.status_code, 200)
        self.assertTrue(label_resp.content.startswith(b"%PDF-1.4"))

    def test_15_inconsistent_order_status_sync_and_validation_guard(self):
        """Verify that inconsistent Order statuses are reconciled and prevented."""
        from core.admin_forms import AdminOrderStatusForm

        # Cancel all packages
        self.package_a.status = SellerOrder.STATUS_CANCELLED
        self.package_a.save(update_fields=["status"])
        self.package_b.status = SellerOrder.STATUS_CANCELLED
        self.package_b.save(update_fields=["status"])

        # Set parent order to Shipped (the inconsistent state reported by user)
        self.order.status = Order.STATUS_SHIPPED
        self.order.save(update_fields=["status"])

        # sync_status_from_packages should automatically fix parent status to cancelled
        synced_status = self.order.sync_status_from_packages(commit=True)
        self.assertEqual(synced_status, Order.STATUS_CANCELLED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_CANCELLED)

        # AdminOrderStatusForm should prevent marking order Shipped if all packages are cancelled
        form = AdminOrderStatusForm(order=self.order, data={"status": Order.STATUS_SHIPPED})
        self.assertFalse(form.is_valid())
        self.assertIn("status", form.errors)

    @override_settings(SHIPROCKET_MOCK_MODE=True)
    def test_16_seller_schedule_pickup_endpoint(self):
        """Verify authorized seller can schedule courier pickup for their booked package."""
        self.client.force_login(self.seller_a_user)

        self.package_a.status = SellerOrder.STATUS_READY_TO_SHIP
        self.package_a.save(update_fields=["status"])

        # Book mock shipment first
        from core.shiprocket import create_or_dispatch_shipment
        booking = create_or_dispatch_shipment(package_id=self.package_a.pk, user=self.seller_a_user)
        booking.pickup_status = ShipmentBooking.PICKUP_NOT_REQUESTED
        booking.save(update_fields=["pickup_status"])

        pickup_url = reverse("core:seller_order_schedule_pickup", kwargs={"pk": self.package_a.pk})
        resp = self.client.post(pickup_url)
        self.assertEqual(resp.status_code, 302)

        booking.refresh_from_db()
        self.assertEqual(booking.pickup_status, ShipmentBooking.PICKUP_SCHEDULED)
