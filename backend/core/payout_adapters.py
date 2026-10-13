"""
Fitmeezo Marketplace Payout Provider Adapters & Razorpay Route Integration Architecture.

Provides:
1. Base provider abstraction separating customer payment gateways from seller marketplace settlement.
2. High-integrity Mock/Simulated Payout Provider Adapter for local testing (ZERO real banking calls).
3. Webhook signature verification and idempotent webhook processing.
4. Comprehensive documentation and architectural readiness for Razorpay Route / RazorpayX.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import hmac
import logging
import uuid
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class PayoutAdapterError(Exception):
    """Base exception for payout provider adapter operations."""
    pass


class BasePayoutProviderAdapter:
    """
    Abstract interface for marketplace disbursement providers.
    Enforces strict separation between customer checkout gateways and merchant settlement rails.
    """

    provider_id: str = "base"
    provider_name: str = "Base Provider"
    is_live: bool = False

    def initiate_payout(
        self,
        batch: Any,
        actor: Any = None,
        simulate_failure: bool = False,
        failure_reason: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        """
        Initiates disbursement for an eligible, locked PayoutBatch.
        Returns: (success: bool, message: str, provider_metadata: dict)
        """
        raise NotImplementedError("Subclasses must implement initiate_payout.")

    def verify_webhook_signature(self, payload: bytes, signature: str, secret: str = "") -> bool:
        """
        Cryptographically verifies incoming webhook signature.
        """
        raise NotImplementedError("Subclasses must implement verify_webhook_signature.")

    def process_webhook_event(self, event_data: dict[str, Any]) -> tuple[bool, str]:
        """
        Idempotently processes incoming asynchronous provider settlement notifications.
        """
        raise NotImplementedError("Subclasses must implement process_webhook_event.")


class MockPayoutProviderAdapter(BasePayoutProviderAdapter):
    """
    Simulated local development payout provider adapter.

    CRITICAL SAFETY & FINANCIAL CORRECTNESS RULES:
    - Never initiates live banking, NEFT, RTGS, IMPS, or Razorpay Route API calls.
    - Clearly stamps every transaction reference as 'TEST/SIMULATED'.
    - Validates seller bank verification status prior to mock disbursement.
    - Supports controlled failure simulation with automatic ledger entry unlinking.
    - Supports idempotent webhook event processing.
    """

    provider_id: str = "mock_bank_transfer"
    provider_name: str = "Fitmeezo Mock / Simulated Payout Adapter (Offline Sandbox)"
    is_live: bool = False

    def initiate_payout(
        self,
        batch: Any,
        actor: Any = None,
        simulate_failure: bool = False,
        failure_reason: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        """
        Executes simulated mock bank transfer disbursement.
        Never makes a real network or bank call during development.
        """
        from core.models import PayoutBatch, SellerLedgerEntry

        if not isinstance(batch, PayoutBatch):
            raise PayoutAdapterError("Invalid batch object passed to provider adapter.")

        # Require that live payouts remain disabled
        if not getattr(settings, "FITMEEZO_MOCK_PAYOUTS", True):
            raise PayoutAdapterError(
                "Live payouts are disabled in this environment. FITMEEZO_MOCK_PAYOUTS must be True."
            )

        with transaction.atomic():
            locked_batch = PayoutBatch.objects.select_for_update().filter(pk=batch.pk).first()
            if not locked_batch:
                raise PayoutAdapterError("Payout batch not found.")

            old_status = locked_batch.status

            if locked_batch.status == PayoutBatch.STATUS_ON_HOLD:
                raise PayoutAdapterError(
                    f"Payout batch #{locked_batch.batch_reference} is ON HOLD. "
                    f"Reason: {locked_batch.hold_reason or 'Placed on hold by platform administrator'}. "
                    "A held payment must not be sent."
                )

            # Prevent duplicate disbursements on already completed batches
            if locked_batch.status == PayoutBatch.STATUS_COMPLETED:
                return True, (
                    f"Duplicate disbursement prevented: Batch #{locked_batch.batch_reference} "
                    f"was already completed with reference {locked_batch.external_reference}."
                ), {
                    "status": "completed",
                    "duplicate": True,
                    "external_reference": locked_batch.external_reference,
                }

            if locked_batch.status not in (PayoutBatch.STATUS_DRAFT, PayoutBatch.STATUS_PROCESSING, PayoutBatch.STATUS_PENDING):
                raise PayoutAdapterError(
                    f"Batch #{locked_batch.batch_reference} cannot be processed from status: {locked_batch.get_status_display()}."
                )

            from core.models import PayoutAuditLog

            # Bank verification check: Non-platform sellers must have verified bank details
            if locked_batch.seller and not locked_batch.is_platform_fulfilled:
                if not locked_batch.seller.is_bank_payout_eligible:
                    clean_reason = "Seller bank account is not verified. Platform admin verification required."
                    locked_batch.status = PayoutBatch.STATUS_FAILED
                    locked_batch.failure_reason = clean_reason
                    locked_batch.provider_status = "simulated_failed"
                    locked_batch.retry_count = locked_batch.retry_count + 1
                    locked_batch.processed_at = timezone.now()
                    locked_batch.processed_by = actor if hasattr(actor, "id") else None
                    locked_batch.save(update_fields=["status", "failure_reason", "provider_status", "retry_count", "processed_at", "processed_by", "updated_at"])

                    # Release covered entries for retry
                    SellerLedgerEntry.objects.filter(payout_batch=locked_batch).update(payout_batch=None)

                    PayoutAuditLog.objects.create(
                        payout_batch=locked_batch,
                        action=PayoutAuditLog.ACTION_FAILED,
                        actor=actor if hasattr(actor, "id") else None,
                        from_status=old_status,
                        to_status=PayoutBatch.STATUS_FAILED,
                        reason=clean_reason,
                        metadata={"simulated": True, "provider": self.provider_name},
                    )

                    return False, f"TEST/SIMULATED payout rejected: {clean_reason}", {"status": "failed", "reason": clean_reason}

            # Failure simulation path
            if simulate_failure:
                clean_reason = failure_reason or "Simulated automated banking network transfer rejection."
                locked_batch.status = PayoutBatch.STATUS_FAILED
                locked_batch.failure_reason = clean_reason
                locked_batch.provider_status = "simulated_failed"
                locked_batch.retry_count = locked_batch.retry_count + 1
                locked_batch.processed_at = timezone.now()
                locked_batch.processed_by = actor if hasattr(actor, "id") else None
                locked_batch.save(update_fields=["status", "failure_reason", "provider_status", "retry_count", "processed_at", "processed_by", "updated_at"])

                # Release covered entries
                SellerLedgerEntry.objects.filter(payout_batch=locked_batch).update(payout_batch=None)

                PayoutAuditLog.objects.create(
                    payout_batch=locked_batch,
                    action=PayoutAuditLog.ACTION_FAILED,
                    actor=actor if hasattr(actor, "id") else None,
                    from_status=old_status,
                    to_status=PayoutBatch.STATUS_FAILED,
                    reason=clean_reason,
                    metadata={"simulated": True, "provider": self.provider_name},
                )

                return False, f"[TEST/SIMULATED] Payout #{locked_batch.batch_reference} marked as FAILED: {clean_reason}", {
                    "status": "failed",
                    "reason": clean_reason,
                    "simulated": True,
                }

            # Successful mock disbursement execution
            now_ts = timezone.now()
            sim_utr = f"MOCK-PAY-{uuid.uuid4().hex[:12].upper()}"

            locked_batch.status = PayoutBatch.STATUS_COMPLETED
            locked_batch.external_reference = sim_utr
            locked_batch.provider_status = "simulated_sandbox"
            locked_batch.retry_count = locked_batch.retry_count + 1
            locked_batch.processed_at = now_ts
            locked_batch.processed_by = actor if hasattr(actor, "id") else None

            # Snapshot bank metadata on the batch
            if locked_batch.seller and not locked_batch.bank_account_masked:
                locked_batch.bank_account_masked = locked_batch.seller.get_masked_account_number()
                locked_batch.bank_ifsc_code = locked_batch.seller.bank_ifsc_code
                locked_batch.bank_beneficiary_name = (
                    locked_batch.seller.bank_account_holder_name or locked_batch.seller.store_name
                )

            locked_batch.save(
                update_fields=[
                    "status",
                    "external_reference",
                    "provider_status",
                    "retry_count",
                    "processed_at",
                    "processed_by",
                    "bank_account_masked",
                    "bank_ifsc_code",
                    "bank_beneficiary_name",
                    "updated_at",
                ]
            )

            # Post immutable PAYOUT (Debit) settlement ledger entry to close merchant balance
            payout_debit_key = f"PAYOUT-DISBURSE-{locked_batch.id}"
            if not SellerLedgerEntry.objects.filter(idempotency_key=payout_debit_key).exists():
                SellerLedgerEntry.objects.create(
                    seller=locked_batch.seller,
                    is_platform_fulfilled=locked_batch.is_platform_fulfilled,
                    entry_type=SellerLedgerEntry.TYPE_DEBIT,
                    category=SellerLedgerEntry.CATEGORY_PAYOUT,
                    amount=locked_batch.amount,
                    currency=locked_batch.currency,
                    status=SellerLedgerEntry.STATUS_POSTED,
                    source_type="payout_batch",
                    source_reference=locked_batch.batch_reference,
                    payout_batch=locked_batch,
                    idempotency_key=payout_debit_key,
                    description=f"[TEST/SIMULATED] Mock disbursement settlement for #{locked_batch.batch_reference} ({sim_utr})",
                    posted_at=now_ts,
                    created_by=actor if hasattr(actor, "id") else None,
                    audit_metadata={
                        "external_reference": sim_utr,
                        "payout_method": locked_batch.payout_method,
                        "beneficiary": locked_batch.bank_beneficiary_name,
                        "masked_account": locked_batch.bank_account_masked,
                        "ifsc": locked_batch.bank_ifsc_code,
                        "provider": self.provider_name,
                        "simulated": True,
                    },
                )

            PayoutAuditLog.objects.create(
                payout_batch=locked_batch,
                action=PayoutAuditLog.ACTION_DISBURSED,
                actor=actor if hasattr(actor, "id") else None,
                from_status=old_status,
                to_status=PayoutBatch.STATUS_COMPLETED,
                metadata={
                    "external_reference": sim_utr,
                    "provider": self.provider_name,
                    "simulated": True,
                },
            )

            msg = (
                f"[TEST/SIMULATED] Payout batch #{locked_batch.batch_reference} completed successfully. "
                f"Simulated UTR: {sim_utr}. Zero real funds transferred (Live payouts not configured)."
            )
            return True, msg, {
                "status": "completed",
                "simulated_utr": sim_utr,
                "amount": str(locked_batch.amount),
                "simulated": True,
            }

    def verify_webhook_signature(self, payload: bytes, signature: str, secret: str = "") -> bool:
        """
        Verifies mock HMAC SHA-256 webhook signature.
        """
        if not signature:
            return False
        effective_secret = secret or getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "mock_webhook_secret_key")
        expected = hmac.new(effective_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def process_webhook_event(self, event_data: dict[str, Any]) -> tuple[bool, str]:
        """
        Idempotently processes simulated webhook events such as:
        - 'payout.processed'
        - 'payout.failed'
        - 'payout.reversed'
        """
        from core.models import PayoutBatch

        event_id = event_data.get("id") or event_data.get("event_id")
        event_type = event_data.get("event") or event_data.get("event_type")
        payload = event_data.get("payload", {})
        batch_ref = payload.get("batch_reference") or payload.get("reference_id")

        if not batch_ref:
            return False, "Missing batch_reference in webhook payload."

        batch = PayoutBatch.objects.filter(batch_reference=batch_ref).first()
        if not batch:
            return False, f"PayoutBatch '{batch_ref}' not found."

        # Idempotency check: If already completed or failed, do not repeat
        if event_type == "payout.processed" and batch.status == PayoutBatch.STATUS_COMPLETED:
            return True, f"Idempotent: Batch #{batch_ref} already COMPLETED."

        if event_type == "payout.failed" and batch.status == PayoutBatch.STATUS_FAILED:
            return True, f"Idempotent: Batch #{batch_ref} already FAILED."

        if event_type == "payout.processed":
            return self.initiate_payout(batch, simulate_failure=False)
        elif event_type == "payout.failed":
            reason = payload.get("failure_reason", "Provider webhook reported failure.")
            return self.initiate_payout(batch, simulate_failure=True, failure_reason=reason)
        else:
            return True, f"Event '{event_type}' acknowledged (no state transition required)."


# ==============================================================================
# RAZORPAY ROUTE MARKETPLACE INTEGRATION AUDIT & DOCUMENTATION
# ==============================================================================

class RazorpayRouteMarketplaceDocumentation:
    """
    Architectural documentation and compliance prerequisites for Razorpay Route / RazorpayX.

    STATUS: ACCESS MUST BE REQUESTED (NOT ACTIVE IN CURRENT CREDENTIALS)
    =============================================================================
    The existing project settings provide standard customer gateway credentials:
    - RAZORPAY_KEY_ID
    - RAZORPAY_KEY_SECRET

    These keys allow customer checkout (card, UPI, netbanking payment capture into
    the platform merchant account). They do NOT grant access to Razorpay Route
    (Marketplace Split Transfers) or RazorpayX (Commercial Payouts).

    REQUIRED STEPS FOR LIVE RAZORPAY ROUTE ACTIVATION:
    -----------------------------------------------------------------------------
    1. Razorpay Route Partnership Activation:
       - Contact Razorpay account management to enable 'Razorpay Route (Marketplace)'
         on the parent merchant MID.
       - Agreement must specify the RBI Escrow Account structure for marketplace split payments.

    2. Seller Linked Account Onboarding (/v1/accounts):
       - Sellers must be onboarded using Razorpay Route Accounts API:
         POST https://api.razorpay.com/v1/accounts
         Body:
         {
           "name": seller.legal_business_name,
           "email": seller.support_email,
           "phone": seller.registered_mobile,
           "business_type": seller.business_type,  // proprietorship, partnership, private_limited
           "legal_business_name": seller.legal_business_name,
           "profile": {
             "category": "ecommerce",
             "subcategory": "marketplace",
             "addresses": {
               "registered": {
                 "street1": seller.registered_address,
                 "city": seller.city,
                 "state": seller.state,
                 "postal_code": seller.postal_code,
                 "country": "IN"
               }
             }
           },
           "bank_account": {
             "account_number": seller.bank_account_number,
             "ifsc": seller.bank_ifsc_code,
             "beneficiary_name": seller.bank_account_holder_name
           }
         }
       - Razorpay returns an `account_id` (e.g. 'acc_G2h7...').
       - Seller bank ownership is verified by Razorpay penny drop before activation.

    3. Order Split Transfers (/v1/orders/{order_id}/transfers or /v1/payments/{payment_id}/transfers):
       - When a customer pays for an order containing multiple sellers:
         POST https://api.razorpay.com/v1/payments/{payment_id}/transfers
         Body:
         {
           "transfers": [
             {
               "account": seller_1.razorpay_account_id,
               "amount": int(seller_1_net_amount * 100),  // in paise
               "currency": "INR",
               "on_hold": 1,                              // HELD until delivery cooling window
               "on_hold_until": int(matured_timestamp)
             }
           ]
         }

    4. Transfer Settlement & Release:
       - Once package is DELIVERED and return window expires (7 days):
         PATCH https://api.razorpay.com/v1/transfers/{transfer_id}
         Body: {"on_hold": 0}

    5. Webhook Subscriptions:
       - Subscribe to webhook events:
         - `transfer.processed`: Transfer successfully cleared to seller.
         - `transfer.failed`: Transfer rejected (e.g. invalid bank IFSC).
         - `transfer.reversed`: Refund triggered, deducting funds back to customer.
       - Verify signature with HMAC SHA-256 using `RAZORPAY_WEBHOOK_SECRET`.
       - Idempotently handle duplicate events.
    """
    pass


# Default provider instance
default_payout_provider = MockPayoutProviderAdapter()


class LivePayoutProviderAdapter(BasePayoutProviderAdapter):
    """
    Live marketplace payout provider integration rail (e.g. RazorpayX / Razorpay Route).
    Enforces that live transfers cannot be triggered unless:
    1. FITMEEZO_LIVE_PAYOUTS_ENABLED is True in settings.
    2. Explicit provider credentials and webhook secrets are configured.
    3. Merchant bank account is verified.
    """

    provider_id: str = "live_provider"
    provider_name: str = "Production Live Payout Provider Rail"
    is_live: bool = True

    def initiate_payout(
        self,
        batch: Any,
        actor: Any = None,
        simulate_failure: bool = False,
        failure_reason: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        """
        Disbursement rail for live banking funds transfers.
        Strictly raises PayoutAdapterError until credentials & explicit enablement are confirmed.
        """
        if not getattr(settings, "FITMEEZO_LIVE_PAYOUTS_ENABLED", False):
            raise PayoutAdapterError(
                "Live payouts are disabled in this environment. FITMEEZO_LIVE_PAYOUTS_ENABLED is False. "
                "Real bank transfers cannot be initiated until provider credentials and activation are confirmed."
            )

        # Validation of required provider credentials
        key_id = getattr(settings, "RAZORPAY_KEY_ID", "")
        key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")
        account_number = getattr(settings, "RAZORPAYX_ACCOUNT_NUMBER", "")
        if not (key_id and key_secret and account_number):
            raise PayoutAdapterError(
                "Live payout provider credentials incomplete. RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, "
                "and RAZORPAYX_ACCOUNT_NUMBER must be configured."
            )

        raise NotImplementedError("Live provider payout integration is pending merchant banking API activation.")

    def verify_webhook_signature(self, payload: bytes, signature: str, secret: str = "") -> bool:
        if not signature:
            return False
        effective_secret = secret or getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
        if not effective_secret:
            return False
        expected = hmac.new(effective_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def process_webhook_event(self, event_data: dict[str, Any]) -> tuple[bool, str]:
        raise NotImplementedError("Live webhook processing pending merchant banking API activation.")


def get_payout_provider() -> BasePayoutProviderAdapter:
    """
    Factory function returning the active payout provider adapter.
    Defaults to MockPayoutProviderAdapter with live transfers strictly disabled.
    """
    if getattr(settings, "FITMEEZO_LIVE_PAYOUTS_ENABLED", False):
        return LivePayoutProviderAdapter()
    return default_payout_provider


def get_payout_system_status() -> dict[str, Any]:
    """
    Returns current configuration status of the payout subsystem.
    """
    is_live_enabled = getattr(settings, "FITMEEZO_LIVE_PAYOUTS_ENABLED", False)
    is_mock = getattr(settings, "FITMEEZO_MOCK_PAYOUTS", True)
    return {
        "is_live_configured": is_live_enabled,
        "is_mock_enabled": is_mock,
        "status_label": "Live Payouts Enabled" if is_live_enabled else "Live payouts not configured",
        "provider_name": "Live Payout Rail" if is_live_enabled else "Mock / Sandbox Simulation (Zero real transfers)",
        "badge_class": "bg-success" if is_live_enabled else "bg-warning text-dark",
    }

