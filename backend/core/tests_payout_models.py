"""
Tests for FitMeezo PayoutBatch manual bank transfer fields and
SellerLedgerEntry manual hold release audit fields (Prompt 3).
"""

from datetime import date, datetime
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    Order,
    OrderItem,
    PayoutBatch,
    Product,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class PayoutModelSchemaTestCase(TestCase):
    """
    Validates:
    1. Existing payout batches remain readable with blank manual-transfer fields.
    2. Multiple blank manual_utr_reference values are permitted.
    3. Duplicate non-empty real UTRs are rejected by the database constraint.
    4. Mock references do not conflict with real bank UTRs.
    5. Hold-release audit fields can accurately represent an authorized release.
    6. Existing commission and payout model behavior remains unchanged.
    """

    def setUp(self):
        # Admin / Staff User
        self.admin_user = User.objects.create_user(
            username="finance_admin",
            email="finance@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
        )

        # Customer User
        self.customer = User.objects.create_user(
            username="buyer_payout",
            email="buyer@fitmeezo.com",
            password="BuyerPassword123!",
        )

        # Seller User & Profile
        self.seller_user = User.objects.create_user(
            username="merchant_user",
            email="merchant@fitmeezo.com",
            password="MerchantPassword123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Loom & Needle",
            legal_business_name="Loom & Needle Apparels LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="support@loomneedle.com",
            support_phone="9876543210",
            registered_address="12 Textile Park, Surat, Gujarat",
            city="Surat",
            state="Gujarat",
            postal_code="395002",
            pickup_address="12 Textile Park, Surat, Gujarat",
            pickup_city="Surat",
            pickup_state="Gujarat",
            pickup_postal_code="395002",
            gstin="24ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            bank_account_holder_name="Loom & Needle Apparels LLP",
            bank_account_number="987654321098",
            bank_ifsc_code="HDFC0001234",
            bank_name="HDFC Bank",
            bank_branch="Surat Ring Road",
            bank_verification_status=Seller.BANK_STATUS_VERIFIED,
            status=Seller.STATUS_APPROVED,
            commission_rate=Decimal("8.50"),
        )

    def test_existing_payout_batches_readable_with_blank_manual_transfer_fields(self):
        """
        Batches created without manual fields default safely to mock channel,
        empty manual_utr_reference, and None manual_transfer_date.
        """
        batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("15000.00"),
            payout_cycle="2026-10-C1",
            status=PayoutBatch.STATUS_DRAFT,
            external_reference="MOCK-PAY-TEST01",
        )
        batch.refresh_from_db()

        self.assertEqual(batch.payout_channel, PayoutBatch.PAYOUT_CHANNEL_MOCK)
        self.assertEqual(batch.manual_utr_reference, "")
        self.assertIsNone(batch.manual_transfer_date)
        self.assertEqual(batch.external_reference, "MOCK-PAY-TEST01")

    def test_multiple_blank_utr_values_allowed(self):
        """
        The conditional unique constraint permits any number of batches
        with blank manual_utr_reference="".
        """
        batch1 = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("5000.00"),
            payout_cycle="2026-10-C1",
            status=PayoutBatch.STATUS_DRAFT,
            manual_utr_reference="",
        )
        batch2 = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("7500.00"),
            payout_cycle="2026-10-C2",
            status=PayoutBatch.STATUS_DRAFT,
            manual_utr_reference="",
        )
        self.assertIsNotNone(batch1.pk)
        self.assertIsNotNone(batch2.pk)
        self.assertEqual(batch1.manual_utr_reference, "")
        self.assertEqual(batch2.manual_utr_reference, "")

    def test_duplicate_non_empty_real_utrs_rejected_by_database_constraint(self):
        """
        If two distinct batches attempt to record the same non-empty real UTR,
        the database-level UniqueConstraint raises IntegrityError.
        """
        shared_utr = "HDFC2026101600987654"

        batch1 = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("12000.00"),
            payout_cycle="2026-10-C1",
            payout_channel=PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
            manual_utr_reference=shared_utr,
            manual_transfer_date=date(2026, 10, 16),
            status=PayoutBatch.STATUS_COMPLETED,
        )
        self.assertIsNotNone(batch1.pk)

        # Attempting duplicate non-empty UTR must raise IntegrityError
        with self.assertRaises(IntegrityError):
            PayoutBatch.objects.create(
                batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
                seller=self.seller,
                amount=Decimal("8000.00"),
                payout_cycle="2026-10-C2",
                payout_channel=PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
                manual_utr_reference=shared_utr,
                manual_transfer_date=date(2026, 10, 16),
                status=PayoutBatch.STATUS_COMPLETED,
            )

    def test_mock_references_do_not_conflict_with_real_bank_utrs(self):
        """
        A batch using a mock external_reference (e.g. MOCK-PAY-XXXX) does not
        interfere with or consume manual_utr_reference.
        """
        mock_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("4500.00"),
            payout_cycle="2026-10-C1",
            external_reference="MOCK-PAY-ABC123XYZ",
            manual_utr_reference="",
            payout_channel=PayoutBatch.PAYOUT_CHANNEL_MOCK,
        )

        real_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("4500.00"),
            payout_cycle="2026-10-C2",
            external_reference="NEFT-BANK-REF",
            manual_utr_reference="SBIN2026101677665544",
            manual_transfer_date=date(2026, 10, 16),
            payout_channel=PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK,
        )

        self.assertEqual(mock_batch.manual_utr_reference, "")
        self.assertEqual(real_batch.manual_utr_reference, "SBIN2026101677665544")
        self.assertEqual(mock_batch.payout_channel, "mock_bank_transfer")
        self.assertEqual(real_batch.payout_channel, "manual_bank_transfer")

    def test_hold_release_audit_fields_on_seller_ledger_entry(self):
        """
        SellerLedgerEntry supports manual hold release audit trail:
        is_hold_released, hold_released_at, hold_released_by, hold_release_reason.
        Default is False / None / blank.
        """
        entry = SellerLedgerEntry.objects.create(
            seller=self.seller,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("2500.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            source_type="seller_order",
            source_reference="SO-TEST-001",
            idempotency_key=f"TEST-ENTRY-{uuid.uuid4().hex[:8]}",
            description="Gross sale for package",
        )
        entry.refresh_from_db()

        # Defaults
        self.assertFalse(entry.is_hold_released)
        self.assertIsNone(entry.hold_released_at)
        self.assertIsNone(entry.hold_released_by)
        self.assertEqual(entry.hold_release_reason, "")

        # Record manual release
        now = timezone.now()
        entry.is_hold_released = True
        entry.hold_released_at = now
        entry.hold_released_by = self.admin_user
        entry.hold_release_reason = "Customer confirmed satisfaction; early clearance approved."
        entry.save(update_fields=["is_hold_released", "hold_released_at", "hold_released_by", "hold_release_reason"])

        entry.refresh_from_db()
        self.assertTrue(entry.is_hold_released)
        self.assertEqual(entry.hold_released_by, self.admin_user)
        self.assertEqual(entry.hold_release_reason, "Customer confirmed satisfaction; early clearance approved.")
        self.assertIsNotNone(entry.hold_released_at)

        # Financial values remain completely untouched
        self.assertEqual(entry.amount, Decimal("2500.00"))
        self.assertEqual(entry.entry_type, SellerLedgerEntry.TYPE_CREDIT)

    def test_existing_commission_and_payout_model_behavior_unchanged(self):
        """
        Verifies that Seller.commission_rate and OrderItem snapshotting
        continue to function identically after schema update.
        """
        # Seller commission rate
        self.assertEqual(self.seller.commission_rate, Decimal("8.50"))

        # Order and OrderItem
        order = Order.objects.create(
            order_number=f"FM-{timezone.now().strftime('%Y%m%d')}-TST01",
            user=self.customer,
            shipping_full_name="Buyer Test",
            shipping_phone_number="9123456780",
            shipping_address_line1="123 Market Rd",
            shipping_city="Ahmedabad",
            shipping_state="Gujarat",
            shipping_postal_code="380001",
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
        )
        seller_order = SellerOrder.objects.create(
            order=order,
            seller=self.seller,
            sub_order_number=f"{order.order_number}-S{self.seller.id}",
            subtotal=Decimal("1000.00"),
            total_commission=Decimal("85.00"),
            net_seller_payable=Decimal("915.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            seller_order=seller_order,
            product_name="Cotton Shirt",
            unit_price=Decimal("1000.00"),
            quantity=1,
            line_total=Decimal("1000.00"),
            seller=self.seller,
            commission_rate=self.seller.commission_rate,
            commission_amount=Decimal("85.00"),
        )

        item.refresh_from_db()
        self.assertEqual(item.commission_rate, Decimal("8.50"))
        self.assertEqual(item.commission_amount, Decimal("85.00"))
        self.assertEqual(seller_order.net_seller_payable, Decimal("915.00"))
