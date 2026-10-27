"""
Security Unit Tests for:
1. Bank Account Number Authenticated Encryption at Rest (Fernet AES-128-CBC + HMAC-SHA256).
2. Tampered Ciphertext & Missing/Invalid Encryption Key Validation.
3. Maker-Checker Dual-Authorization for PayoutBatch Approval, Disbursement, and Manual Bank Transfers.
4. Masked Bank Account Number Display.
5. Reversal RBAC Authorization.
"""

from decimal import Decimal
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.admin_permissions import (
    ROLE_FINANCE_MANAGER,
    ROLE_SUPER_ADMIN,
    ROLE_SUPPORT_MANAGER,
    ensure_admin_groups,
    set_user_role,
)
from core.encryption import (
    BankDecryptionError,
    BankEncryptionConfigurationError,
    encrypt_bank_account_number,
    decrypt_bank_account_number,
    get_bank_fernet,
)
from core.models import (
    Order,
    PayoutBatch,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from core.ledger_services import (
    PayoutAuthorizationError,
    LedgerServiceError,
    approve_payout_batch,
    create_payout_batch,
    process_mock_payout_batch,
    record_manual_bank_transfer,
    reverse_payout_batch,
)

User = get_user_model()

TEST_ENCRYPTION_KEY = "Rml0bWVlem9UZXN0RW5jcnlwdGlvbktleTMyQnl0ZXM="


@override_settings(
    SECURE_SSL_REDIRECT=False,
    FITMEEZO_BANK_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
    FITMEEZO_BANK_ENCRYPTION_KEY_ROTATION=[],
)
class BankAccountEncryptionSecurityTestCase(TestCase):
    def setUp(self):
        self.seller_user = User.objects.create_user(
            username="seller_crypto_test",
            email="seller_crypto@example.com",
            password="TestPassword@123",
        )
        self.plain_account = "9876543210123456"
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Crypto Secure Store",
            legal_business_name="Crypto Secure Store Pvt Ltd",
            registered_mobile="9876543210",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_verification_status=Seller.BANK_STATUS_VERIFIED,
            bank_account_number=self.plain_account,
            bank_ifsc_code="HDFC0001234",
            bank_account_holder_name="Crypto Secure Store",
            bank_name="HDFC Bank",
            bank_verified_at=timezone.now(),
        )

    def test_bank_account_encrypted_at_rest_in_raw_database(self):
        """Verify raw database storage contains authenticated Fernet ciphertext, not plaintext."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT bank_account_number FROM core_seller WHERE id = %s", [self.seller.id])
            row = cursor.fetchone()

        raw_db_value = row[0]
        # Raw value in DB must start with Fernet magic prefix 'gAAAAA'
        self.assertTrue(
            raw_db_value.startswith("gAAAAA"),
            f"Database value is not encrypted! Value: {raw_db_value}",
        )
        # Plaintext must not exist anywhere in the raw database column
        self.assertNotIn(self.plain_account, raw_db_value)

        # ORM model access transparently decrypts
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_account_number, self.plain_account)

    def test_masked_account_number_display(self):
        """Masked account display must preserve the last 4 digits while masking the prefix."""
        masked = self.seller.get_masked_account_number()
        self.assertTrue(masked.endswith("3456"))
        self.assertNotIn("987654321012", masked)
        self.assertIn("••••", masked)

        # Edge cases
        self.seller.bank_account_number = ""
        self.assertEqual(self.seller.get_masked_account_number(), "Not Provided")

        self.seller.bank_account_number = "12345678"
        self.assertEqual(self.seller.get_masked_account_number(), "••••5678")

    def test_tampered_ciphertext_raises_bank_decryption_error(self):
        """Altered/corrupted ciphertext in database must raise BankDecryptionError, never silent corruption."""
        # Intentionally tamper with ciphertext in raw DB
        tampered_ciphertext = "gAAAAABfFakeTamperedCiphertextPaddingThatFailsHMACAuthenticationCheck1234567890="
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core_seller SET bank_account_number = %s WHERE id = %s",
                [tampered_ciphertext, self.seller.id],
            )

        with self.assertRaises(BankDecryptionError):
            corrupted_seller = Seller.objects.get(id=self.seller.id)
            _ = corrupted_seller.bank_account_number

    @override_settings(FITMEEZO_BANK_ENCRYPTION_KEY="", FITMEEZO_BANK_ENCRYPTION_KEY_ROTATION=[])
    def test_missing_encryption_key_raises_configuration_error_without_fallback(self):
        """When encryption key is missing or blank, system must raise BankEncryptionConfigurationError."""
        with self.assertRaises(BankEncryptionConfigurationError):
            encrypt_bank_account_number("123456789012")

    def test_key_rotation_multi_fernet_support(self):
        """New key can decrypt values encrypted with an older secondary key in rotation."""
        from cryptography.fernet import Fernet
        old_key = Fernet.generate_key().decode("ascii")
        new_key = Fernet.generate_key().decode("ascii")

        # Encrypt with old key
        old_cipher = Fernet(old_key.encode("ascii")).encrypt(b"98765432101234").decode("ascii")

        # Configure new_key as primary, old_key in rotation
        with override_settings(
            FITMEEZO_BANK_ENCRYPTION_KEY=new_key,
            FITMEEZO_BANK_ENCRYPTION_KEY_ROTATION=[old_key],
        ):
            decrypted = decrypt_bank_account_number(old_cipher)
            self.assertEqual(decrypted, "98765432101234")

    def test_legacy_plaintext_read_and_upgrade_on_save(self):
        """
        Pre-migration verification: Existing database records stored in plaintext can be read
        seamlessly by the ORM, retain masked display and bank verification status, and are upgraded
        to authenticated ciphertext upon save.
        """
        legacy_plain = "55443322110099"
        # Directly inject unencrypted plaintext into raw DB (simulating existing unmigrated production row)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core_seller SET bank_account_number = %s WHERE id = %s",
                [legacy_plain, self.seller.id],
            )

        # 1. ORM reads legacy plaintext without error
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_account_number, legacy_plain)

        # 2. Masked display preserves last 4 digits
        masked = self.seller.get_masked_account_number()
        self.assertTrue(masked.endswith("0099"))
        self.assertIn("••••", masked)

        # 3. Bank verification status and payout eligibility remain intact
        self.assertEqual(self.seller.bank_verification_status, Seller.BANK_STATUS_VERIFIED)
        self.assertTrue(self.seller.is_bank_payout_eligible)

        # 4. Saving the seller upgrades the raw database value to authenticated ciphertext
        self.seller.save()

        with connection.cursor() as cursor:
            cursor.execute("SELECT bank_account_number FROM core_seller WHERE id = %s", [self.seller.id])
            raw_db_value = cursor.fetchone()[0]

        self.assertTrue(raw_db_value.startswith("gAAAAA"), f"Did not encrypt on save: {raw_db_value}")
        self.assertNotIn(legacy_plain, raw_db_value)

        # 5. ORM continues to decrypt seamlessly after encryption upgrade
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.bank_account_number, legacy_plain)

    @override_settings(FITMEEZO_BANK_ENCRYPTION_KEY="", FITMEEZO_BANK_ENCRYPTION_KEY_ROTATION=[])
    def test_missing_key_during_legacy_read_raises_configuration_error(self):
        """Even for legacy unmigrated plaintext, attempting to access bank accounts without an encryption key raises BankEncryptionConfigurationError."""
        with self.assertRaises(BankEncryptionConfigurationError):
            decrypt_bank_account_number("55443322110099")


@override_settings(
    SECURE_SSL_REDIRECT=False,
    FITMEEZO_BANK_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
    FITMEEZO_BANK_ENCRYPTION_KEY_ROTATION=[],
)
class MakerCheckerAuthorizationTestCase(TestCase):
    def setUp(self):
        ensure_admin_groups()

        # Maker: Finance Manager who prepares batches
        self.maker_admin = User.objects.create_user(
            username="maker_finance",
            email="maker@example.com",
            password="MakerPassword@123",
            is_staff=True,
        )
        set_user_role(self.maker_admin, ROLE_FINANCE_MANAGER)

        # Checker: Different Finance Manager who audits & approves
        self.checker_admin = User.objects.create_user(
            username="checker_finance",
            email="checker@example.com",
            password="CheckerPassword@123",
            is_staff=True,
        )
        set_user_role(self.checker_admin, ROLE_FINANCE_MANAGER)

        # Super Admin
        self.super_admin = User.objects.create_superuser(
            username="super_checker",
            email="super@example.com",
            password="SuperPassword@123",
        )
        set_user_role(self.super_admin, ROLE_SUPER_ADMIN)

        # Ordinary Staff (not Finance Manager or Super Admin)
        self.regular_staff = User.objects.create_user(
            username="regular_staff",
            email="staff@example.com",
            password="StaffPassword@123",
            is_staff=True,
        )
        set_user_role(self.regular_staff, ROLE_SUPPORT_MANAGER)

        # Seller
        self.seller_user = User.objects.create_user(
            username="seller_maker_checker",
            email="seller_mc@example.com",
            password="SellerPassword@123",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Maker Checker Merchant",
            legal_business_name="Maker Checker Merchant Pvt Ltd",
            registered_mobile="9876543211",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_verification_status=Seller.BANK_STATUS_VERIFIED,
            bank_account_number="11223344556677",
            bank_ifsc_code="SBIN0001234",
            bank_account_holder_name="Maker Checker Merchant",
            bank_name="State Bank of India",
            bank_verified_at=timezone.now(),
        )

        # Eligible Ledger Entry
        self.entry = SellerLedgerEntry.objects.create(
            seller=self.seller,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("5000.00"),
            status=SellerLedgerEntry.STATUS_POSTED,
            description="Delivered sale payout eligible",
            idempotency_key=f"SALE-{uuid.uuid4().hex[:12]}",
        )

        # Create batch prepared by Maker
        self.batch = create_payout_batch(
            seller=self.seller,
            actor=self.maker_admin,
            notes="Prepared for weekly disbursement",
        )

        self.client = Client()

    def test_payout_batch_creator_persisted(self):
        """Verify PayoutBatch persistently records created_by actor."""
        self.assertEqual(self.batch.created_by, self.maker_admin)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.created_by_id, self.maker_admin.id)

    def test_creator_cannot_approve_own_batch_service_level(self):
        """Batch creator attempting to approve own batch via service layer must raise PayoutAuthorizationError."""
        with self.assertRaises(PayoutAuthorizationError) as ctx:
            approve_payout_batch(payout_batch=self.batch, actor=self.maker_admin)
        self.assertIn("Maker-Checker", str(ctx.exception))

        # Batch status must remain unapproved
        self.batch.refresh_from_db()
        self.assertIsNone(self.batch.approved_at)
        self.assertIsNone(self.batch.approved_by)

    def test_creator_cannot_approve_own_batch_admin_view_level(self):
        """Batch creator attempting to approve via HTTP view must be rejected with error message."""
        self.client.login(username="maker_finance", password="MakerPassword@123")
        url = reverse("custom_admin:payout_approve", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, follow=True)

        self.assertContains(response, "Maker-Checker Authorization Violation")
        self.batch.refresh_from_db()
        self.assertIsNone(self.batch.approved_at)

    def test_different_authorized_admin_can_approve(self):
        """Different authorized Finance Manager (Checker) can approve the batch."""
        success, msg, approved_batch = approve_payout_batch(payout_batch=self.batch, actor=self.checker_admin)
        self.assertTrue(success)
        self.batch.refresh_from_db()
        self.assertIsNotNone(self.batch.approved_at)
        self.assertEqual(self.batch.approved_by, self.checker_admin)

    def test_super_admin_can_approve_if_different_person(self):
        """Super Admin can approve as Checker if not the Maker."""
        success, msg, approved_batch = approve_payout_batch(payout_batch=self.batch, actor=self.super_admin)
        self.assertTrue(success)
        self.batch.refresh_from_db()
        self.assertIsNotNone(self.batch.approved_at)
        self.assertEqual(self.batch.approved_by, self.super_admin)

    def test_creator_cannot_disburse_own_batch(self):
        """Batch creator cannot execute disbursement settlement even after approval."""
        # Approved by checker
        approve_payout_batch(payout_batch=self.batch, actor=self.checker_admin)

        # Maker tries to disburse
        with self.assertRaises(PayoutAuthorizationError) as ctx:
            process_mock_payout_batch(payout_batch=self.batch, actor=self.maker_admin)
        self.assertIn("Maker-Checker", str(ctx.exception))

        # View-level rejection for maker
        self.client.login(username="maker_finance", password="MakerPassword@123")
        url = reverse("custom_admin:payout_process", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, follow=True)
        self.assertContains(response, "Maker-Checker Authorization Violation")

    def test_different_authorized_admin_can_disburse(self):
        """Checker can execute disbursement settlement."""
        approve_payout_batch(payout_batch=self.batch, actor=self.checker_admin)
        success, msg, disbursed_batch = process_mock_payout_batch(payout_batch=self.batch, actor=self.checker_admin)
        self.assertTrue(success)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, PayoutBatch.STATUS_COMPLETED)
        self.assertEqual(self.batch.processed_by, self.checker_admin)

    def test_creator_cannot_record_manual_bank_transfer(self):
        """Batch creator cannot record manual bank transfer for own batch."""
        approve_payout_batch(payout_batch=self.batch, actor=self.checker_admin)

        # Service-level check
        with self.assertRaises(PayoutAuthorizationError) as ctx:
            record_manual_bank_transfer(
                payout_batch=self.batch,
                actor=self.maker_admin,
                utr_reference="CMS1234567890123456",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("Maker-Checker", str(ctx.exception))

        # View-level check
        self.client.login(username="maker_finance", password="MakerPassword@123")
        url = reverse("custom_admin:payout_record_manual_transfer", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, {
            "manual_utr_reference": "CMS1234567890123456",
            "manual_transfer_date": timezone.now().date().isoformat(),
            "confirmation": "on",
        }, follow=True)
        self.assertContains(response, "Maker-Checker Authorization Violation")

    def test_different_authorized_admin_can_record_manual_bank_transfer(self):
        """Checker (different authorized admin) can record external manual bank transfer successfully."""
        approve_payout_batch(payout_batch=self.batch, actor=self.checker_admin)

        self.client.login(username="checker_finance", password="CheckerPassword@123")
        url = reverse("custom_admin:payout_record_manual_transfer", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, {
            "manual_utr_reference": "CMS9988776655443322",
            "manual_transfer_date": timezone.now().date().isoformat(),
            "confirmation": "on",
            "notes": "Verified via corporate banking portal",
        }, follow=True)

        self.assertContains(response, "Manual bank transfer recorded successfully")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, PayoutBatch.STATUS_COMPLETED)
        self.assertEqual(self.batch.manual_utr_reference, "CMS9988776655443322")
        self.assertEqual(self.batch.processed_by, self.checker_admin)

    def test_maker_checker_template_ui_context(self):
        """Verify UI template dynamically renders Maker-Checker restrictions for creator and enables them for checker."""
        detail_url = reverse("custom_admin:payout_detail", kwargs={"batch_reference": self.batch.batch_reference})

        # 1. Creator views page: sees Maker-Checker restriction alert and disabled buttons
        self.client.login(username="maker_finance", password="MakerPassword@123")
        resp_maker = self.client.get(detail_url)
        self.assertContains(resp_maker, "Maker-Checker Restriction")
        self.assertContains(resp_maker, "Approval Restricted (Maker)")
        self.assertContains(resp_maker, "disabled")

        # 2. Checker views page: sees active Approve button and does NOT see maker restriction alert
        self.client.login(username="checker_finance", password="CheckerPassword@123")
        resp_checker = self.client.get(detail_url)
        self.assertContains(resp_checker, "Approve for Settlement")
        self.assertNotContains(resp_checker, "Approval Restricted (Maker)")

    def test_reversal_rbac_authorization(self):
        """Only authorized Finance Manager or Super Admin can reverse payout batches."""
        # Complete batch first
        approve_payout_batch(payout_batch=self.batch, actor=self.checker_admin)
        process_mock_payout_batch(payout_batch=self.batch, actor=self.checker_admin)

        # Regular staff cannot reverse
        with self.assertRaises(PayoutAuthorizationError):
            reverse_payout_batch(
                payout_batch=self.batch,
                actor=self.regular_staff,
                reason="Unauthorized attempt",
            )

        # Checker (Finance Manager) can reverse with auditable reason
        success, msg, reversed_batch = reverse_payout_batch(
            payout_batch=self.batch,
            actor=self.checker_admin,
            reason="Audited cancellation of payment",
        )
        self.assertTrue(success)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, PayoutBatch.STATUS_REVERSED)
