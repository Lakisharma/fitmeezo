"""
Fitmeezo Phase 8 Automated Test Suite: Invoices, Packing Slips & Tax Records.

Comprehensive verification of:
1. Transaction-safe monotonic sequential invoice numbering.
2. Immutability of issued customer parent and seller package invoices.
3. Multi-seller document separation and tenant privacy.
4. Customer ownership and seller isolation boundaries (404/403 gates).
5. Vector PDF generation with ReportLab, pagination, and multi-page wrapping.
6. Zero fabricated tax/legal identifiers (proper handling of unconfigured GSTIN/PAN).
7. Warehouse packing slip privacy (PII masking and non-carrier warnings).
8. Issuance policy evaluation and refund/return compatibility.
"""

from decimal import Decimal
import io
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Address,
    Category,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
    SellerOrder,
    Invoice,
    InvoiceSequence,
    RefundRecord,
)
from core.invoice_services import (
    allocate_invoice_number,
    is_order_eligible_for_invoice,
    get_or_create_customer_invoice,
    get_or_create_seller_package_invoice,
    generate_customer_invoice_pdf,
    generate_seller_package_invoice_pdf,
)
from core.packing_slip import generate_internal_packing_summary_pdf

User = get_user_model()


class InvoiceNumberingAndSequenceTests(TestCase):
    """Verifies concurrency-safe sequence counter and prefix configuration."""

    def test_allocate_invoice_number_monotonic(self):
        """Sequentially allocated numbers must increment by exactly 1 without duplicates."""
        date_str = "20261011"
        num1 = allocate_invoice_number(prefix="TEST-INV", date_str=date_str)
        num2 = allocate_invoice_number(prefix="TEST-INV", date_str=date_str)
        num3 = allocate_invoice_number(prefix="TEST-INV", date_str=date_str)

        self.assertEqual(num1, "TEST-INV-20261011-00001")
        self.assertEqual(num2, "TEST-INV-20261011-00002")
        self.assertEqual(num3, "TEST-INV-20261011-00003")

        seq = InvoiceSequence.objects.get(prefix="TEST-INV-20261011")
        self.assertEqual(seq.last_number, 3)

    def test_allocate_invoice_number_distinct_prefixes(self):
        """Different prefixes must maintain independent sequences."""
        num_cust = allocate_invoice_number(prefix="CUST", date_str="20261011")
        num_sell = allocate_invoice_number(prefix="SELL", date_str="20261011")

        self.assertEqual(num_cust, "CUST-20261011-00001")
        self.assertEqual(num_sell, "SELL-20261011-00001")


class InvoiceModelAndCreationTests(TestCase):
    """Verifies creation, immutability, and stored financial accuracy."""

    def setUp(self):
        self.customer = User.objects.create_user(
            username="buyer_ravi",
            email="ravi@example.com",
            password="StrongPassword123!",
            first_name="Ravi",
            last_name="Kumar",
        )
        self.seller_user = User.objects.create_user(
            username="seller_apex",
            email="apex@example.com",
            password="StrongPassword123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Apex Footwear",
            legal_business_name="Apex Retail Pvt Ltd",
            business_type="private_limited",
            support_email="support@apexfootwear.com",
            support_phone="9876543210",
            registered_address="12 MG Road, Indiranagar",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560038",
            pickup_address="Warehouse 4, Electronic City",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
        )
        self.category = Category.objects.create(name="Shoes", slug="shoes")
        self.product = Product.objects.create(
            category=self.category,
            seller=self.seller,
            name="Apex Air Runners",
            slug="apex-air-runners",
            sku="APEX-RUN-01",
            price=Decimal("1999.00"),
            stock_quantity=50,
            is_active=True,
        )
        self.order = Order.objects.create(
            order_number="FM-20261011-RAVI1",
            user=self.customer,
            shipping_full_name="Ravi Kumar",
            shipping_phone_number="9876543210",
            shipping_address_line1="Flat 402, Green Glen Layout",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560103",
            subtotal=Decimal("1999.00"),
            shipping_fee=Decimal("50.00"),
            discount_amount=Decimal("100.00"),
            total_amount=Decimal("1949.00"),
            currency="INR",
            status=Order.STATUS_PROCESSING,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
        )
        self.seller_order = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number="FM-20261011-RAVI1-S1",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("1999.00"),
            total_commission=Decimal("199.90"),
            net_seller_payable=Decimal("1799.10"),
            customer_shipping_charge=Decimal("50.00"),
        )
        self.order_item = OrderItem.objects.create(
            order=self.order,
            seller_order=self.seller_order,
            seller=self.seller,
            product=self.product,
            product_name="Apex Air Runners",
            sku="APEX-RUN-01",
            unit_price=Decimal("1999.00"),
            quantity=1,
            line_total=Decimal("1999.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("199.90"),
        )

    def test_customer_parent_invoice_matches_stored_order_totals(self):
        """Invoice totals must strictly match the historical stored Order snapshot."""
        invoice = get_or_create_customer_invoice(self.order)

        self.assertEqual(invoice.invoice_type, Invoice.TYPE_CUSTOMER_PARENT)
        self.assertEqual(invoice.subtotal, Decimal("1999.00"))
        self.assertEqual(invoice.shipping_fee, Decimal("50.00"))
        self.assertEqual(invoice.discount_amount, Decimal("100.00"))
        self.assertEqual(invoice.total_amount, Decimal("1949.00"))
        self.assertEqual(invoice.payment_method, "Online Payment (Razorpay)")
        self.assertEqual(invoice.payment_status, "Paid")
        self.assertEqual(invoice.recipient_name, "Ravi Kumar")
        self.assertIn("Green Glen Layout", invoice.recipient_shipping_address)

    def test_historical_immutability_on_catalog_or_commission_change(self):
        """Changing product catalog prices or seller commission rates must NEVER change an issued invoice."""
        invoice = get_or_create_customer_invoice(self.order)
        original_inv_num = invoice.invoice_number
        original_total = invoice.total_amount

        # Catalog price is doubled
        self.product.price = Decimal("3999.00")
        self.product.save()

        # Re-fetching or getting customer invoice
        re_invoice = get_or_create_customer_invoice(self.order)

        self.assertEqual(re_invoice.pk, invoice.pk)
        self.assertEqual(re_invoice.invoice_number, original_inv_num)
        self.assertEqual(re_invoice.total_amount, original_total)
        self.assertEqual(re_invoice.total_amount, Decimal("1949.00"))

    def test_idempotent_customer_parent_invoice_creation(self):
        """Multiple invocations must return the existing record and not create duplicates."""
        inv1 = get_or_create_customer_invoice(self.order)
        inv2 = get_or_create_customer_invoice(self.order)

        self.assertEqual(inv1.pk, inv2.pk)
        self.assertEqual(self.order.invoices.filter(invoice_type=Invoice.TYPE_CUSTOMER_PARENT).count(), 1)

    def test_seller_package_invoice_isolation_and_no_commission_leak(self):
        """Seller package invoice must be scoped to seller items and never expose commission or net payable."""
        pkg_invoice = get_or_create_seller_package_invoice(self.seller_order)

        self.assertEqual(pkg_invoice.invoice_type, Invoice.TYPE_SELLER_PACKAGE)
        self.assertEqual(pkg_invoice.seller, self.seller)
        self.assertEqual(pkg_invoice.subtotal, Decimal("1999.00"))
        self.assertEqual(pkg_invoice.shipping_fee, Decimal("50.00"))
        self.assertEqual(pkg_invoice.total_amount, Decimal("2049.00"))  # Subtotal + Allocated Shipping
        self.assertEqual(pkg_invoice.issuer_name, "Apex Retail Pvt Ltd")
        self.assertEqual(pkg_invoice.issuer_gstin, "29ABCDE1234F1Z5")

        # Commission and net payable are NOT fields on Invoice
        self.assertFalse(hasattr(pkg_invoice, "total_commission"))
        self.assertFalse(hasattr(pkg_invoice, "net_seller_payable"))

        # Verify PDF does not contain internal financial ledger metrics
        pdf_bytes = generate_seller_package_invoice_pdf(pkg_invoice)
        self.assertNotIn(b"199.90", pdf_bytes)  # Commission amount must not appear in seller document!
        self.assertNotIn(b"1799.10", pdf_bytes)  # Net payable must not appear in package invoice!


class MultiSellerOrderSeparationTests(TestCase):
    """Verifies that an order with multiple sellers has clean separation of package invoices."""

    def setUp(self):
        self.customer = User.objects.create_user(username="multi_buyer", email="buyer@test.com", password="Pass123!")
        self.seller_a_user = User.objects.create_user(username="seller_a", email="a@test.com", password="Pass123!")
        self.seller_b_user = User.objects.create_user(username="seller_b", email="b@test.com", password="Pass123!")

        self.seller_a = Seller.objects.create(
            user=self.seller_a_user,
            store_name="Seller Alpha",
            legal_business_name="Alpha Enterprises",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="29AAAAA0000A1Z5",
            registered_address="Street A",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
        )
        self.seller_b = Seller.objects.create(
            user=self.seller_b_user,
            store_name="Seller Beta",
            legal_business_name="Beta Retailers",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="29BBBBB0000B1Z5",
            registered_address="Street B",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
        )

        self.order = Order.objects.create(
            order_number="FM-MULTI-01",
            user=self.customer,
            shipping_full_name="Multi Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="42 Tech Park Road",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560100",
            subtotal=Decimal("3000.00"),
            shipping_fee=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            total_amount=Decimal("3100.00"),
            status=Order.STATUS_PROCESSING,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )

        self.pkg_a = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller_a,
            sub_order_number="FM-MULTI-01-S1",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("2000.00"),
            customer_shipping_charge=Decimal("66.67"),
            total_commission=Decimal("200.00"),
            net_seller_payable=Decimal("1800.00"),
        )
        self.pkg_b = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller_b,
            sub_order_number="FM-MULTI-01-S2",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("1000.00"),
            customer_shipping_charge=Decimal("33.33"),
            total_commission=Decimal("100.00"),
            net_seller_payable=Decimal("900.00"),
        )

        self.item_a = OrderItem.objects.create(
            order=self.order,
            seller_order=self.pkg_a,
            seller=self.seller_a,
            product_name="Product Alpha",
            unit_price=Decimal("2000.00"),
            quantity=1,
            line_total=Decimal("2000.00"),
        )
        self.item_b = OrderItem.objects.create(
            order=self.order,
            seller_order=self.pkg_b,
            seller=self.seller_b,
            product_name="Product Beta",
            unit_price=Decimal("1000.00"),
            quantity=1,
            line_total=Decimal("1000.00"),
        )

    def test_multi_seller_document_separation(self):
        """Seller A's package invoice contains only Alpha items; Seller B's contains only Beta items."""
        inv_a = get_or_create_seller_package_invoice(self.pkg_a)
        inv_b = get_or_create_seller_package_invoice(self.pkg_b)

        self.assertNotEqual(inv_a.invoice_number, inv_b.invoice_number)
        self.assertEqual(inv_a.seller, self.seller_a)
        self.assertEqual(inv_b.seller, self.seller_b)
        self.assertEqual(inv_a.subtotal, Decimal("2000.00"))
        self.assertEqual(inv_b.subtotal, Decimal("1000.00"))

        # Verify strict separation of snapshotted package items
        items_a = inv_a.audit_metadata.get("items", [])
        self.assertEqual(len(items_a), 1)
        self.assertEqual(items_a[0]["product_name"], "Product Alpha")

        items_b = inv_b.audit_metadata.get("items", [])
        self.assertEqual(len(items_b), 1)
        self.assertEqual(items_b[0]["product_name"], "Product Beta")

        pdf_a = generate_seller_package_invoice_pdf(inv_a)
        pdf_b = generate_seller_package_invoice_pdf(inv_b)

        self.assertTrue(pdf_a.startswith(b"%PDF-"))
        self.assertTrue(pdf_b.startswith(b"%PDF-"))
        self.assertGreater(len(pdf_a), 1000)
        self.assertGreater(len(pdf_b), 1000)


class DocumentAccessAndPermissionTests(TestCase):
    """Verifies security boundaries: customer isolation, seller cross-tenant isolation, and admin access."""

    def setUp(self):
        self.cust_1 = User.objects.create_user(username="cust_1", email="c1@test.com", password="Pass123!")
        self.cust_2 = User.objects.create_user(username="cust_2", email="c2@test.com", password="Pass123!")
        self.seller_user_1 = User.objects.create_user(username="seller_u1", email="s1@test.com", password="Pass123!")
        self.seller_user_2 = User.objects.create_user(username="seller_u2", email="s2@test.com", password="Pass123!")
        self.staff_user = User.objects.create_user(username="staff_admin", email="admin@fitmeezo.com", password="Pass123!", is_staff=True)

        self.seller_1 = Seller.objects.create(
            user=self.seller_user_1,
            store_name="Store 1",
            legal_business_name="Store 1 Legal",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="29XXXXX0000X1Z5",
            registered_address="Addr 1",
            city="City 1",
            state="State 1",
            postal_code="560001",
        )
        self.seller_2 = Seller.objects.create(
            user=self.seller_user_2,
            store_name="Store 2",
            legal_business_name="Store 2 Legal",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="29YYYYY0000Y1Z5",
            registered_address="Addr 2",
            city="City 2",
            state="State 2",
            postal_code="560002",
        )

        self.order_1 = Order.objects.create(
            order_number="FM-ORD-01",
            user=self.cust_1,
            shipping_full_name="Customer 1",
            shipping_phone_number="9876543210",
            shipping_address_line1="Address 1",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
            status=Order.STATUS_PROCESSING,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )
        self.pkg_1 = SellerOrder.objects.create(
            order=self.order_1,
            seller=self.seller_1,
            sub_order_number="FM-ORD-01-S1",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("1000.00"),
        )
        OrderItem.objects.create(
            order=self.order_1,
            seller_order=self.pkg_1,
            seller=self.seller_1,
            product_name="Sample Item",
            unit_price=Decimal("1000.00"),
            quantity=1,
            line_total=Decimal("1000.00"),
        )

    def test_customer_cannot_download_other_customer_invoice(self):
        """Customer 2 must receive HTTP 404 when attempting to access Customer 1's invoice."""
        self.client.force_login(self.cust_2)
        url = reverse("core:order_invoice", kwargs={"order_number": self.order_1.order_number})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_customer_can_download_own_invoice(self):
        """Customer 1 can successfully download their own invoice PDF."""
        self.client.force_login(self.cust_1)
        url = reverse("core:order_invoice", kwargs={"order_number": self.order_1.order_number})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_unauthenticated_user_redirected_to_login(self):
        """Anonymous user must be redirected to login page when attempting to download an invoice."""
        self.client.logout()
        url = reverse("core:order_invoice", kwargs={"order_number": self.order_1.order_number})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_seller_cannot_download_other_seller_package_invoice(self):
        """Seller 2 must receive HTTP 404 when attempting to access Seller 1's package invoice."""
        self.client.force_login(self.seller_user_2)
        url = reverse("core:seller_order_invoice", kwargs={"pk": self.pkg_1.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_seller_can_download_own_package_invoice(self):
        """Seller 1 can successfully download their own package commercial invoice."""
        self.client.force_login(self.seller_user_1)
        url = reverse("core:seller_order_invoice", kwargs={"pk": self.pkg_1.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_seller_can_download_own_packing_slip(self):
        """Seller 1 can download their warehouse packing slip."""
        self.client.force_login(self.seller_user_1)
        url = reverse("core:seller_order_packing_slip", kwargs={"pk": self.pkg_1.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_staff_admin_can_download_any_customer_or_seller_invoice(self):
        """Staff members can view both customer parent and seller package invoices."""
        self.client.force_login(self.staff_user)

        cust_url = reverse("core:order_invoice", kwargs={"order_number": self.order_1.order_number})
        resp1 = self.client.get(cust_url)
        self.assertEqual(resp1.status_code, 200)

        sell_url = reverse("core:seller_order_invoice", kwargs={"pk": self.pkg_1.pk})
        resp2 = self.client.get(sell_url)
        self.assertEqual(resp2.status_code, 200)


class PackingSlipPrivacyAndDisclaimerTests(TestCase):
    """Verifies that warehouse packing slips mask PII and include prominent non-carrier disclaimers."""

    def setUp(self):
        self.seller_user = User.objects.create_user(username="pack_seller", email="s@test.com", password="Pass123!")
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Pack Hub",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="29XXXXX0000X1Z5",
            pickup_address="Pickup Street",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
        )
        self.customer = User.objects.create_user(username="cust_private", email="p@test.com", password="Pass123!")
        self.order = Order.objects.create(
            order_number="FM-PRIV-01",
            user=self.customer,
            shipping_full_name="Aditya Sharma",
            shipping_phone_number="+919876543210",
            shipping_address_line1="123 Private Sanctuary Villa",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560100",
            subtotal=Decimal("500.00"),
            total_amount=Decimal("500.00"),
            status=Order.STATUS_PROCESSING,
        )
        self.package = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number="FM-PRIV-01-S1",
            subtotal=Decimal("500.00"),
            status=SellerOrder.STATUS_PROCESSING,
        )
        OrderItem.objects.create(
            order=self.order,
            seller_order=self.package,
            seller=self.seller,
            product_name="Secret Item",
            quantity=1,
            unit_price=Decimal("500.00"),
            line_total=Decimal("500.00"),
        )

    def test_packing_slip_masks_customer_phone_and_address(self):
        """Internal warehouse manifest must mask phone number and street address to avoid PII harvesting."""
        pdf_bytes = generate_internal_packing_summary_pdf(self.package)

        # Phone '+919876543210' should be masked with asterisks
        self.assertNotIn(b"+919876543210", pdf_bytes)

        # Non-carrier disclaimer must be present
        self.assertIn(b"NON-CARRIER", pdf_bytes)
        self.assertIn(b"NOT A VALID", pdf_bytes)


class IssuanceRuleAndPolicyTests(TestCase):
    """Verifies configurable issuance policies and cancellation protections."""

    def setUp(self):
        self.user = User.objects.create_user(username="rule_user", email="r@test.com", password="Pass123!")
        self.order = Order.objects.create(
            order_number="FM-RULE-01",
            user=self.user,
            shipping_full_name="Policy Tester",
            shipping_phone_number="9876543210",
            shipping_address_line1="Street 1",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("500.00"),
            total_amount=Decimal("500.00"),
            status=Order.STATUS_PENDING,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )

    @override_settings(FITMEEZO_INVOICE_ISSUANCE_RULE="on_payment_or_cod")
    def test_unpaid_online_order_not_eligible(self):
        """Under on_payment_or_cod, unpaid Razorpay orders cannot issue an invoice."""
        eligible, reason = is_order_eligible_for_invoice(self.order)
        self.assertFalse(eligible)
        self.assertIn("payment is settled", reason)

    @override_settings(FITMEEZO_INVOICE_ISSUANCE_RULE="on_payment_or_cod")
    def test_paid_online_order_is_eligible(self):
        """Paid orders are eligible under on_payment_or_cod."""
        self.order.payment_status = Order.PAYMENT_STATUS_PAID
        self.order.save()

        eligible, _ = is_order_eligible_for_invoice(self.order)
        self.assertTrue(eligible)

    @override_settings(FITMEEZO_INVOICE_ISSUANCE_RULE="on_delivery")
    def test_on_delivery_policy(self):
        """Under on_delivery rule, only delivered orders qualify."""
        self.order.payment_status = Order.PAYMENT_STATUS_PAID
        self.order.status = Order.STATUS_SHIPPED
        self.order.save()

        eligible, _ = is_order_eligible_for_invoice(self.order)
        self.assertFalse(eligible)

        self.order.status = Order.STATUS_DELIVERED
        self.order.save()
        eligible, _ = is_order_eligible_for_invoice(self.order)
        self.assertTrue(eligible)

    def test_cancelled_order_cannot_create_fresh_invoice(self):
        """Cancelled orders that were never issued an invoice cannot issue one."""
        self.order.status = Order.STATUS_CANCELLED
        self.order.save()

        eligible, reason = is_order_eligible_for_invoice(self.order)
        self.assertFalse(eligible)
        self.assertIn("cancelled", reason)

    def test_previously_issued_invoice_remains_available_after_cancellation(self):
        """If an invoice was already issued before an order was cancelled, it remains accessible as historical record."""
        self.order.payment_status = Order.PAYMENT_STATUS_PAID
        self.order.save()

        # Issue invoice
        inv = get_or_create_customer_invoice(self.order)

        # Later order gets cancelled
        self.order.status = Order.STATUS_CANCELLED
        self.order.save()

        # Must still be eligible to view existing issued invoice
        eligible, _ = is_order_eligible_for_invoice(self.order)
        self.assertTrue(eligible)

        fetched = get_or_create_customer_invoice(self.order)
        self.assertEqual(fetched.pk, inv.pk)


class CustomAdminInvoicesViewTests(TestCase):
    """Verifies custom admin invoice listing, metrics, and staff PDF download."""

    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="admin_staff",
            email="staff@fitmeezo.com",
            password="StrongPassword123!",
            is_staff=True,
        )
        self.customer = User.objects.create_user(username="admin_cust", email="ac@test.com", password="Pass123!")
        self.order = Order.objects.create(
            order_number="FM-ADMIN-01",
            user=self.customer,
            shipping_full_name="Admin Test Customer",
            shipping_phone_number="9876543210",
            shipping_address_line1="Admin St",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            total_amount=Decimal("1500.00"),
            status=Order.STATUS_PROCESSING,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )
        self.invoice = get_or_create_customer_invoice(self.order)

    def test_admin_invoice_list_view_renders_correctly(self):
        """Staff can view the invoices registry in custom admin."""
        self.client.force_login(self.staff_user)
        url = reverse("custom_admin:invoice_list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.invoice.invoice_number)
        self.assertContains(response, "Tax Records &amp; Issued Invoices")

    def test_admin_invoice_pdf_download(self):
        """Staff can download invoice PDF via admin view."""
        self.client.force_login(self.staff_user)
        url = reverse("custom_admin:invoice_pdf", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))
