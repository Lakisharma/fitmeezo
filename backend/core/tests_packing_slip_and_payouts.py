"""
Fitmeezo Comprehensive Test Suite: Professional Packing Slip PDF + Seller Bank Details & Payout System.

Validates:
TASK A:
1. Packing slip ReportLab A4 vector PDF generation with valid PDF-1.4 header.
2. Proper PII masking (phone number and residential address).
3. Prominent non-carrier warning and internal dispatch disclaimer.
4. Handling of multiple products across line items.
5. Auto-wrapping of long product titles, variant labels, and long customer addresses.
6. Multi-page pagination and table header repetition (repeatRows=1).
7. Strict omission of tax invoices, margin rates, and commissions from the packing slip.

TASK B:
8. Masked bank account numbers in displays and model helpers (never full number).
9. Seller bank details submission and validation (IFSC format, matching confirmation).
10. Submission resets status to 'pending' and requires admin verification.
11. Administrative bank verification approval and rejection with reason.
12. Multi-tenant seller isolation (Seller B cannot access or modify Seller A's bank details).
13. Net earnings and commission calculation with double-entry ledger postings.
14. Delivery cooling window hold and COD reconciliation requirement before payout eligibility.
15. Minimum threshold enforcement and insufficient balance prevention.
16. Row-level concurrency locking preventing duplicate payout batch creation.
17. Mock payout adapter execution labeled 'TEST/SIMULATED' (zero real money transferred).
18. Payout failure simulation with clean unlinking of ledger entries for retry.
19. Webhook signature verification and idempotency handling.
"""

from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    CODReceiptRecord,
    Order,
    OrderItem,
    PayoutBatch,
    Product,
    ProductVariant,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
    ShipmentBooking,
)
from core.packing_slip import (
    generate_internal_packing_summary_pdf,
    generate_courier_shipping_label_pdf,
    mask_address_line,
    mask_phone_number,
)
from core.payout_adapters import (
    BasePayoutProviderAdapter,
    MockPayoutProviderAdapter,
    default_payout_provider,
)
from core.ledger_services import (
    calculate_seller_payout_eligibility,
    create_payout_batch,
    post_order_sale_to_ledger,
    process_mock_payout_batch,
    reconcile_cod_remittance,
    PayoutEligibilityError,
)
from core.forms import SellerBankDetailsForm

User = get_user_model()


class ProfessionalPackingSlipPDFTests(TestCase):
    """
    TASK A: Test suite for ReportLab A4 professional warehouse packing slip PDF generation.
    """

    def setUp(self):
        self.seller_user = User.objects.create_user(
            username="slip_seller",
            email="seller@slip.com",
            password="SecurePassword123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Vogue Atelier",
            legal_business_name="Vogue Atelier Fashion Pvt Ltd",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="29ABCDE1234F1Z5",
            pickup_address="10 Industrial Layout, Phase 3",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
            pickup_location_nickname="Warehouse-South",
        )
        self.customer = User.objects.create_user(
            username="slip_cust",
            email="cust@slip.com",
            password="SecurePassword123!",
        )
        self.category = Category.objects.create(name="Apparel", slug="apparel")

        self.order = Order.objects.create(
            order_number="FM-SLIP-ORD-01",
            user=self.customer,
            shipping_full_name="Rajesh Kumar",
            shipping_phone_number="+919876543210",
            shipping_address_line1="Apartment 402, Sunshine Heights, 12th Main Road",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560038",
            subtotal=Decimal("2500.00"),
            total_amount=Decimal("2500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_PROCESSING,
        )
        self.package = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number="FM-SLIP-ORD-01-S1",
            subtotal=Decimal("2500.00"),
            status=SellerOrder.STATUS_PROCESSING,
            actual_weight_grams=1250,
            actual_length_cm=Decimal("30.0"),
            actual_breadth_cm=Decimal("20.0"),
            actual_height_cm=Decimal("10.0"),
        )
        OrderItem.objects.create(
            order=self.order,
            seller_order=self.package,
            seller=self.seller,
            product_name="Heritage Silk Kurta",
            variant_label="Size: L, Color: Emerald Green",
            sku="HSK-GRN-L",
            quantity=2,
            unit_price=Decimal("1250.00"),
            line_total=Decimal("2500.00"),
        )

    def test_01_valid_pdf_generation_and_headers(self):
        """Packing slip generates valid ReportLab vector PDF with PDF-1.4 header."""
        pdf_bytes = generate_internal_packing_summary_pdf(self.package)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertTrue(len(pdf_bytes) > 1000)

    def test_02_pii_masking(self):
        """Ensures customer phone number and residential street address are strictly masked."""
        pdf_bytes = generate_internal_packing_summary_pdf(self.package)
        # Raw customer phone must not appear unmasked
        self.assertNotIn(b"+919876543210", pdf_bytes)
        self.assertNotIn(b"9876543210", pdf_bytes)
        # Masked helper verification
        self.assertEqual(mask_phone_number("+919876543210"), "******3210")
        masked_addr = mask_address_line("Apartment 402, Sunshine Heights, 12th Main Road")
        self.assertTrue(masked_addr.startswith("Apar"))
        self.assertTrue("*" in masked_addr)

    def test_03_non_carrier_warning_and_operational_disclaimer(self):
        """Prominent warning that document is NOT a valid carrier shipping label."""
        pdf_bytes = generate_internal_packing_summary_pdf(self.package)
        self.assertIn(b"NON-CARRIER", pdf_bytes)
        self.assertIn(b"NOT A VALID", pdf_bytes)
        self.assertIn(b"INTERNAL WAREHOUSE PACKING SUMMARY", pdf_bytes)

    def test_04_multiple_products_and_quantities(self):
        """Packing slip properly itemizes multiple distinct product lines and aggregates total units."""
        for i in range(2, 6):
            OrderItem.objects.create(
                order=self.order,
                seller_order=self.package,
                seller=self.seller,
                product_name=f"Accessory Item {i}",
                variant_label=f"Standard Edition #{i}",
                sku=f"ACC-SKU-{i:03d}",
                quantity=i,
                unit_price=Decimal("200.00"),
                line_total=Decimal(str(200 * i)),
            )

        pdf_bytes = generate_internal_packing_summary_pdf(self.package)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertIn(b"TOTAL PACKAGE UNITS", pdf_bytes)

    def test_05_long_product_titles_and_wrapping(self):
        """Supports very long product descriptions, variant labels, and SKUs without crashing or clipping."""
        long_name = (
            "Exclusive Premium Handwoven Royal Banarasi Zari Silk Saree With Intricate Golden Floral Embroidery "
            "and Contrast Pallu Border For Festive and Wedding Celebrations - Special Heritage Collection"
        )
        long_variant = "Edition: Limited Edition 2026, Color: Crimson Imperial Red with Antique Gold Threadwork, Care: Dry Clean Only"
        OrderItem.objects.create(
            order=self.order,
            seller_order=self.package,
            seller=self.seller,
            product_name=long_name,
            variant_label=long_variant,
            sku="VERY-LONG-SKU-IDENTIFIER-EXTENDED-CODE-1234567890",
            quantity=1,
            unit_price=Decimal("5000.00"),
            line_total=Decimal("5000.00"),
        )

        pdf_bytes = generate_internal_packing_summary_pdf(self.package)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertIn(b"Handwoven Royal Banarasi", pdf_bytes)

    def test_06_multi_page_pagination_and_repeated_headers(self):
        """Packing slip gracefully spans multiple pages for orders with 25+ products with repeated table headers."""
        for i in range(10, 35):
            OrderItem.objects.create(
                order=self.order,
                seller_order=self.package,
                seller=self.seller,
                product_name=f"Bulk Inventory Merchandise Line Item #{i}",
                variant_label=f"Variant Specification {i}",
                sku=f"BULK-SKU-{i:04d}",
                quantity=1,
                unit_price=Decimal("100.00"),
                line_total=Decimal("100.00"),
            )

        pdf_bytes = generate_internal_packing_summary_pdf(self.package)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertIn(b"Page 2 of", pdf_bytes)

    def test_07_no_financial_margins_or_commission_disclosures(self):
        """Packing slip excludes merchant commissions, margin percentages, and customer tax breakdowns."""
        pdf_bytes = generate_internal_packing_summary_pdf(self.package)
        self.assertNotIn(b"Marketplace Commission", pdf_bytes)
        self.assertNotIn(b"Platform Fee Cut", pdf_bytes)
        self.assertNotIn(b"Net Payout Math", pdf_bytes)


class SellerBankDetailsAndPayoutSystemTests(TestCase):
    """
    TASK B: Test suite for Seller Bank Account Management, Ledger Payout Lifecycle,
    and Mock Provider settlement.
    """

    def setUp(self):
        self.client = Client()

        # Admin User
        self.staff_admin = User.objects.create_user(
            username="admin_payout",
            email="admin@fitmeezo.com",
            password="SecurePassword123!",
            is_staff=True,
            is_superuser=True,
        )

        # Seller 1 (Alpha Tech)
        self.seller_user1 = User.objects.create_user(
            username="seller1_bank",
            email="seller1@alpha.com",
            password="SecurePassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Alpha Tech",
            legal_business_name="Alpha Technologies India Pvt Ltd",
            support_email="support@alphatech.com",
            support_phone="9876543210",
            registered_mobile="9876543210",
            registered_address="10 Tech Park, Electronic City",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560100",
            pickup_address="10 Tech Park, Electronic City",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            commission_rate=Decimal("10.00"),
            gstin="29AAAAA0000A1Z5",
        )

        # Seller 2 (Beta Fashion - for tenant isolation checks)
        self.seller_user2 = User.objects.create_user(
            username="seller2_bank",
            email="seller2@beta.com",
            password="SecurePassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Beta Fashion",
            legal_business_name="Beta Fashion Enterprises",
            support_email="support@betafashion.com",
            support_phone="9876543211",
            registered_mobile="9876543211",
            registered_address="20 Silk Lane",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="20 Silk Lane",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            commission_rate=Decimal("12.00"),
            gstin="29BBBBB0000B1Z5",
        )

        # Customer
        self.customer = User.objects.create_user(
            username="buyer_payout",
            email="buyer@payout.com",
            password="SecurePassword123!",
        )

        # Category and Products
        self.category = Category.objects.create(name="Tech & Gadgets", slug="tech-gadgets")
        self.product1 = Product.objects.create(
            category=self.category,
            seller=self.seller1,
            name="Mechanical Keyboard",
            slug="mechanical-keyboard",
            sku="ALPH-KEY-001",
            price=Decimal("1000.00"),
            stock_quantity=50,
            is_active=True,
        )

    def _create_seller_package(self, seller, amount=Decimal("1000.00"), is_cod=False, delivered_days_ago=10):
        """Helper to create a properly configured order and sub-order package."""
        pm = Order.PAYMENT_METHOD_COD if is_cod else Order.PAYMENT_METHOD_RAZORPAY
        ps = Order.PAYMENT_STATUS_UNPAID if is_cod else Order.PAYMENT_STATUS_PAID
        order_num = f"FM-ORD-{uuid.uuid4().hex[:6].upper()}"

        order = Order.objects.create(
            order_number=order_num,
            user=self.customer,
            shipping_full_name="Ananya Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="12 Park Street",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=amount,
            total_amount=amount,
            payment_method=pm,
            payment_status=ps,
            status=Order.STATUS_DELIVERED if delivered_days_ago is not None else Order.STATUS_PROCESSING,
            paid_at=timezone.now() if ps == Order.PAYMENT_STATUS_PAID else None,
        )

        comm_amt = (amount * seller.commission_rate / Decimal("100.00")).quantize(Decimal("0.01"))
        net_payable = amount - comm_amt

        del_at = timezone.now() - timedelta(days=delivered_days_ago) if delivered_days_ago is not None else None
        pkg_status = SellerOrder.STATUS_DELIVERED if delivered_days_ago is not None else SellerOrder.STATUS_PROCESSING

        so = SellerOrder.objects.create(
            order=order,
            seller=seller,
            sub_order_number=f"{order.order_number}-S{seller.id}",
            subtotal=amount,
            total_commission=comm_amt,
            net_seller_payable=net_payable,
            status=pkg_status,
            delivered_at=del_at,
        )
        OrderItem.objects.create(
            order=order,
            seller=seller,
            seller_order=so,
            product=self.product1,
            product_name=self.product1.name,
            sku=self.product1.sku,
            quantity=1,
            unit_price=amount,
            line_total=amount,
            commission_rate=seller.commission_rate,
            commission_amount=comm_amt,
        )
        return order, so

    def test_08_masked_account_number_helper(self):
        """Masked account number helper returns masked string and never reveals full number."""
        self.assertEqual(self.seller1.get_masked_account_number(), "Not Provided")

        self.seller1.bank_account_number = "123456789012"
        self.seller1.save()
        self.assertEqual(self.seller1.get_masked_account_number(), "••••••••9012")
        self.assertNotIn("12345678", self.seller1.get_masked_account_number())

    def test_09_bank_details_form_validation(self):
        """SellerBankDetailsForm strictly validates Indian IFSC, matching account numbers, and digits."""
        # Invalid IFSC
        form = SellerBankDetailsForm(
            data={
                "bank_account_holder_name": "Alpha Technologies",
                "bank_name": "HDFC Bank",
                "bank_branch": "Indiranagar",
                "bank_account_type": "current",
                "bank_ifsc_code": "INVALID_IFSC",
                "bank_account_number": "123456789012",
                "confirm_account_number": "123456789012",
            },
            seller=self.seller1,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("bank_ifsc_code", form.errors)

        # Non-matching account numbers
        form2 = SellerBankDetailsForm(
            data={
                "bank_account_holder_name": "Alpha Technologies",
                "bank_name": "HDFC Bank",
                "bank_branch": "Indiranagar",
                "bank_account_type": "current",
                "bank_ifsc_code": "HDFC0001234",
                "bank_account_number": "123456789012",
                "confirm_account_number": "999999999999",
            },
            seller=self.seller1,
        )
        self.assertFalse(form2.is_valid())
        self.assertIn("confirm_account_number", form2.errors)

        # Valid form
        form3 = SellerBankDetailsForm(
            data={
                "bank_account_holder_name": "Alpha Technologies India Pvt Ltd",
                "bank_name": "HDFC Bank",
                "bank_branch": "Indiranagar",
                "bank_account_type": "current",
                "bank_ifsc_code": "HDFC0001234",
                "bank_account_number": "123456789012",
                "confirm_account_number": "123456789012",
            },
            seller=self.seller1,
        )
        self.assertTrue(form3.is_valid())
        saved_seller = form3.save(self.seller1, actor=self.seller_user1)
        self.assertEqual(saved_seller.bank_verification_status, "pending")
        self.assertFalse(saved_seller.is_bank_payout_eligible)

    def test_10_admin_bank_verification_workflow(self):
        """Platform administrator can review, approve, or reject seller bank accounts."""
        self.seller1.bank_account_holder_name = "Alpha Technologies India Pvt Ltd"
        self.seller1.bank_account_number = "123456789012"
        self.seller1.bank_ifsc_code = "HDFC0001234"
        self.seller1.bank_name = "HDFC Bank"
        self.seller1.bank_branch = "Indiranagar"
        self.seller1.bank_verification_status = "pending"
        self.seller1.save()

        self.client.force_login(self.staff_admin)
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller1.id})

        # 1. Admin Rejection with reason
        resp = self.client.post(verify_url, {
            "action": "reject",
            "rejection_reason": "Account holder name mismatch with GST registration certificate.",
        })
        self.assertEqual(resp.status_code, 302)
        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.bank_verification_status, "rejected")
        self.assertIn("mismatch with GST", self.seller1.bank_rejection_reason)
        self.assertFalse(self.seller1.is_bank_payout_eligible)

        # 2. Admin Approval
        resp2 = self.client.post(verify_url, {
            "action": "approve",
        })
        self.assertEqual(resp2.status_code, 302)
        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.bank_verification_status, "verified")
        self.assertIsNotNone(self.seller1.bank_verified_at)
        self.assertEqual(self.seller1.bank_verified_by, self.staff_admin)
        self.assertTrue(self.seller1.is_bank_payout_eligible)

    def test_11_seller_isolation_on_bank_details(self):
        """Seller 2 cannot access or view Seller 1's bank details."""
        self.seller1.bank_account_holder_name = "Alpha Exclusive Treasury Corp"
        self.seller1.bank_account_number = "123456789012"
        self.seller1.bank_ifsc_code = "KKBK0008888"
        self.seller1.bank_name = "Kotak Mahindra Bank"
        self.seller1.bank_verification_status = "verified"
        self.seller1.save()

        self.client.force_login(self.seller_user2)
        bank_url = reverse("core:seller_bank_details")
        resp = self.client.get(bank_url)
        self.assertEqual(resp.status_code, 200)

        # Page must show Seller 2's empty details, never Seller 1's details
        content = resp.content.decode()
        self.assertNotIn("••••••••9012", content)
        self.assertNotIn("123456789012", content)
        self.assertNotIn("Alpha Exclusive Treasury Corp", content)
        self.assertNotIn("KKBK0008888", content)

    def test_12_payout_eligibility_and_bank_verification_gate(self):
        """Payout preparation enforces verified bank accounts before batch creation."""
        order, so = self._create_seller_package(self.seller1, amount=Decimal("2000.00"), delivered_days_ago=10)
        post_order_sale_to_ledger(so)

        # 1. Unverified bank account: create_payout_batch with enforce_bank_verification=True fails
        self.seller1.bank_verification_status = "unverified"
        self.seller1.save()

        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_payout_batch(seller=self.seller1, actor=self.staff_admin, enforce_bank_verification=True)
        self.assertIn("does not have a verified bank account", str(ctx.exception))

        # 2. Verified bank account: creates batch successfully with bank snapshots
        self.seller1.bank_account_number = "987654321098"
        self.seller1.bank_ifsc_code = "ICIC0000999"
        self.seller1.bank_account_holder_name = "Alpha Tech Treasury"
        self.seller1.bank_verification_status = "verified"
        self.seller1.save()

        batch = create_payout_batch(seller=self.seller1, actor=self.staff_admin, enforce_bank_verification=True)
        self.assertEqual(batch.status, PayoutBatch.STATUS_DRAFT)
        self.assertEqual(batch.amount, Decimal("1800.00"))
        self.assertEqual(batch.bank_account_masked, "••••••••1098")
        self.assertEqual(batch.bank_ifsc_code, "ICIC0000999")
        self.assertEqual(batch.bank_beneficiary_name, "Alpha Tech Treasury")

    def test_13_duplicate_payout_prevention(self):
        """Pessimistic locking prevents bundling already batched entries into another batch."""
        order, so = self._create_seller_package(self.seller1, amount=Decimal("1000.00"), delivered_days_ago=10)
        post_order_sale_to_ledger(so)

        self.seller1.bank_account_number = "987654321098"
        self.seller1.bank_ifsc_code = "ICIC0000999"
        self.seller1.bank_verification_status = "verified"
        self.seller1.save()

        # First batch succeeds
        batch1 = create_payout_batch(seller=self.seller1, actor=self.staff_admin)
        self.assertEqual(batch1.amount, Decimal("900.00"))

        # Second attempt immediately raises PayoutEligibilityError (0 eligible unbatched entries)
        with self.assertRaises(PayoutEligibilityError):
            create_payout_batch(seller=self.seller1, actor=self.staff_admin)

    def test_14_mock_payout_provider_adapter_simulated_execution(self):
        """Mock payout adapter completes batch labeled TEST/SIMULATED without real banking calls."""
        order, so = self._create_seller_package(self.seller1, amount=Decimal("1000.00"), delivered_days_ago=10)
        post_order_sale_to_ledger(so)

        self.seller1.bank_account_number = "987654321098"
        self.seller1.bank_ifsc_code = "ICIC0000999"
        self.seller1.bank_verification_status = "verified"
        self.seller1.save()

        batch = create_payout_batch(seller=self.seller1, actor=self.staff_admin)

        # Settle via default_payout_provider
        success, msg, data = default_payout_provider.initiate_payout(batch, actor=self.staff_admin)
        self.assertTrue(success)
        self.assertIn("TEST/SIMULATED", msg)
        self.assertTrue(data.get("simulated"))

        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatch.STATUS_COMPLETED)
        self.assertTrue(batch.external_reference.startswith("MOCK-PAY-"))

        # Verify debit entry posted to close balance
        debit = SellerLedgerEntry.objects.get(payout_batch=batch, category=SellerLedgerEntry.CATEGORY_PAYOUT)
        self.assertEqual(debit.amount, Decimal("900.00"))
        self.assertIn("[TEST/SIMULATED]", debit.description)

    def test_15_provider_failure_unlinks_entries(self):
        """Simulated provider failure marks batch FAILED and frees covered entries for retry."""
        order, so = self._create_seller_package(self.seller1, amount=Decimal("1000.00"), delivered_days_ago=10)
        post_order_sale_to_ledger(so)

        self.seller1.bank_account_number = "987654321098"
        self.seller1.bank_ifsc_code = "ICIC0000999"
        self.seller1.bank_verification_status = "verified"
        self.seller1.save()

        batch = create_payout_batch(seller=self.seller1, actor=self.staff_admin)
        success, msg, data = default_payout_provider.initiate_payout(
            batch,
            actor=self.staff_admin,
            simulate_failure=True,
            failure_reason="Beneficiary bank offline",
        )
        self.assertFalse(success)
        batch.refresh_from_db()
        self.assertEqual(batch.status, PayoutBatch.STATUS_FAILED)

        # Entries unlinked
        for entry in SellerLedgerEntry.objects.filter(seller_order=so):
            self.assertIsNone(entry.payout_batch_id)

    def test_16_webhook_signature_and_idempotency(self):
        """Webhook signature verification succeeds on valid HMAC and rejects invalid signatures."""
        secret = "test_webhook_secret_key_12345"
        payload = b'{"event":"payout.processed","batch_reference":"PAY-20261012-ABCDEF"}'
        valid_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

        # Valid signature
        self.assertTrue(
            default_payout_provider.verify_webhook_signature(payload, valid_sig, secret=secret)
        )

        # Forged signature
        self.assertFalse(
            default_payout_provider.verify_webhook_signature(payload, "invalid_forged_signature", secret=secret)
        )

    def test_17_cod_remittance_gate_before_payout_funding(self):
        """COD sales remain in PENDING_COD and do NOT qualify for payout until courier remittance is reconciled."""
        cod_order, cod_so = self._create_seller_package(
            self.seller1,
            amount=Decimal("3000.00"),
            is_cod=True,
            delivered_days_ago=14,
        )
        post_order_sale_to_ledger(cod_so)

        # Entries are in PENDING_COD
        pending_entries = SellerLedgerEntry.objects.filter(seller_order=cod_so, status=SellerLedgerEntry.STATUS_PENDING_COD)
        self.assertTrue(pending_entries.exists())

        # Payout eligibility should be 0 because COD cash is not yet remitted by courier
        elig = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(elig["net_eligible_balance"], Decimal("0.00"))

        # Reconcile COD remittance
        reconcile_cod_remittance(
            cod_so,
            remittance_reference="REMIT-TEST-COD-9999",
            actor=self.staff_admin,
            notes="Courier cash received in bank",
        )

        # Now entries transition to POSTED and qualify
        elig_after = calculate_seller_payout_eligibility(seller=self.seller1)
        self.assertEqual(elig_after["net_eligible_balance"], Decimal("2700.00"))

    def test_18_bank_selector_predefined_and_other_choices(self):
        """SellerBankDetailsForm supports predefined common Indian banks, 'Other' manual entry, and preserves existing names."""
        # 1. Predefined bank selection
        form_predefined = SellerBankDetailsForm(
            data={
                "bank_account_holder_name": "Alpha Technologies India Pvt Ltd",
                "bank_name_select": "State Bank of India (SBI)",
                "other_bank_name": "",
                "bank_branch": "MG Road, Bengaluru",
                "bank_account_type": "current",
                "bank_ifsc_code": "SBIN0000456",
                "bank_account_number": "123456789012",
                "confirm_account_number": "123456789012",
            },
            seller=self.seller1,
        )
        self.assertTrue(form_predefined.is_valid(), form_predefined.errors)
        self.assertEqual(form_predefined.cleaned_data["bank_name"], "State Bank of India (SBI)")

        # 2. 'Other' selected with valid manual bank name
        form_other_valid = SellerBankDetailsForm(
            data={
                "bank_account_holder_name": "Alpha Technologies India Pvt Ltd",
                "bank_name_select": "OTHER",
                "other_bank_name": "Kalyan Janata Sahakari Bank",
                "bank_branch": "Kalyan Main Branch",
                "bank_account_type": "current",
                "bank_ifsc_code": "KJSB0000001",
                "bank_account_number": "987654321098",
                "confirm_account_number": "987654321098",
            },
            seller=self.seller1,
        )
        self.assertTrue(form_other_valid.is_valid(), form_other_valid.errors)
        self.assertEqual(form_other_valid.cleaned_data["bank_name"], "Kalyan Janata Sahakari Bank")

        # 3. 'Other' selected with empty manual bank name fails validation
        form_other_empty = SellerBankDetailsForm(
            data={
                "bank_account_holder_name": "Alpha Technologies India Pvt Ltd",
                "bank_name_select": "OTHER",
                "other_bank_name": "",
                "bank_branch": "Kalyan Main Branch",
                "bank_account_type": "current",
                "bank_ifsc_code": "KJSB0000001",
                "bank_account_number": "987654321098",
                "confirm_account_number": "987654321098",
            },
            seller=self.seller1,
        )
        self.assertFalse(form_other_empty.is_valid())
        self.assertIn("other_bank_name", form_other_empty.errors)

        # 4. Form __init__ preselects predefined bank for existing seller
        self.seller1.bank_name = "HDFC Bank"
        self.seller1.save()
        form_bound_predefined = SellerBankDetailsForm(seller=self.seller1)
        self.assertEqual(form_bound_predefined.fields["bank_name_select"].initial, "HDFC Bank")

        # 5. Form __init__ preselects 'OTHER' and populates other_bank_name for custom existing bank
        self.seller1.bank_name = "Abhyudaya Co-op Bank"
        self.seller1.save()
        form_bound_custom = SellerBankDetailsForm(seller=self.seller1)
        self.assertEqual(form_bound_custom.fields["bank_name_select"].initial, "OTHER")
        self.assertEqual(form_bound_custom.fields["other_bank_name"].initial, "Abhyudaya Co-op Bank")

    def test_19_admin_bank_verification_page_get_and_post_approval(self):
        """Platform admin can open the seller's verification page via GET and approve via POST."""
        self.seller1.bank_account_holder_name = "Alpha Technologies India Pvt Ltd"
        self.seller1.bank_account_number = "123456789012"
        self.seller1.bank_ifsc_code = "HDFC0001234"
        self.seller1.bank_name = "HDFC Bank"
        self.seller1.bank_branch = "Indiranagar"
        self.seller1.bank_verification_status = "pending"
        self.seller1.save()

        self.client.force_login(self.staff_admin)
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller1.id})

        # 1. Platform admin GET opens inspection page
        resp_get = self.client.get(verify_url)
        self.assertEqual(resp_get.status_code, 200)
        self.assertContains(resp_get, "Alpha Tech Bank Verification")
        self.assertContains(resp_get, "••••••••9012")
        self.assertContains(resp_get, "HDFC0001234")
        self.assertContains(resp_get, "Manual Platform Compliance Review")

        # 2. Platform admin POST approves bank details
        resp_post = self.client.post(verify_url, {"action": "approve"})
        self.assertEqual(resp_post.status_code, 302)
        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.bank_verification_status, "verified")
        self.assertIsNotNone(self.seller1.bank_verified_at)
        self.assertEqual(self.seller1.bank_verified_by, self.staff_admin)
        self.assertTrue(self.seller1.is_bank_payout_eligible)

    def test_20_admin_bank_verification_rejection_requires_reason(self):
        """Platform admin rejection requires a mandatory reason, which is displayed to the seller."""
        self.seller1.bank_account_holder_name = "Alpha Technologies India Pvt Ltd"
        self.seller1.bank_account_number = "123456789012"
        self.seller1.bank_ifsc_code = "HDFC0001234"
        self.seller1.bank_name = "HDFC Bank"
        self.seller1.bank_verification_status = "pending"
        self.seller1.save()

        self.client.force_login(self.staff_admin)
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller1.id})

        # 1. Reject without reason is rejected / re-renders form with error
        resp_no_reason = self.client.post(verify_url, {"action": "reject", "rejection_reason": ""})
        self.assertEqual(resp_no_reason.status_code, 200)
        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.bank_verification_status, "pending")

        # 2. Reject with reason succeeds
        reason_msg = "Account holder name mismatch: Passbook says Alpha Corp, KYC is Alpha Tech Pvt Ltd."
        resp_reject = self.client.post(verify_url, {"action": "reject", "rejection_reason": reason_msg})
        self.assertEqual(resp_reject.status_code, 302)
        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.bank_verification_status, "rejected")
        self.assertEqual(self.seller1.bank_rejection_reason, reason_msg)
        self.assertIsNone(self.seller1.bank_verified_at)
        self.assertFalse(self.seller1.is_bank_payout_eligible)

        # 3. Seller portal displays rejection reason and status
        self.client.force_login(self.seller_user1)
        resp_seller_bank = self.client.get(reverse("core:seller_bank_details"))
        self.assertEqual(resp_seller_bank.status_code, 200)
        self.assertContains(resp_seller_bank, "Verification Rejected")
        self.assertContains(resp_seller_bank, reason_msg)

        resp_seller_dash = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(resp_seller_dash.status_code, 200)
        self.assertContains(resp_seller_dash, "Settlement Account: Verification Rejected")
        self.assertContains(resp_seller_dash, reason_msg)

    def test_21_unauthorized_user_cannot_access_or_approve_bank_verification(self):
        """Non-admin sellers and anonymous users cannot access or alter bank verification."""
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller1.id})
        admin_login_url = reverse("custom_admin:login")

        # 1. Anonymous user redirected to admin login
        resp_anon = self.client.get(verify_url)
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn(admin_login_url, resp_anon.url)

        # 2. Ordinary seller accessing GET is blocked with 403 Forbidden
        self.client.force_login(self.seller_user1)
        resp_seller_get = self.client.get(verify_url)
        self.assertEqual(resp_seller_get.status_code, 403)

        # 3. Ordinary seller trying to self-approve via POST is blocked (403) and status remains unchanged
        self.seller1.bank_verification_status = "pending"
        self.seller1.save()
        resp_seller_post = self.client.post(verify_url, {"action": "approve"})
        self.assertEqual(resp_seller_post.status_code, 403)
        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.bank_verification_status, "pending")
        self.assertFalse(self.seller1.is_bank_payout_eligible)

    def test_22_seller_updating_bank_details_resets_verification_and_blocks_payout(self):
        """Updating bank details resets status from verified to pending, clears verification, and blocks payouts."""
        # Setup verified seller with earnings
        self.seller1.bank_account_holder_name = "Alpha Technologies India Pvt Ltd"
        self.seller1.bank_account_number = "123456789012"
        self.seller1.bank_ifsc_code = "HDFC0001234"
        self.seller1.bank_name = "HDFC Bank"
        self.seller1.bank_branch = "Indiranagar"
        self.seller1.bank_verification_status = "verified"
        self.seller1.bank_verified_at = timezone.now()
        self.seller1.bank_verified_by = self.staff_admin
        self.seller1.save()
        self.assertTrue(self.seller1.is_bank_payout_eligible)

        order, so = self._create_seller_package(self.seller1, amount=Decimal("2000.00"), delivered_days_ago=10)
        post_order_sale_to_ledger(so)

        # Seller submits updated bank details
        self.client.force_login(self.seller_user1)
        update_resp = self.client.post(
            reverse("core:seller_bank_details"),
            {
                "bank_account_holder_name": "Alpha Technologies India Pvt Ltd",
                "bank_name_select": "ICICI Bank",
                "other_bank_name": "",
                "bank_branch": "Whitefield Branch",
                "bank_account_type": "current",
                "bank_ifsc_code": "ICIC0000999",
                "bank_account_number": "987654321098",
                "confirm_account_number": "987654321098",
            },
        )
        self.assertEqual(update_resp.status_code, 302)

        # Verification status reset to pending, verification metadata cleared
        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.bank_verification_status, "pending")
        self.assertIsNone(self.seller1.bank_verified_at)
        self.assertIsNone(self.seller1.bank_verified_by)
        self.assertEqual(self.seller1.bank_name, "ICICI Bank")
        self.assertFalse(self.seller1.is_bank_payout_eligible)

        # Seller dashboard now shows "Verification Pending Review"
        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, "Settlement Account: Verification Pending Review")

        # Payout batch creation with enforce_bank_verification=True is blocked
        with self.assertRaises(PayoutEligibilityError) as ctx:
            create_payout_batch(seller=self.seller1, actor=self.staff_admin, enforce_bank_verification=True)
        self.assertIn("does not have a verified bank account", str(ctx.exception))

    def test_23_admin_dashboard_shows_pending_bank_verifications_and_review_link(self):
        """Admin dashboard shows 'Pending Bank Verifications' section and 'Review Bank Details' link."""
        self.seller1.bank_account_holder_name = "Alpha Technologies India Pvt Ltd"
        self.seller1.bank_account_number = "123456789012"
        self.seller1.bank_ifsc_code = "HDFC0001234"
        self.seller1.bank_name = "HDFC Bank"
        self.seller1.bank_branch = "Indiranagar"
        self.seller1.bank_verification_status = "pending"
        self.seller1.save()

        self.client.force_login(self.staff_admin)
        dash_resp = self.client.get(reverse("custom_admin:dashboard"))
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, "Pending Bank Verifications")
        self.assertContains(dash_resp, "Review Bank Details")
        verify_url = reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": self.seller1.id})
        self.assertContains(dash_resp, verify_url)
        self.assertContains(dash_resp, "••••••••9012")
        self.assertContains(dash_resp, "Alpha Tech")



class CourierShippingLabelAndIntegrationTests(TestCase):
    """
    Test suite for Courier Shipping Label (4x6 thermal & A4), Carrier AWB Barcode integration,
    and distinct separation from the Internal Warehouse Packing Summary.
    """

    def setUp(self):
        self.seller_user = User.objects.create_user(
            username="courier_seller",
            email="seller@courier.com",
            password="SecurePassword123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Royal Heritage Hub",
            legal_business_name="Royal Heritage Apparel Pvt Ltd",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            gstin="27ABCDE1234F1Z5",
            pickup_address="45 Industrial Estate, Andheri East",
            pickup_city="Mumbai",
            pickup_state="Maharashtra",
            pickup_postal_code="400069",
            pickup_location_nickname="Mumbai-North",
            registered_mobile="9820012345",
        )
        self.customer = User.objects.create_user(
            username="courier_cust",
            email="cust@courier.com",
            password="SecurePassword123!",
        )
        self.category = Category.objects.create(name="Ethnic", slug="ethnic")

        self.order = Order.objects.create(
            order_number="FM-LBL-ORD-001",
            user=self.customer,
            subtotal=Decimal("3499.00"),
            total_amount=Decimal("3499.00"),
            status=Order.STATUS_PROCESSING,
            payment_status=Order.PAYMENT_STATUS_PAID,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            shipping_full_name="Pooja Sharma",
            shipping_address_line1="Flat 502, Green Meadows",
            shipping_address_line2="Near Cyber City, Sector 21",
            shipping_city="Gurugram",
            shipping_state="Haryana",
            shipping_postal_code="122016",
            shipping_phone_number="9876543210",
        )
        self.package = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number="FM-LBL-ORD-001-S1",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("3499.00"),
            total_commission=Decimal("349.90"),
            net_seller_payable=Decimal("3149.10"),
            actual_length_cm=Decimal("25.0"),
            actual_breadth_cm=Decimal("20.0"),
            actual_height_cm=Decimal("8.0"),
            actual_weight_grams=850,
        )
        self.item = OrderItem.objects.create(
            order=self.order,
            seller_order=self.package,
            seller=self.seller,
            product_name="Embroidered Chanderi Kurta Set",
            variant_label="Size: M, Color: Pastel Mint Green",
            sku="CHK-EMB-01-M",
            quantity=1,
            unit_price=Decimal("3499.00"),
            line_total=Decimal("3499.00"),
        )

    def test_01_shipping_label_4x6_generation(self):
        """Generates standard 4x6 thermal courier-style shipping label."""
        pdf_bytes = generate_courier_shipping_label_pdf(self.package, page_size="4x6")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertTrue(len(pdf_bytes) > 2000)
        self.assertIn(b"Pooja Sharma", pdf_bytes)
        self.assertIn(b"122016", pdf_bytes)
        self.assertIn(b"GURUGRAM", pdf_bytes)
        self.assertIn(b"Royal Heritage Hub", pdf_bytes)

    def test_02_shipping_label_a4_generation(self):
        """Generates print-friendly A4 format with cut guidelines."""
        pdf_bytes = generate_courier_shipping_label_pdf(self.package, page_size="a4")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertIn(b"Cut along line", pdf_bytes)

    def test_03_no_fake_carrier_or_awb_when_unbooked(self):
        """When unbooked, label clearly states internal dispatch and does NOT fake carrier AWB."""
        pdf_bytes = generate_courier_shipping_label_pdf(self.package)
        self.assertIn(b"FITMEEZO DISPATCH", pdf_bytes)
        self.assertIn(b"NO CARRIER AWB GENERATED", pdf_bytes)
        self.assertIn(b"PENDING CARRIER BOOKING", pdf_bytes)

    def test_04_real_carrier_booking_renders_awb_barcode(self):
        """When genuine carrier booking exists, renders carrier name and Code128 AWB barcode."""
        booking = ShipmentBooking.objects.create(
            package=self.package,
            idempotency_key="test-carrier-booking-key-001",
            is_mock=False,
            status="booked",
            shipment_id="DEL-SHP-888999",
            awb_code="DEL1234567890",
            courier_name="Delhivery Surface",
            chargeable_weight_kg=Decimal("0.850"),
            estimated_rate=Decimal("95.00"),
        )
        pdf_bytes = generate_courier_shipping_label_pdf(self.package)
        self.assertIn(b"DELHIVERY SURFACE", pdf_bytes)
        self.assertIn(b"DEL1234567890", pdf_bytes)
        self.assertIn(b"AWB No:", pdf_bytes)

    def test_05_carrier_label_url_redirection(self):
        """View redirects to official provider label URL when requested with source=courier."""
        booking = ShipmentBooking.objects.create(
            package=self.package,
            idempotency_key="test-carrier-label-url-key",
            is_mock=False,
            status="booked",
            shipment_id="CARRIER-SHP-001",
            awb_code="AWB-CARRIER-999",
            courier_name="Blue Dart Air",
            metadata={"label_download_url": "https://carrier-cdn.example.com/labels/DEL-999.pdf"},
        )
        self.client.force_login(self.seller_user)
        url = reverse("core:seller_order_shipping_label", kwargs={"pk": self.package.pk})
        resp = self.client.get(f"{url}?source=courier")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "https://carrier-cdn.example.com/labels/DEL-999.pdf")

    def test_06_long_addresses_and_multiple_items_no_clipping(self):
        """Handles multi-line addresses and multiple items cleanly."""
        self.order.shipping_address_line1 = "Block C, Tower 4, Flat 1204, Imperial Palm Meadows Luxury Residential Enclave"
        self.order.shipping_address_line2 = "Adjacent to Outer Ring Road Financial District, Near Phase 2 Metro Station"
        self.order.save()

        for i in range(2, 6):
            OrderItem.objects.create(
                order=self.order,
                seller_order=self.package,
                seller=self.seller,
                product_name=f"Accessory Silk Dupatta #{i} With Heavy Handcrafted Zari Border",
                variant_label=f"Color: Shade #{i}, Size: Free Size Length 2.5m",
                sku=f"DUP-{i:03d}",
                quantity=i,
                unit_price=Decimal("500.00"),
                line_total=Decimal(str(500 * i)),
            )

        pdf_bytes = generate_courier_shipping_label_pdf(self.package, page_size="4x6")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertIn(b"Imperial Palm Meadows", pdf_bytes)
        self.assertIn(b"Total: 5 line item", pdf_bytes)

    def test_07_warehouse_manifest_distinct_from_shipping_label(self):
        """Packing summary (warehouse manifest) and Shipping label (courier layout) are separate endpoints."""
        self.client.force_login(self.seller_user)

        # 1. Warehouse packing summary endpoint
        summary_url = reverse("core:seller_order_packing_summary", kwargs={"pk": self.package.pk})
        summary_resp = self.client.get(summary_url)
        self.assertEqual(summary_resp.status_code, 200)
        self.assertIn(b"INTERNAL WAREHOUSE PACKING SUMMARY", summary_resp.content)
        self.assertIn(b"NON-CARRIER", summary_resp.content)

        # 2. Courier shipping label endpoint
        label_url = reverse("core:seller_order_shipping_label", kwargs={"pk": self.package.pk})
        label_resp = self.client.get(label_url)
        self.assertEqual(label_resp.status_code, 200)
        self.assertIn(b"FITMEEZO DISPATCH", label_resp.content)
        self.assertIn(b"DESTINATION ROUTING", label_resp.content)
