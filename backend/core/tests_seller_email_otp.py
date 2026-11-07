import datetime
import io
import re
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from core.models import Seller, SellerLoginOTP
from core.otp_services import (
    generate_and_send_seller_email_otp,
    verify_seller_email_otp_code,
    normalize_email_address,
    mask_email_address,
)

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SellerEmailOTPLoginTests(TestCase):
    """
    Comprehensive test suite for Seller Hub Email OTP Authentication.
    Validates SMTP email dispatch, PBKDF2 cryptographic hashing, 5-minute expiration,
    single-use enforcement, 60-second cooldown, max 5 attempts, rate limiting,
    suspended seller protection, and regression safety.
    """

    def setUp(self):
        mail.outbox.clear()

        # 1. Approved Seller with user email
        self.approved_user = User.objects.create_user(
            username="royal_vendor",
            email="vendor@royalapparel.in",
            password="SecurePassword123!",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        self.seller = Seller.objects.create(
            user=self.approved_user,
            store_name="Royal Apparel Hub",
            legal_business_name="Royal Apparel Private Limited",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="vendor@royalapparel.in",
            support_phone="9876543210",
            registered_mobile="9876543210",
            registered_address="102 MG Road, Bengaluru",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Warehouse 4, Electronic City, Bengaluru",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            commission_rate=Decimal("10.00"),
        )

        # 2. Suspended Seller
        self.suspended_user = User.objects.create_user(
            username="suspended_vendor",
            email="suspended@badstore.in",
            password="Password456!",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        self.suspended_seller = Seller.objects.create(
            user=self.suspended_user,
            store_name="Suspended Store",
            legal_business_name="Suspended Store LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="suspended@badstore.in",
            support_phone="9876543212",
            registered_mobile="9876543212",
            registered_address="99 Market Yard, Pune",
            city="Pune",
            state="Maharashtra",
            postal_code="411001",
            pickup_address="Plot 5, MIDC, Pune",
            pickup_city="Pune",
            pickup_state="Maharashtra",
            pickup_postal_code="411002",
            gstin="27ABCDE1234F1Z5",
            pan_number="ABCDE1234P",
            status=Seller.STATUS_SUSPENDED,
            is_active=True,
        )

        # 3. Pending Seller
        self.pending_user = User.objects.create_user(
            username="pending_vendor",
            email="pending@newstore.in",
            password="Password789!",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        self.pending_seller = Seller.objects.create(
            user=self.pending_user,
            store_name="Pending Store",
            legal_business_name="Pending Store Enterprise",
            business_type=Seller.BUSINESS_TYPE_INDIVIDUAL,
            support_email="pending@newstore.in",
            support_phone="9876543213",
            registered_mobile="9876543213",
            registered_address="50 MG Road, Delhi",
            city="Delhi",
            state="Delhi",
            postal_code="110001",
            pickup_address="50 MG Road, Delhi",
            pickup_city="Delhi",
            pickup_state="Delhi",
            pickup_postal_code="110001",
            gstin="07ABCDE1234F1Z5",
            pan_number="ABCDE1234D",
            status=Seller.STATUS_PENDING,
            is_active=True,
        )

    def test_normalize_and_mask_email_utilities(self):
        """Validates email normalization and privacy-preserving masking."""
        self.assertEqual(normalize_email_address("  Vendor@Store.IN "), "vendor@store.in")
        self.assertIsNone(normalize_email_address("invalid-email-string"))
        self.assertEqual(mask_email_address("vendor@royalapparel.in"), "v***r@royalapparel.in")
        self.assertEqual(mask_email_address("ab@store.in"), "a*@store.in")

    def test_seller_email_otp_request_success_via_ajax(self):
        """Approved seller requests Email OTP via AJAX and receives real formatted transactional email."""
        response = self.client.post(
            reverse("core:seller_send_otp"),
            {"email": "vendor@royalapparel.in"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["cooldown"], 60)
        self.assertIn("v***r@royalapparel.in", data["masked_email"])

        # Verify real email dispatched via locmem backend
        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]
        self.assertIn("vendor@royalapparel.in", sent_email.to)
        self.assertIn("Seller Hub Verification Code", sent_email.subject)
        self.assertIn("Royal Apparel Hub", sent_email.body)
        self.assertIn("valid for 5 minutes", sent_email.body)

        # Verify cryptographic storage (NEVER plaintext)
        otp_record = SellerLoginOTP.objects.filter(seller=self.seller, email="vendor@royalapparel.in").first()
        self.assertIsNotNone(otp_record)
        self.assertFalse(otp_record.is_used)
        self.assertFalse(otp_record.otp_hash.isdigit())
        self.assertTrue(otp_record.otp_hash.startswith("pbkdf2_"))

    def test_seller_email_otp_verify_success_and_session_login(self):
        """Seller verifies the generated 6-digit OTP and is logged into the Seller Hub."""
        # 1. Request OTP
        success, msg, cooldown = generate_and_send_seller_email_otp("vendor@royalapparel.in")
        self.assertTrue(success)
        self.assertEqual(len(mail.outbox), 1)

        # Extract 6-digit OTP from email body
        import re
        match = re.search(r"\b\d{6}\b", mail.outbox[0].body)
        self.assertIsNotNone(match)
        raw_otp = match.group(0)

        # 2. Verify via AJAX
        response = self.client.post(
            reverse("core:seller_verify_otp"),
            {
                "email": "vendor@royalapparel.in",
                "otp": raw_otp,
                "remember_me": "on",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["redirect_url"], reverse("core:seller_dashboard"))

        # Verify user is authenticated
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.approved_user.id)

        # Verify OTP record is marked as used
        otp_record = SellerLoginOTP.objects.filter(seller=self.seller, email="vendor@royalapparel.in").first()
        self.assertTrue(otp_record.is_used)
        self.assertIsNotNone(otp_record.used_at)

    def test_seller_email_otp_single_use_enforcement(self):
        """An OTP code cannot be reused once verified."""
        generate_and_send_seller_email_otp("vendor@royalapparel.in")
        match = re.search(r"\b\d{6}\b", mail.outbox[0].body)
        raw_otp = match.group(0)

        # First verification succeeds
        valid, msg, s = verify_seller_email_otp_code("vendor@royalapparel.in", raw_otp)
        self.assertTrue(valid)

        # Second verification fails immediately
        valid2, msg2, s2 = verify_seller_email_otp_code("vendor@royalapparel.in", raw_otp)
        self.assertFalse(valid2)
        self.assertIn("already been used", msg2)
        self.assertIsNone(s2)

    def test_seller_email_otp_5_minute_expiry(self):
        """Expired OTP (> 5 minutes old) is rejected."""
        generate_and_send_seller_email_otp("vendor@royalapparel.in")
        match = re.search(r"\b\d{6}\b", mail.outbox[0].body)
        raw_otp = match.group(0)

        otp_record = SellerLoginOTP.objects.filter(seller=self.seller, email="vendor@royalapparel.in").first()
        # Fast-forward expires_at to the past
        otp_record.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        otp_record.save(update_fields=["expires_at"])

        valid, msg, s = verify_seller_email_otp_code("vendor@royalapparel.in", raw_otp)
        self.assertFalse(valid)
        self.assertIn("expired", msg.lower())

    def test_seller_email_otp_60_second_resend_cooldown(self):
        """Requesting another OTP within 60 seconds triggers a cooldown rejection."""
        generate_and_send_seller_email_otp("vendor@royalapparel.in")

        # Second immediate request
        success, msg, cooldown = generate_and_send_seller_email_otp("vendor@royalapparel.in")
        self.assertFalse(success)
        self.assertIn("wait", msg.lower())
        self.assertGreater(cooldown, 0)
        self.assertLessEqual(cooldown, 60)

    def test_seller_email_otp_max_5_verification_attempts(self):
        """After 5 invalid attempts, the OTP is permanently invalidated."""
        generate_and_send_seller_email_otp("vendor@royalapparel.in")
        match = re.search(r"\b\d{6}\b", mail.outbox[0].body)
        raw_otp = match.group(0)

        # 4 wrong attempts
        for attempt in range(1, 5):
            valid, msg, s = verify_seller_email_otp_code("vendor@royalapparel.in", "000000")
            self.assertFalse(valid)
            self.assertIn("attempt(s) remaining", msg)

        # 5th wrong attempt triggers invalidation
        valid, msg, s = verify_seller_email_otp_code("vendor@royalapparel.in", "000000")
        self.assertFalse(valid)
        self.assertIn("Maximum attempts exceeded", msg)

        # 6th attempt with CORRECT code fails because OTP is locked out
        valid, msg, s = verify_seller_email_otp_code("vendor@royalapparel.in", raw_otp)
        self.assertFalse(valid)
        self.assertIn("invalidated", msg.lower())

    def test_suspended_seller_cannot_request_email_otp(self):
        """Suspended seller is blocked from requesting an OTP code."""
        success, msg, cd = generate_and_send_seller_email_otp("suspended@badstore.in")
        self.assertFalse(success)
        self.assertIn("currently suspended", msg)
        self.assertEqual(len(mail.outbox), 0)

    def test_suspended_seller_cannot_verify_email_otp(self):
        """If a seller is suspended after OTP dispatch, verification is blocked with clear notice."""
        success, msg, cd = generate_and_send_seller_email_otp("vendor@royalapparel.in")
        self.assertTrue(success)
        match = re.search(r"\b\d{6}\b", mail.outbox[0].body)
        raw_otp = match.group(0)

        # Suspend seller
        self.seller.status = Seller.STATUS_SUSPENDED
        self.seller.save(update_fields=["status"])

        valid, verify_msg, s = verify_seller_email_otp_code("vendor@royalapparel.in", raw_otp)
        self.assertFalse(valid)
        self.assertIn("currently suspended", verify_msg)
        self.assertIsNone(s)

    def test_pending_and_inactive_seller_rejection(self):
        """Pending application and inactive seller accounts are blocked with appropriate alerts."""
        success_p, msg_p, _ = generate_and_send_seller_email_otp("pending@newstore.in")
        self.assertFalse(success_p)
        self.assertIn("pending administrator review", msg_p)

        self.seller.is_active = False
        self.seller.save(update_fields=["is_active"])
        success_i, msg_i, _ = generate_and_send_seller_email_otp("vendor@royalapparel.in")
        self.assertFalse(success_i)
        self.assertIn("inactive", msg_i.lower())

    def test_unregistered_email_rejected(self):
        """Attempting OTP login with an unregistered email returns clear notice."""
        success, msg, _ = generate_and_send_seller_email_otp("unknown@unregistered.in")
        self.assertFalse(success)
        self.assertIn("No seller account found", msg)

    def test_hourly_rate_limit_per_email(self):
        """Hourly rate limit caps requests to 5 per hour per email address."""
        # Create 5 existing OTP requests within the last hour
        for _ in range(5):
            SellerLoginOTP.objects.create(
                seller=self.seller,
                email="vendor@royalapparel.in",
                otp_hash="dummy_hash",
                expires_at=timezone.now() + datetime.timedelta(minutes=5),
            )
        SellerLoginOTP.objects.filter(seller=self.seller, email="vendor@royalapparel.in").update(
            created_at=timezone.now() - datetime.timedelta(minutes=10)
        )

        success, msg, _ = generate_and_send_seller_email_otp("vendor@royalapparel.in")
        self.assertFalse(success)
        self.assertIn("Too many verification requests", msg)

    def test_smtp_send_failure_handled_gracefully(self):
        """If the SMTP server throws an exception, the user gets a clean error and OTP is invalidated."""
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=Exception("SMTP Connection timed out")):
            success, msg, _ = generate_and_send_seller_email_otp("vendor@royalapparel.in")
            self.assertFalse(success)
            self.assertIn("Failed to deliver verification email", msg)

            # Record must be marked is_used so orphan active OTP is not left
            otp_record = SellerLoginOTP.objects.filter(seller=self.seller, email="vendor@royalapparel.in").first()
            self.assertTrue(otp_record.is_used)

    def test_password_login_continues_to_operate_seamlessly(self):
        """Existing password login tab at /seller/login/ operates without any regression."""
        response = self.client.post(
            reverse("core:seller_login"),
            {
                "username_or_email": "royal_vendor",
                "password": "SecurePassword123!",
            },
        )
        self.assertRedirects(response, reverse("core:seller_dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.approved_user.id)

    def test_management_command_test_seller_email_otp_dry_run(self):
        """Management command test_seller_email_otp executes diagnostics without external network calls."""
        out = io.StringIO()
        call_command(
            "test_seller_email_otp",
            "--dry-run",
            "--email",
            "vendor@royalapparel.in",
            stdout=out,
        )
        output = out.getvalue()
        self.assertIn("Fitmeezo Seller Hub SMTP / Email Diagnostics", output)
        self.assertIn("Destination Email: v***r@royalapparel.in", output)
        self.assertIn("[DRY RUN] Inspection complete", output)
