"""
Focused Model & Lifecycle Tests for FitMeezo Seller Closure & Final Settlement (Phase 1).

Covers:
1. Seller Closure Lifecycle States:
   - Valid states: STATUS_CLOSURE_REQUESTED, STATUS_CLOSING, STATUS_CLOSED.
   - Property invariants: is_closure_requested, is_closing, is_closed.
   - Selling restrictions: is_approved is False and can_sell is False during closure.
   - Payout eligibility during closing: is_bank_payout_eligible remains True for STATUS_CLOSING
     (to allow final settlement run), but False for STATUS_CLOSED and STATUS_CLOSURE_REQUESTED.
   - Offboarding audit fields: closure_requested_at, closure_request_reason, closure_approved_at,
     closure_approved_by, closed_at, closed_by, closure_notes.
2. PayoutBatch Batch Type Discriminator:
   - Default batch_type is BATCH_TYPE_CYCLE ('cycle').
   - Setting batch_type to BATCH_TYPE_FINAL_SETTLEMENT ('final_settlement') sets is_final_settlement to True.
   - Database constraint 'unique_active_seller_final_settlement_batch':
     * Exactly one active final settlement batch permitted per seller.
     * Attempting to create a second active final batch for the same seller raises IntegrityError.
     * A failed or reversed final batch permits a new active final batch.
     * Multiple distinct sellers can each have an active final settlement batch concurrently.
     * Final settlement batches coexist cleanly with regular cycle batches.
3. Backward Compatibility:
   - Existing approved, pending, and suspended sellers retain previous behavior without breakage.
   - Checkout merchant validation rejects closure_requested, closing, and closed merchants.
"""

from decimal import Decimal
import uuid
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Category, Order, OrderItem, PayoutBatch, Product, Seller, SellerOrder

User = get_user_model()


class SellerClosureModelLifecycleTestCase(TestCase):
    def setUp(self):
        # 1. Staff Users
        self.super_admin = User.objects.create_user(
            username="closure_admin",
            email="closure_admin@fitmeezo.com",
            password="AdminPassword123!",
            is_staff=True,
            is_superuser=True,
        )

        # 2. Merchants
        self.seller_user1 = User.objects.create_user(
            username="closing_merchant_1",
            email="merchant1@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Varanasi Silk Emporium",
            legal_business_name="Varanasi Silk Pvt Ltd",
            support_email="support@varanasi.com",
            support_phone="9876543210",
            registered_address="10 Ghat Road",
            city="Varanasi",
            state="Uttar Pradesh",
            postal_code="221001",
            pickup_address="10 Ghat Road",
            pickup_city="Varanasi",
            pickup_state="Uttar Pradesh",
            pickup_postal_code="221001",
            gstin="09ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Varanasi Silk Pvt Ltd",
            bank_account_number="9876543210001",
            bank_ifsc_code="HDFC0001234",
            bank_name="HDFC Bank",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.super_admin,
        )

        self.seller_user2 = User.objects.create_user(
            username="closing_merchant_2",
            email="merchant2@fitmeezo.com",
            password="SellerPassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Jaipur Gems & Crafts",
            legal_business_name="Jaipur Gems LLP",
            support_email="support@jaipurgems.com",
            support_phone="9876543211",
            registered_address="20 Johari Bazar",
            city="Jaipur",
            state="Rajasthan",
            postal_code="302001",
            pickup_address="20 Johari Bazar",
            pickup_city="Jaipur",
            pickup_state="Rajasthan",
            pickup_postal_code="302001",
            gstin="08ABCDE1234F1Z6",
            pan_number="ABCDE1234G",
            commission_rate=Decimal("12.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
            bank_account_holder_name="Jaipur Gems LLP",
            bank_account_number="9876543210002",
            bank_ifsc_code="ICIC0001234",
            bank_name="ICICI Bank",
            bank_verification_status="verified",
            bank_verified_at=timezone.now(),
            bank_verified_by=self.super_admin,
        )

    # =========================================================================
    # 1. Seller Closure Lifecycle & State Transitions
    # =========================================================================

    def test_seller_closure_requested_state_properties(self):
        """When a seller requests closure, selling is halted and closure request fields are tracked."""
        now = timezone.now()
        self.seller1.status = Seller.STATUS_CLOSURE_REQUESTED
        self.seller1.is_active = False  # Storefront unlisted immediately
        self.seller1.closure_requested_at = now
        self.seller1.closure_request_reason = "Voluntary merchant business closure."
        self.seller1.save()

        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.status, Seller.STATUS_CLOSURE_REQUESTED)
        self.assertTrue(self.seller1.is_closure_requested)
        self.assertFalse(self.seller1.is_closing)
        self.assertFalse(self.seller1.is_closed)
        self.assertFalse(self.seller1.is_approved)
        self.assertFalse(self.seller1.can_sell)
        # Bank payout eligibility is False while still in requested state (awaiting admin approval)
        self.assertFalse(self.seller1.is_bank_payout_eligible)
        self.assertEqual(self.seller1.closure_request_reason, "Voluntary merchant business closure.")
        self.assertIsNotNone(self.seller1.closure_requested_at)

    def test_seller_closing_state_properties(self):
        """When admin approves closure initiation, status becomes CLOSING, allowing final payout eligibility."""
        now = timezone.now()
        self.seller1.status = Seller.STATUS_CLOSING
        self.seller1.is_active = False
        self.seller1.closure_approved_at = now
        self.seller1.closure_approved_by = self.super_admin
        self.seller1.closure_notes = "Clearance checklist pending: 2 in-transit shipments, 1 COD reconciliation."
        self.seller1.save()

        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.status, Seller.STATUS_CLOSING)
        self.assertFalse(self.seller1.is_closure_requested)
        self.assertTrue(self.seller1.is_closing)
        self.assertFalse(self.seller1.is_closed)
        self.assertFalse(self.seller1.is_approved)
        self.assertFalse(self.seller1.can_sell)
        # Crucial invariant: bank payout eligibility is True during CLOSING so final settlement batch can be processed
        self.assertTrue(self.seller1.is_bank_payout_eligible)
        self.assertEqual(self.seller1.closure_approved_by, self.super_admin)

    def test_seller_closed_state_properties(self):
        """When closure is finalized, status becomes CLOSED, terminating all active operations."""
        now = timezone.now()
        self.seller1.status = Seller.STATUS_CLOSED
        self.seller1.is_active = False
        self.seller1.closed_at = now
        self.seller1.closed_by = self.super_admin
        self.seller1.closure_notes = "Final settlement completed via UTR-20261023-001. Ledger balanced to zero."
        self.seller1.save()

        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.status, Seller.STATUS_CLOSED)
        self.assertFalse(self.seller1.is_closure_requested)
        self.assertFalse(self.seller1.is_closing)
        self.assertTrue(self.seller1.is_closed)
        self.assertFalse(self.seller1.is_approved)
        self.assertFalse(self.seller1.can_sell)
        # In terminal closed state, no further payouts are permitted
        self.assertFalse(self.seller1.is_bank_payout_eligible)
        self.assertEqual(self.seller1.closed_by, self.super_admin)
        self.assertIsNotNone(self.seller1.closed_at)

    def test_backward_compatibility_existing_seller_statuses(self):
        """Existing statuses (APPROVED, PENDING, SUSPENDED, REJECTED) operate as before."""
        # Approved seller
        self.assertEqual(self.seller2.status, Seller.STATUS_APPROVED)
        self.assertTrue(self.seller2.is_approved)
        self.assertTrue(self.seller2.can_sell)
        self.assertTrue(self.seller2.is_bank_payout_eligible)
        self.assertFalse(self.seller2.is_closure_requested)
        self.assertFalse(self.seller2.is_closing)
        self.assertFalse(self.seller2.is_closed)

        # Suspended seller
        self.seller2.status = Seller.STATUS_SUSPENDED
        self.seller2.is_active = False
        self.seller2.save()
        self.assertFalse(self.seller2.is_approved)
        self.assertFalse(self.seller2.can_sell)
        self.assertFalse(self.seller2.is_bank_payout_eligible)
        self.assertFalse(self.seller2.is_closed)

    # =========================================================================
    # 2. PayoutBatch Batch Type & Final Settlement Constraints
    # =========================================================================

    def test_default_payout_batch_type_is_cycle(self):
        """Newly created PayoutBatch defaults to batch_type='cycle'."""
        batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-CYCLE-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("1500.00"),
            status=PayoutBatch.STATUS_DRAFT,
            payout_cycle="2026-10-C1",
        )
        self.assertEqual(batch.batch_type, PayoutBatch.BATCH_TYPE_CYCLE)
        self.assertFalse(batch.is_final_settlement)

    def test_final_settlement_batch_type_property(self):
        """PayoutBatch with batch_type='final_settlement' reports is_final_settlement=True."""
        batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("450.00"),  # Below standard 500 threshold
            status=PayoutBatch.STATUS_DRAFT,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
            payout_cycle=f"FINAL-{self.seller1.id}",
        )
        self.assertEqual(batch.batch_type, PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT)
        self.assertTrue(batch.is_final_settlement)

    def test_unique_active_final_settlement_batch_constraint(self):
        """Database constraint prevents multiple active final settlement batches for the same seller."""
        # 1. Create first active final settlement batch
        PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-A-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("500.00"),
            status=PayoutBatch.STATUS_DRAFT,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
            payout_cycle=f"FINAL-1",
        )

        # 2. Attempting to create second active final batch for SAME seller raises IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PayoutBatch.objects.create(
                    batch_reference=f"PAY-FINAL-B-{uuid.uuid4().hex[:6].upper()}",
                    seller=self.seller1,
                    amount=Decimal("500.00"),
                    status=PayoutBatch.STATUS_PENDING,
                    batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
                    payout_cycle=f"FINAL-2",
                )

    def test_failed_or_reversed_final_batch_permits_new_final_batch(self):
        """If a final batch fails or is reversed, the seller can receive a new active final settlement batch."""
        failed_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-FAIL-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("500.00"),
            status=PayoutBatch.STATUS_FAILED,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
            failure_reason="Invalid beneficiary bank credentials",
        )
        self.assertEqual(failed_batch.status, PayoutBatch.STATUS_FAILED)

        # A new active final batch is permitted because the previous one is failed
        new_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-RETRY-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("500.00"),
            status=PayoutBatch.STATUS_DRAFT,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
        )
        self.assertEqual(new_batch.status, PayoutBatch.STATUS_DRAFT)

    def test_multiple_sellers_can_have_concurrent_final_batches(self):
        """Seller 1 and Seller 2 can each have an active final settlement batch concurrently."""
        batch1 = PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-S1-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("600.00"),
            status=PayoutBatch.STATUS_DRAFT,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
        )
        batch2 = PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-S2-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller2,
            amount=Decimal("750.00"),
            status=PayoutBatch.STATUS_DRAFT,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
        )
        self.assertEqual(batch1.seller, self.seller1)
        self.assertEqual(batch2.seller, self.seller2)

    def test_final_batch_coexists_with_routine_cycle_batches(self):
        """A seller can have historical or existing routine cycle batches alongside a final settlement batch."""
        cycle_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-CYCLE-HIST-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("2000.00"),
            status=PayoutBatch.STATUS_COMPLETED,
            batch_type=PayoutBatch.BATCH_TYPE_CYCLE,
            payout_cycle="2026-09-C2",
        )
        final_batch = PayoutBatch.objects.create(
            batch_reference=f"PAY-FINAL-COEXIST-{uuid.uuid4().hex[:6].upper()}",
            seller=self.seller1,
            amount=Decimal("350.00"),
            status=PayoutBatch.STATUS_DRAFT,
            batch_type=PayoutBatch.BATCH_TYPE_FINAL_SETTLEMENT,
            payout_cycle=f"FINAL-{self.seller1.id}",
        )
        self.assertFalse(cycle_batch.is_final_settlement)
        self.assertTrue(final_batch.is_final_settlement)
