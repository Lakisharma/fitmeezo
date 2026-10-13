import datetime
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category,
    CustomerProfile,
    Product,
    Seller,
    SellerLoginOTP,
    SellerOrder,
)
from core.otp_services import (
    generate_and_send_seller_otp,
    normalize_mobile_number,
    verify_seller_otp_code,
)
from core.sms_services import (
    ConsoleSMSProvider,
    MockSMSProvider,
    UnconfiguredSMSProvider,
    get_sms_provider,
    mask_phone_number,
)

User = get_user_model()


class SellerMobileOTPLoginTests(TestCase):
    """
    Comprehensive test suite for Seller Mobile OTP Authentication.
    Validates cryptographic hashing, expiration, single-use enforcement,
    resend throttling, rate limiting, access control, and data isolation.
    """

    def setUp(self):
        MockSMSProvider.clear()

        # 1. Create User & Approved Seller
        self.approved_user = User.objects.create_user(
            username="approved_vendor",
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
            support_email="support@royalapparel.in",
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

        # 2. Create Second Seller for isolation tests
        self.seller2_user = User.objects.create_user(
            username="seller_two",
            email="two@store.in",
            password="Password456!",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        self.seller2 = Seller.objects.create(
            user=self.seller2_user,
            store_name="Second Merchant Co",
            legal_business_name="Second Merchant LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="two@store.in",
            support_phone="9876543211",
            registered_mobile="9876543211",
            registered_address="202 Ring Road, Delhi",
            city="New Delhi",
            state="Delhi",
            postal_code="110001",
            pickup_address="202 Ring Road, Delhi",
            pickup_city="New Delhi",
            pickup_state="Delhi",
            pickup_postal_code="110001",
            gstin="07ABCDE1234F1Z9",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            commission_rate=Decimal("12.00"),
        )

        # 3. Create Pending Seller
        self.pending_user = User.objects.create_user(
            username="pending_vendor",
            email="pending@store.in",
            password="PendingPass123!",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        self.pending_seller = Seller.objects.create(
            user=self.pending_user,
            store_name="Pending Review Store",
            legal_business_name="Pending Entity",
            business_type=Seller.BUSINESS_TYPE_INDIVIDUAL,
            support_email="pending@store.in",
            support_phone="9876543212",
            registered_mobile="9876543212",
            registered_address="404 Market Road, Pune",
            city="Pune",
            state="Maharashtra",
            postal_code="411001",
            pickup_address="404 Market Road, Pune",
            pickup_city="Pune",
            pickup_state="Maharashtra",
            pickup_postal_code="411001",
            gstin="27ABCDE1234F1Z8",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_PENDING,
            is_active=True,
        )

        # 4. Create Staff User
        self.staff_user = User.objects.create_user(
            username="platform_admin",
            email="admin@fitmeezo.in",
            password="AdminPass123!",
            is_staff=True,
            is_superuser=True,
            is_active=True,
        )

    # --------------------------------------------------------------------------
    # Helper & Masking Tests
    # --------------------------------------------------------------------------

    def test_mobile_normalization_and_masking(self):
        """Validates mobile cleaning and safe masking."""
        self.assertEqual(normalize_mobile_number("9876543210"), "9876543210")
        self.assertEqual(normalize_mobile_number("+91 98765-43210"), "9876543210")
        self.assertEqual(normalize_mobile_number("09876543210"), "9876543210")
        self.assertIsNone(normalize_mobile_number("12345"))
        self.assertIsNone(normalize_mobile_number("5876543210"))  # Does not start with 6-9

        self.assertEqual(mask_phone_number("9876543210"), "******3210")
        self.assertEqual(mask_phone_number(""), "****")

    # --------------------------------------------------------------------------
    # Send OTP Tests
    # --------------------------------------------------------------------------

    @override_settings(DEBUG=True)
    def test_send_otp_success_for_approved_seller(self):
        """Approved seller requests OTP: code dispatched and hashed record created."""
        response = self.client.post(
            reverse("core:seller_send_otp"),
            {"mobile": "9876543210"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["cooldown"], 60)

        # Verify DB record created with hash
        otp_record = SellerLoginOTP.objects.filter(mobile="9876543210").first()
        self.assertIsNotNone(otp_record)
        self.assertEqual(otp_record.seller, self.seller)
        self.assertFalse(otp_record.is_used)

        # Verify last sent OTP via mock provider matches hash
        last_otp = MockSMSProvider.get_last_otp("9876543210")
        self.assertIsNotNone(last_otp)
        self.assertEqual(len(last_otp), 6)
        self.assertTrue(check_password(last_otp, otp_record.otp_hash))

    def test_plaintext_otp_never_stored_in_database(self):
        """Plaintext OTP is never stored in database; only PBKDF2 hash exists."""
        success, msg, cd = generate_and_send_seller_otp("9876543210")
        self.assertTrue(success)

        otp_record = SellerLoginOTP.objects.filter(mobile="9876543210").first()
        raw_otp = MockSMSProvider.get_last_otp("9876543210")

        # The raw OTP string must NOT match the stored hash string
        self.assertNotEqual(otp_record.otp_hash, raw_otp)
        self.assertTrue(otp_record.otp_hash.startswith("pbkdf2_"))
        self.assertTrue(check_password(raw_otp, otp_record.otp_hash))

    # --------------------------------------------------------------------------
    # Verify OTP & Login Session Tests
    # --------------------------------------------------------------------------

    @override_settings(DEBUG=True)
    def test_verify_otp_valid_creates_authenticated_session(self):
        """Submitting valid OTP authenticates session and redirects to seller dashboard."""
        generate_and_send_seller_otp("9876543210")
        raw_otp = MockSMSProvider.get_last_otp("9876543210")

        response = self.client.post(
            reverse("core:seller_verify_otp"),
            {"mobile": "9876543210", "otp": raw_otp, "remember_me": "on"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["redirect_url"], reverse("core:seller_dashboard"))

        # Verify Django session is authenticated
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.approved_user.id)

        # Verify dashboard is accessible
        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, "Royal Apparel Hub")

    @override_settings(DEBUG=True)
    def test_verify_otp_invalid_code_decrements_attempts(self):
        """Submitting invalid OTP fails and increments attempt counter."""
        generate_and_send_seller_otp("9876543210")

        response = self.client.post(
            reverse("core:seller_verify_otp"),
            {"mobile": "9876543210", "otp": "000000"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("Invalid OTP", data["message"])
        self.assertIn("4 attempt(s) remaining", data["message"])

        otp_record = SellerLoginOTP.objects.filter(mobile="9876543210").first()
        self.assertEqual(otp_record.attempts_count, 1)

    def test_verify_otp_locked_after_max_attempts(self):
        """After 5 failed attempts, OTP record is locked and invalidated."""
        generate_and_send_seller_otp("9876543210")
        raw_otp = MockSMSProvider.get_last_otp("9876543210")

        # 5 wrong attempts
        for _ in range(5):
            verify_seller_otp_code("9876543210", "111111")

        # 6th attempt with correct OTP must fail
        success, msg, seller = verify_seller_otp_code("9876543210", raw_otp)
        self.assertFalse(success)
        self.assertIn("Too many failed verification attempts", msg)
        self.assertIsNone(seller)

    def test_verify_otp_expired_fails(self):
        """OTP older than 5 minutes is rejected."""
        generate_and_send_seller_otp("9876543210")
        raw_otp = MockSMSProvider.get_last_otp("9876543210")

        otp_record = SellerLoginOTP.objects.filter(mobile="9876543210").first()
        # Fast-forward expiration to 10 minutes ago
        otp_record.expires_at = timezone.now() - datetime.timedelta(minutes=10)
        otp_record.save(update_fields=["expires_at"])

        success, msg, seller = verify_seller_otp_code("9876543210", raw_otp)
        self.assertFalse(success)
        self.assertIn("expired", msg.lower())
        self.assertIsNone(seller)

    def test_verify_otp_single_use_cannot_be_reused(self):
        """An OTP cannot be verified twice."""
        generate_and_send_seller_otp("9876543210")
        raw_otp = MockSMSProvider.get_last_otp("9876543210")

        # First verification succeeds
        success1, msg1, seller1 = verify_seller_otp_code("9876543210", raw_otp)
        self.assertTrue(success1)
        self.assertIsNotNone(seller1)

        # Second verification fails
        success2, msg2, seller2 = verify_seller_otp_code("9876543210", raw_otp)
        self.assertFalse(success2)
        self.assertIn("already been used", msg2.lower())
        self.assertIsNone(seller2)

    # --------------------------------------------------------------------------
    # Throttling & Rate Limiting Tests
    # --------------------------------------------------------------------------

    def test_send_otp_resend_cooldown_throttling(self):
        """Requesting OTP again within 60s is throttled with remaining countdown."""
        success1, msg1, cd1 = generate_and_send_seller_otp("9876543210")
        self.assertTrue(success1)
        self.assertEqual(cd1, 60)

        # Immediate second request must fail
        success2, msg2, cd2 = generate_and_send_seller_otp("9876543210")
        self.assertFalse(success2)
        self.assertIn("Please wait", msg2)
        self.assertTrue(0 < cd2 <= 60)

    def test_send_otp_hourly_rate_limit(self):
        """Exceeding 5 requests in an hour blocks further requests."""
        for i in range(5):
            # Create simulated historical records within the hour
            SellerLoginOTP.objects.create(
                seller=self.seller,
                mobile="9876543210",
                otp_hash="mock",
                expires_at=timezone.now() + datetime.timedelta(minutes=5),
                is_used=True,
            )
        # Shift created_at back so it does not trigger the 60s resend cooldown
        SellerLoginOTP.objects.filter(mobile="9876543210").update(
            created_at=timezone.now() - datetime.timedelta(minutes=5)
        )

        # 6th request must be rate-limited
        success, msg, cd = generate_and_send_seller_otp("9876543210")
        self.assertFalse(success)
        self.assertIn("Too many OTP requests", msg)

    # --------------------------------------------------------------------------
    # Access Control & Security Tests
    # --------------------------------------------------------------------------

    def test_send_otp_unapproved_seller_rejected(self):
        """Pending, rejected, or suspended seller cannot receive login OTP."""
        # Pending seller
        success, msg, _ = generate_and_send_seller_otp("9876543212")
        self.assertFalse(success)
        self.assertIn("pending administrator review", msg)

        # Suspended seller
        self.seller.status = Seller.STATUS_SUSPENDED
        self.seller.save(update_fields=["status"])
        success_susp, msg_susp, _ = generate_and_send_seller_otp("9876543210")
        self.assertFalse(success_susp)
        self.assertIn("suspended", msg_susp)

    def test_send_otp_nonexistent_mobile_rejected(self):
        """Unregistered mobile number receives clear, safe rejection without leaking data."""
        success, msg, _ = generate_and_send_seller_otp("9999999999")
        self.assertFalse(success)
        self.assertIn("No seller account found", msg)

    def test_send_otp_staff_account_blocked(self):
        """Staff/administrators cannot use vendor OTP login."""
        # Attach staff user to a mock seller
        self.seller.user = self.staff_user
        self.seller.save(update_fields=["user"])

        success, msg, _ = generate_and_send_seller_otp("9876543210")
        self.assertFalse(success)
        self.assertIn("Staff and platform administrators", msg)

    def test_duplicate_mobile_numbers_prevented(self):
        """Duplicate mobile numbers are rejected by Seller model validation."""
        dup_user = User.objects.create_user(
            username="dup_mobile_tester",
            email="dup_mobile@test.in",
            password="Pass123Password!",
        )
        dup_seller = Seller(
            user=dup_user,
            store_name="Duplicate Mobile Store",
            legal_business_name="Dup Entity",
            business_type=Seller.BUSINESS_TYPE_INDIVIDUAL,
            support_email="dup@store.in",
            support_phone="9876543210",
            registered_mobile="9876543210",  # Same as self.seller
            registered_address="123 Road",
            city="Pune",
            state="Maharashtra",
            postal_code="411001",
            gstin="33XYZAB5678G1Z1",
            pan_number="XYZAB5678G",
        )
        with self.assertRaises(ValidationError) as ctx:
            dup_seller.clean()
        self.assertIn("registered_mobile", ctx.exception.message_dict)

    # --------------------------------------------------------------------------
    # Production Safety & Provider Guarantees
    # --------------------------------------------------------------------------

    @override_settings(DEBUG=False, FORCE_PROD_SMS_CHECK=True)
    def test_production_safety_disallows_mock_or_console_provider(self):
        """Mock or Console SMS provider raises ImproperlyConfigured in production."""
        import os
        old_val = os.environ.get("SMS_PROVIDER")
        try:
            os.environ["SMS_PROVIDER"] = "mock"
            with self.assertRaises(ImproperlyConfigured):
                get_sms_provider()

            os.environ["SMS_PROVIDER"] = "console"
            with self.assertRaises(ImproperlyConfigured):
                get_sms_provider()
        finally:
            if old_val is not None:
                os.environ["SMS_PROVIDER"] = old_val
            else:
                os.environ.pop("SMS_PROVIDER", None)

    @override_settings(DEBUG=False)
    def test_unconfigured_sms_provider_in_production_never_pretends_success(self):
        """When no provider configured in production, SMS dispatch fails cleanly."""
        provider = UnconfiguredSMSProvider()
        success, msg = provider.send_otp("9876543210", "123456")
        self.assertFalse(success)
        self.assertIn("SMS service is not currently configured", msg)

    @override_settings(DEBUG=True)
    def test_console_sms_provider_prints_to_stdout_and_stderr(self):
        """ConsoleSMSProvider prints dev OTP banner to both stdout and stderr with flush."""
        import io
        import sys

        provider = ConsoleSMSProvider()
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()

        orig_stdout, orig_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = captured_stdout
            sys.stderr = captured_stderr
            success, msg = provider.send_otp("9876543210", "123456")
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr

        self.assertTrue(success)
        self.assertIn("[Dev Mode] 6-digit OTP logged to server terminal/console", msg)

        stdout_val = captured_stdout.getvalue()
        stderr_val = captured_stderr.getvalue()

        self.assertIn("[FITMEEZO DEV OTP] Mobile: 9876543210 | OTP: 123456", stdout_val)
        self.assertIn("[FITMEEZO DEV OTP] Mobile: 9876543210 | OTP: 123456", stderr_val)

    # --------------------------------------------------------------------------
    # Seller Data Isolation Tests
    # --------------------------------------------------------------------------

    @override_settings(DEBUG=True)
    def test_seller_data_isolation_after_otp_login(self):
        """Authenticated seller via OTP is strictly isolated to their own records."""
        # Create products for seller 1 and seller 2
        cat = Category.objects.create(name="Kurtis", slug="kurtis")
        p1 = Product.objects.create(
            seller=self.seller,
            category=cat,
            name="Royal Silk Kurti",
            slug="royal-silk-kurti",
            sku="RSK-001",
            price=Decimal("1500.00"),
            stock_quantity=10,
        )
        p2 = Product.objects.create(
            seller=self.seller2,
            category=cat,
            name="Second Store Saree",
            slug="second-store-saree",
            sku="SSS-001",
            price=Decimal("2500.00"),
            stock_quantity=5,
        )

        # Log seller 1 in via OTP
        generate_and_send_seller_otp("9876543210")
        raw_otp = MockSMSProvider.get_last_otp("9876543210")
        self.client.post(
            reverse("core:seller_verify_otp"),
            {"mobile": "9876543210", "otp": raw_otp},
        )

        # Visit Seller Product List
        resp = self.client.get(reverse("core:seller_product_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Royal Silk Kurti")
        self.assertNotContains(resp, "Second Store Saree")

    # --------------------------------------------------------------------------
    # Side-by-Side Password Login Regression Test
    # --------------------------------------------------------------------------

    def test_password_login_continues_to_work_seamlessly(self):
        """Existing password login at /seller/login/ operates with zero regression."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "approved_vendor",
            "password": "SecurePassword123!",
        })
        self.assertRedirects(response, reverse("core:seller_dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.approved_user.id)
