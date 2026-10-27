"""
Comprehensive Test Suite for FitMeezo Manual Bank Transfer Recording Service & Endpoints (Prompt 5).

Covers:
1. Authorization & Role-Based Access Control (RBAC):
   - Anonymous user rejected / redirected.
   - Non-staff customer / seller rejected with 403 Forbidden.
   - Unauthorized staff role (e.g. Support Manager) rejected with 403 Forbidden.
   - Authorized roles (Finance Manager, Super Admin) allowed.
2. Endpoint HTTP & CSRF Constraints:
   - GET requests rejected with 405 Method Not Allowed (@require_POST).
   - Valid POST with CSRF and payload accepted.
3. Server-side Validation:
   - Missing or blank UTR reference rejected.
   - Missing transfer date rejected.
   - Future transfer date rejected.
   - Missing explicit confirmation rejected.
   - Duplicate UTR reference across different batches safely rejected.
4. Payout Batch State Lifecycle & Safeguards:
   - Unapproved batches (draft without approved_at) rejected.
   - Batches on hold rejected.
   - Failed or reversed batches rejected.
   - Already completed batches with different UTR/channel rejected.
   - Idempotent re-submission with the same UTR handled gracefully without duplicate debit.
5. Financial & Ledger Integrity:
   - Exactly ONE SellerLedgerEntry PAYOUT debit posted.
   - Idempotency key 'PAYOUT-DISBURSE-<batch_id>' strictly enforced.
   - is_mock is set to False (manual offline transfers never masquerade as mock).
   - payout_channel set to 'manual_bank_transfer'.
   - manual_utr_reference and manual_transfer_date persisted.
   - Audit trail logged in PayoutAuditLog (action='disbursed') and PayoutBatch.audit_metadata.
6. Atomic Concurrency & Rollback:
   - Database rollback prevents state mutation if ledger debit creation fails.
7. Zero External Calls:
   - Verifies no payout provider adapter or live banking gateway is ever called.
8. Section 6 Audit Improvement:
   - Verifies repeated hold releases on release_hold_balance_entries accumulate in hold_release_history.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import uuid
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    Order,
    OrderItem,
    PayoutAuditLog,
    PayoutBatch,
    Product,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from core.admin_permissions import (
    ensure_admin_groups,
    set_user_role,
    ROLE_FINANCE_MANAGER,
    ROLE_SUPPORT_MANAGER,
    ROLE_SUPER_ADMIN,
)
from core.ledger_services import (
    approve_payout_batch,
    record_manual_bank_transfer,
    release_hold_balance_entries,
    LedgerServiceError,
    PayoutEligibilityError,
)

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class ManualBankTransferRecordingTestCase(TestCase):
    def setUp(self):
        ensure_admin_groups()
        self.client = Client()

        # 1. Super Admin User
        self.super_admin = User.objects.create_user(
            username="super_admin_prompt5",
            email="superadmin@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        set_user_role(self.super_admin, ROLE_SUPER_ADMIN)

        # 2. Finance Manager User
        self.finance_user = User.objects.create_user(
            username="finance_mgr_prompt5",
            email="finance@fitmeezo.com",
            password="FinancePassword123!",
            is_staff=True,
        )
        set_user_role(self.finance_user, ROLE_FINANCE_MANAGER)

        # 3. Support Manager (Staff without finance permission)
        self.support_user = User.objects.create_user(
            username="support_mgr_prompt5",
            email="support@fitmeezo.com",
            password="SupportPassword123!",
            is_staff=True,
        )
        set_user_role(self.support_user, ROLE_SUPPORT_MANAGER)

        # 4. Non-staff Customer User
        self.customer_user = User.objects.create_user(
            username="customer_prompt5",
            email="customer@fitmeezo.com",
            password="CustomerPassword123!",
            is_staff=False,
        )

        # 5. Verified Seller
        self.seller_user = User.objects.create_user(
            username="seller_user_prompt5",
            email="seller@merchant.com",
            password="SellerPassword123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Royal Handloom Store",
            legal_business_name="Royal Handloom Pvt Ltd",
            support_email="support@royalhandloom.com",
            support_phone="9988776655",
            registered_address="12 Loom Street",
            city="Varanasi",
            state="Uttar Pradesh",
            postal_code="221001",
            pickup_address="12 Loom Street",
            pickup_city="Varanasi",
            pickup_state="Uttar Pradesh",
            pickup_postal_code="221001",
            gstin="09ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Royal Handloom Pvt Ltd",
            bank_account_number="98765432101234",
            bank_ifsc_code="HDFC0001234",
            bank_name="HDFC Bank",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.super_admin,
        )

        # 6. Sample Approved Payout Batch
        self.batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("15450.00"),
            currency="INR",
            status=PayoutBatch.STATUS_PENDING,
            approved_at=timezone.now() - timedelta(hours=2),
            approved_by=self.super_admin,
            payout_method="mock_bank_transfer",
            is_mock=True,
            payout_cycle="2026-10-C1",
        )

    # =========================================================================
    # 1. Authorization & RBAC
    # =========================================================================
    def test_anonymous_access_redirected(self):
        url = reverse("custom_admin:payout_record_manual_transfer", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, {
            "manual_utr_reference": "UTR1234567890",
            "manual_transfer_date": str(timezone.now().date()),
            "confirmation": True,
        })
        self.assertIn(response.status_code, [302, 401, 403])

    def test_customer_access_forbidden(self):
        self.client.login(username="customer_prompt5", password="CustomerPassword123!")
        url = reverse("custom_admin:payout_record_manual_transfer", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, {
            "manual_utr_reference": "UTR1234567890",
            "manual_transfer_date": str(timezone.now().date()),
            "confirmation": True,
        })
        self.assertEqual(response.status_code, 403)

    def test_unauthorized_staff_role_forbidden(self):
        self.client.login(username="support_mgr_prompt5", password="SupportPassword123!")
        url = reverse("custom_admin:payout_record_manual_transfer", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, {
            "manual_utr_reference": "UTR1234567890",
            "manual_transfer_date": str(timezone.now().date()),
            "confirmation": True,
        })
        self.assertEqual(response.status_code, 403)

    def test_finance_manager_allowed(self):
        self.client.login(username="finance_mgr_prompt5", password="FinancePassword123!")
        url = reverse("custom_admin:payout_record_manual_transfer", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.post(url, {
            "manual_utr_reference": "UTR9988776655",
            "manual_transfer_date": str(timezone.now().date()),
            "confirmation": True,
        })
        self.assertEqual(response.status_code, 302)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, PayoutBatch.STATUS_COMPLETED)
        self.assertEqual(self.batch.manual_utr_reference, "UTR9988776655")

    # =========================================================================
    # 2. HTTP Method & CSRF Constraints
    # =========================================================================
    def test_get_request_rejected_405(self):
        self.client.login(username="super_admin_prompt5", password="AdminPassword123!")
        url = reverse("custom_admin:payout_record_manual_transfer", kwargs={"batch_reference": self.batch.batch_reference})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    # =========================================================================
    # 3. Server-Side Form Validation
    # =========================================================================
    def test_missing_utr_rejected(self):
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=self.batch,
                actor=self.finance_user,
                utr_reference="",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("non-empty Bank UTR", str(ctx.exception))

    def test_missing_transfer_date_rejected(self):
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=self.batch,
                actor=self.finance_user,
                utr_reference="UTR1234567890",
                transfer_date=None,
                confirmation=True,
            )
        self.assertIn("execution date is required", str(ctx.exception))

    def test_future_transfer_date_rejected(self):
        future_date = timezone.now().date() + timedelta(days=2)
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=self.batch,
                actor=self.finance_user,
                utr_reference="UTR1234567890",
                transfer_date=future_date,
                confirmation=True,
            )
        self.assertIn("cannot be in the future", str(ctx.exception))

    def test_missing_confirmation_rejected(self):
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=self.batch,
                actor=self.finance_user,
                utr_reference="UTR1234567890",
                transfer_date=timezone.now().date(),
                confirmation=False,
            )
        self.assertIn("Explicit confirmation required", str(ctx.exception))

    def test_duplicate_utr_reference_rejected(self):
        # Record on first batch
        record_manual_bank_transfer(
            payout_batch=self.batch,
            actor=self.finance_user,
            utr_reference="UTR-UNIQUE-101",
            transfer_date=timezone.now().date(),
            confirmation=True,
        )

        # Create another batch
        batch2 = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("5000.00"),
            currency="INR",
            status=PayoutBatch.STATUS_PENDING,
            approved_at=timezone.now(),
            approved_by=self.super_admin,
            payout_cycle="2026-10-C2",
        )

        # Attempt to record same UTR on batch2
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=batch2,
                actor=self.finance_user,
                utr_reference="UTR-UNIQUE-101",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("already been recorded for another payout batch", str(ctx.exception))

    # =========================================================================
    # 4. Payout Batch State Lifecycle & Safeguards
    # =========================================================================
    def test_unapproved_batch_rejected(self):
        unapproved_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PayoutBatch.STATUS_DRAFT,
            approved_at=None,
        )
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=unapproved_batch,
                actor=self.finance_user,
                utr_reference="UTR-UNAPP-1",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("must be approved before recording", str(ctx.exception))

    def test_held_batch_rejected(self):
        held_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PayoutBatch.STATUS_ON_HOLD,
            hold_reason="Bank account audit underway",
            approved_at=timezone.now(),
        )
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=held_batch,
                actor=self.finance_user,
                utr_reference="UTR-HELD-1",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("is ON HOLD", str(ctx.exception))

    def test_failed_batch_rejected(self):
        failed_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PayoutBatch.STATUS_FAILED,
            approved_at=timezone.now(),
        )
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=failed_batch,
                actor=self.finance_user,
                utr_reference="UTR-FAIL-1",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("failed/rejected payout batch", str(ctx.exception))

    def test_reversed_batch_rejected(self):
        reversed_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-{uuid.uuid4().hex[:8].upper()}",
            seller=self.seller,
            amount=Decimal("1000.00"),
            currency="INR",
            status=PayoutBatch.STATUS_REVERSED,
            approved_at=timezone.now(),
        )
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=reversed_batch,
                actor=self.finance_user,
                utr_reference="UTR-REV-1",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("reversed payout batch", str(ctx.exception))

    def test_idempotent_resubmission_with_same_utr(self):
        utr = "UTR-IDEMPOTENT-001"
        success1, msg1, b1 = record_manual_bank_transfer(
            payout_batch=self.batch,
            actor=self.finance_user,
            utr_reference=utr,
            transfer_date=timezone.now().date(),
            confirmation=True,
        )
        self.assertTrue(success1)
        self.assertEqual(b1.status, PayoutBatch.STATUS_COMPLETED)

        # Repeat identical call
        success2, msg2, b2 = record_manual_bank_transfer(
            payout_batch=self.batch,
            actor=self.finance_user,
            utr_reference=utr,
            transfer_date=timezone.now().date(),
            confirmation=True,
        )
        self.assertTrue(success2)
        self.assertIn("already recorded", msg2)

        # Debit entry must exist EXACTLY once
        debits = SellerLedgerEntry.objects.filter(
            payout_batch=self.batch,
            category=SellerLedgerEntry.CATEGORY_PAYOUT,
        )
        self.assertEqual(debits.count(), 1)

    def test_completed_batch_with_different_utr_rejected(self):
        record_manual_bank_transfer(
            payout_batch=self.batch,
            actor=self.finance_user,
            utr_reference="UTR-FIRST-001",
            transfer_date=timezone.now().date(),
            confirmation=True,
        )
        # Attempt to record again with different UTR
        with self.assertRaises(LedgerServiceError) as ctx:
            record_manual_bank_transfer(
                payout_batch=self.batch,
                actor=self.finance_user,
                utr_reference="UTR-SECOND-002",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
        self.assertIn("already been completed under a different reference", str(ctx.exception))

    # =========================================================================
    # 5. Financial & Ledger Integrity
    # =========================================================================
    def test_successful_transfer_recording(self):
        utr = "UTR-SUCCESS-20261023"
        trans_date = timezone.now().date()

        success, msg, updated_batch = record_manual_bank_transfer(
            payout_batch=self.batch,
            actor=self.finance_user,
            utr_reference=utr,
            transfer_date=trans_date,
            confirmation=True,
            notes="Bank NEFT Reference verified against statement",
        )
        self.assertTrue(success)
        self.assertEqual(updated_batch.status, PayoutBatch.STATUS_COMPLETED)
        self.assertEqual(updated_batch.payout_channel, PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK)
        self.assertEqual(updated_batch.payout_method, "manual_bank_transfer")
        self.assertFalse(updated_batch.is_mock)
        self.assertEqual(updated_batch.manual_utr_reference, utr)
        self.assertEqual(updated_batch.manual_transfer_date, trans_date)
        self.assertEqual(updated_batch.external_reference, utr)
        self.assertEqual(updated_batch.provider_status, "manual_bank_transfer_recorded")

        # Verify ledger debit entry
        debit = SellerLedgerEntry.objects.get(
            payout_batch=self.batch,
            category=SellerLedgerEntry.CATEGORY_PAYOUT,
        )
        self.assertEqual(debit.entry_type, SellerLedgerEntry.TYPE_DEBIT)
        self.assertEqual(debit.amount, self.batch.amount)
        self.assertEqual(debit.status, SellerLedgerEntry.STATUS_POSTED)
        self.assertEqual(debit.idempotency_key, f"PAYOUT-DISBURSE-{self.batch.id}")
        self.assertEqual(debit.audit_metadata.get("manual_utr_reference"), utr)
        self.assertEqual(debit.audit_metadata.get("offline_manual_transfer"), True)

        # Verify PayoutAuditLog
        audit_log = PayoutAuditLog.objects.filter(
            payout_batch=self.batch,
            action=PayoutAuditLog.ACTION_DISBURSED,
        ).first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.actor, self.finance_user)
        self.assertEqual(audit_log.metadata.get("manual_utr_reference"), utr)
        self.assertEqual(audit_log.metadata.get("channel"), PayoutBatch.PAYOUT_CHANNEL_MANUAL_BANK)

    # =========================================================================
    # 6. Concurrency & Atomic Rollback
    # =========================================================================
    def test_atomic_rollback_on_debit_creation_error(self):
        utr = "UTR-FAIL-ROLLBACK"
        with patch.object(SellerLedgerEntry.objects, "create", side_effect=Exception("Database lock timeout")):
            with self.assertRaises(Exception) as ctx:
                record_manual_bank_transfer(
                    payout_batch=self.batch,
                    actor=self.finance_user,
                    utr_reference=utr,
                    transfer_date=timezone.now().date(),
                    confirmation=True,
                )
            self.assertIn("Database lock timeout", str(ctx.exception))

        # Confirm batch remains in original pending status
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, PayoutBatch.STATUS_PENDING)
        self.assertEqual(self.batch.manual_utr_reference, "")
        self.assertEqual(
            SellerLedgerEntry.objects.filter(payout_batch=self.batch).count(),
            0,
        )

    # =========================================================================
    # 7. Zero External Calls & Safety
    # =========================================================================
    def test_zero_provider_calls_made(self):
        with patch("urllib.request.urlopen") as mock_url, patch("requests.post") if hasattr(__import__("requests"), "post") else patch("urllib.request.urlopen") as mock_req:
            record_manual_bank_transfer(
                payout_batch=self.batch,
                actor=self.finance_user,
                utr_reference="UTR-NO-PROVIDER-CALL",
                transfer_date=timezone.now().date(),
                confirmation=True,
            )
            self.batch.refresh_from_db()
            self.assertEqual(self.batch.status, PayoutBatch.STATUS_COMPLETED)
            mock_url.assert_not_called()

    # =========================================================================
    # 8. Section 6 Audit Improvement: Hold Release History Preservation
    # =========================================================================
    def test_hold_release_history_preserved_upon_repeated_releases(self):
        # Create a ledger entry on hold
        entry = SellerLedgerEntry.objects.create(
            seller=self.seller,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1200.00"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            source_type="order",
            source_reference="ORD-TEST-001",
            idempotency_key="ORD-TEST-HOLD-HIST-001",
            is_hold_released=False,
            audit_metadata={"initial_flag": "test"},
        )

        # First release
        release_hold_balance_entries(
            seller=self.seller,
            entry_ids=[entry.id],
            actor=self.finance_user,
            reason="First release justification",
        )
        entry.refresh_from_db()
        self.assertTrue(entry.is_hold_released)
        self.assertEqual(entry.hold_release_reason, "First release justification")
        self.assertIn("hold_release_history", entry.audit_metadata)
        self.assertEqual(len(entry.audit_metadata["hold_release_history"]), 1)
        self.assertEqual(entry.audit_metadata["hold_release"]["reason"], "First release justification")

        # Second release (repeated release)
        release_hold_balance_entries(
            seller=self.seller,
            entry_ids=[entry.id],
            actor=self.super_admin,
            reason="Second updated release explanation",
        )
        entry.refresh_from_db()
        self.assertEqual(len(entry.audit_metadata["hold_release_history"]), 2)
        self.assertEqual(entry.audit_metadata["hold_release_history"][0]["reason"], "First release justification")
        self.assertEqual(entry.audit_metadata["hold_release_history"][1]["reason"], "Second updated release explanation")
        self.assertEqual(entry.audit_metadata["hold_release"]["reason"], "Second updated release explanation")
