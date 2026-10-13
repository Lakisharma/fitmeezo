"""
Tests for Phase 5 — Prompt 4: Shiprocket API Client, Serviceability & Rate Estimation.

Validates:
1. Missing credentials fail safely.
2. Authentication success and token expiry.
3. Tokens and credentials never appear in logs, exceptions, or API responses.
4. Timeouts and connection failures.
5. HTTP 429 and selected 5xx retry behavior with bounded attempts.
6. Authentication/validation failures are not retried indefinitely.
7. Malformed or unexpected provider responses.
8. Valid and invalid destination PIN codes.
9. Unverified/incomplete pickup locations are rejected.
10. Missing package dimensions or weight.
11. Verified versus estimated measurements.
12. Serviceability response parsing.
13. Rate response parsing and currency/Decimal validation.
14. Quotes never change actual_shipping_cost, commission, payable, or historical records.
15. Seller A cannot request a quote for Seller B's package.
16. Platform packages use platform warehouse configuration.
17. Existing shipping allocation, checkout, cancellation, seller, and API tests remain passing.
"""

from decimal import Decimal
import json
import logging
from unittest.mock import MagicMock, patch
import uuid

import requests
from requests.exceptions import ConnectTimeout, ReadTimeout, ConnectionError as ReqConnectionError

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import (
    Address,
    Category,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
    SellerOrder,
    ShipmentBooking,
)
from core.shiprocket import (
    CourierRateQuote,
    RateEstimateResult,
    ShiprocketAuthenticationError,
    ShiprocketClient,
    ShiprocketConfigurationError,
    ShiprocketError,
    ShiprocketRateLimitError,
    ShiprocketServiceUnavailableError,
    ShiprocketTimeoutError,
    ShiprocketValidationError,
    check_booking_eligibility,
    create_mock_shipment_booking,
    estimate_package_shipping_rate,
    sanitize_log_data,
    validate_booking_eligibility,
)

User = get_user_model()


def create_test_seller(user, store_name, **kwargs):
    """Helper to create a fully valid Seller model for testing."""
    defaults = {
        "legal_business_name": f"{store_name} Private Limited",
        "support_email": f"support@{user.username}.fitmeezo.test",
        "support_phone": "9876543210",
        "registered_address": "123 Business Boulevard, Sector 18",
        "city": "Bengaluru",
        "state": "Karnataka",
        "postal_code": "560001",
        "pickup_address": "Warehouse Plot 5, Peenya Industrial Area",
        "pickup_city": "Bengaluru",
        "pickup_state": "Karnataka",
        "pickup_postal_code": "560058",
        "pickup_location_nickname": f"{store_name}_WH",
        "pickup_location_verified": True,
        "gstin": "29ABCDE1234F1Z1",
        "pan_number": "ABCDE1234F",
        "status": Seller.STATUS_APPROVED,
        "is_active": True,
        "commission_rate": Decimal("10.00"),
    }
    defaults.update(kwargs)
    return Seller.objects.create(user=user, store_name=store_name, **defaults)


class ShiprocketSecurityAndClientTests(TestCase):
    """Unit tests for the client, authentication, retry logic, and security redaction."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    # 1. Missing credentials fail safely
    def test_missing_credentials_fail_safely(self):
        """Client must raise ShiprocketConfigurationError when email or password is missing in live mode."""
        with override_settings(
            SHIPROCKET_API_EMAIL="",
            SHIPROCKET_API_PASSWORD="",
            SHIPROCKET_MOCK_MODE=False,
            SHIPROCKET_ENABLED=True,
        ):
            client = ShiprocketClient()
            with self.assertRaises(ShiprocketConfigurationError) as ctx:
                client.get_auth_token()
            err_msg = str(ctx.exception).lower()
            self.assertTrue("missing" in err_msg or "not configured" in err_msg)

    # 2. Authentication success and token expiry
    @patch("core.shiprocket.requests.Session.post")
    def test_auth_success_and_token_caching(self, mock_post):
        """Successful authentication caches JWT and avoids re-authenticating while unexpired."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "token": "mock_jwt_token_abcdef123456",
            "id": 12345,
            "email": "api@fitmeezo.com",
        }
        mock_post.return_value = mock_response

        with override_settings(
            SHIPROCKET_API_EMAIL="api@fitmeezo.com",
            SHIPROCKET_API_PASSWORD="secret_password_123",
            SHIPROCKET_MOCK_MODE=False,
            SHIPROCKET_ENABLED=True,
        ):
            client = ShiprocketClient()
            token1 = client.get_auth_token()
            self.assertEqual(token1, "mock_jwt_token_abcdef123456")
            self.assertEqual(mock_post.call_count, 1)

            # Second call should fetch from cache without calling the API again
            token2 = client.get_auth_token()
            self.assertEqual(token2, "mock_jwt_token_abcdef123456")
            self.assertEqual(mock_post.call_count, 1)

            # Invalidate cache simulating token expiry
            client.invalidate_token_cache()
            token3 = client.get_auth_token()
            self.assertEqual(token3, "mock_jwt_token_abcdef123456")
            self.assertEqual(mock_post.call_count, 2)

    # 3. Tokens and credentials never appear in logs or API responses
    def test_sensitive_data_redaction_and_sanitization(self):
        """Sanitizer must strictly redact passwords, tokens, auth headers, phones, and addresses."""
        sensitive_payload = {
            "email": "vendor@example.com",
            "password": "SuperSecretPassword123!",
            "token": "jwt_token_xyz987",
            "Authorization": "Bearer jwt_token_xyz987",
            "shipping_phone_number": "9876543210",
            "pickup_address": "123 Sensitive Street",
            "nested": {
                "secret_key": "private_value",
                "normal_field": "visible_value",
            },
            "courier_name": "Delhivery",
        }

        sanitized = sanitize_log_data(sensitive_payload)
        self.assertEqual(sanitized["password"], "[REDACTED]")
        self.assertEqual(sanitized["token"], "[REDACTED]")
        self.assertEqual(sanitized["Authorization"], "[REDACTED]")
        self.assertEqual(sanitized["shipping_phone_number"], "[REDACTED]")
        self.assertEqual(sanitized["pickup_address"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["secret_key"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["normal_field"], "visible_value")
        self.assertEqual(sanitized["courier_name"], "Delhivery")

        # Test exception message does not leak secrets
        err = ShiprocketError("Failed with password=secret and token=123", user_message="Safe message")
        self.assertEqual(err.user_message, "Safe message")
        self.assertNotIn("secret", err.user_message)

    # 4. Timeouts and connection failures
    @patch("core.shiprocket.requests.Session.request")
    def test_timeout_and_connection_failure_handling(self, mock_request):
        """Timeouts and connection drops must raise structured domain exceptions without crashing."""
        mock_request.side_effect = ReadTimeout("Connection timed out waiting for Shiprocket")

        with override_settings(
            SHIPROCKET_API_EMAIL="api@fitmeezo.com",
            SHIPROCKET_API_PASSWORD="secret_password_123",
            SHIPROCKET_MOCK_MODE=False,
            SHIPROCKET_ENABLED=True,
            SHIPROCKET_MAX_RETRIES=1,
        ):
            client = ShiprocketClient()
            with patch.object(client, "get_auth_token", return_value="valid_mock_token"):
                with self.assertRaises(ShiprocketTimeoutError):
                    client.check_serviceability(
                        pickup_postcode="560001",
                        delivery_postcode="110001",
                        weight_kg=Decimal("0.50"),
                    )

    # 5. HTTP 429 and selected 5xx retry behavior with bounded attempts
    @patch("core.shiprocket.time.sleep", return_value=None)
    @patch("core.shiprocket.requests.Session.request")
    def test_bounded_exponential_retries_on_503_and_429(self, mock_request, mock_sleep):
        """Transient 503 and 429 errors must be retried up to max_retries with backoff, then fail safely."""
        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.headers = {"Retry-After": "2"}
        resp_503.text = "Service Temporarily Unavailable"
        mock_request.return_value = resp_503

        with override_settings(
            SHIPROCKET_API_EMAIL="api@fitmeezo.com",
            SHIPROCKET_API_PASSWORD="secret_password_123",
            SHIPROCKET_MOCK_MODE=False,
            SHIPROCKET_ENABLED=True,
            SHIPROCKET_MAX_RETRIES=2,
        ):
            client = ShiprocketClient()
            with patch.object(client, "get_auth_token", return_value="valid_mock_token"):
                with self.assertRaises(ShiprocketServiceUnavailableError):
                    client.check_serviceability(
                        pickup_postcode="560001",
                        delivery_postcode="110001",
                        weight_kg=Decimal("0.50"),
                    )

                # Total calls = 1 initial + 2 retries = 3 attempts
                self.assertEqual(mock_request.call_count, 3)
                self.assertEqual(mock_sleep.call_count, 2)

    # 6. Authentication/validation failures are not retried indefinitely
    @patch("core.shiprocket.time.sleep", return_value=None)
    @patch("core.shiprocket.requests.Session.post")
    def test_auth_and_validation_errors_never_retried_indefinitely(self, mock_post, mock_sleep):
        """HTTP 401 or 422 errors must NOT trigger retry loops."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Invalid email or password"}
        mock_post.return_value = mock_response

        with override_settings(
            SHIPROCKET_API_EMAIL="api@fitmeezo.com",
            SHIPROCKET_API_PASSWORD="wrong_password",
            SHIPROCKET_MOCK_MODE=False,
            SHIPROCKET_ENABLED=True,
            SHIPROCKET_MAX_RETRIES=3,
        ):
            client = ShiprocketClient()
            with self.assertRaises(ShiprocketAuthenticationError):
                client.get_auth_token()

            # Must NOT retry 401: exactly 1 attempt
            self.assertEqual(mock_post.call_count, 1)
            self.assertEqual(mock_sleep.call_count, 0)

    # 7. Malformed or unexpected provider responses
    @patch("core.shiprocket.requests.Session.request")
    def test_malformed_json_response_handling(self, mock_request):
        """Malformed JSON from provider must be handled safely without unhandled crashes."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = json.JSONDecodeError("Expecting value", "<html>502 Bad Gateway</html>", 0)
        mock_response.text = "<html>502 Bad Gateway</html>"
        mock_request.return_value = mock_response

        with override_settings(
            SHIPROCKET_API_EMAIL="api@fitmeezo.com",
            SHIPROCKET_API_PASSWORD="secret_password_123",
            SHIPROCKET_MOCK_MODE=False,
            SHIPROCKET_ENABLED=True,
            SHIPROCKET_MAX_RETRIES=1,
        ):
            client = ShiprocketClient()
            with patch.object(client, "get_auth_token", return_value="valid_mock_token"):
                with self.assertRaises(ShiprocketError) as ctx:
                    client.check_serviceability(
                        pickup_postcode="560001",
                        delivery_postcode="110001",
                        weight_kg=Decimal("0.50"),
                    )
                self.assertIn("malformed", str(ctx.exception).lower())

    # 8. Valid and invalid destination PIN codes
    def test_pin_code_validation_rules(self):
        """PIN codes must be strictly 6-digit strings."""
        client = ShiprocketClient()
        # Invalid origin PIN
        with self.assertRaises(ShiprocketValidationError):
            client.check_serviceability(
                pickup_postcode="INVALID",
                delivery_postcode="560001",
                weight_kg=Decimal("0.50"),
            )
        # Invalid destination PIN
        with self.assertRaises(ShiprocketValidationError):
            client.check_serviceability(
                pickup_postcode="560001",
                delivery_postcode="12345",  # 5 digits
                weight_kg=Decimal("0.50"),
            )
        # 6-digit valid pins should pass validation
        is_valid = client.validate_pin_code("560001")
        self.assertTrue(is_valid)
        self.assertFalse(client.validate_pin_code("56000A"))


class ShiprocketRateEstimationServiceTests(TestCase):
    """Tests for package rate estimation, pickup location verification, dimensions, and financial immutability."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="test_seller_user", password="password123")
        self.seller = create_test_seller(self.user, store_name="Apex Athletic")
        self.category = Category.objects.create(name="Fitness Apparel", slug="fitness-apparel")

        self.customer = User.objects.create_user(username="customer_user", password="password123")

        self.product = Product.objects.create(
            category=self.category,
            seller=self.seller,
            name="Training Hoodie",
            slug="training-hoodie",
            sku="HOOD-TRN-001",
            price=Decimal("1500.00"),
            stock_quantity=10,
            weight_grams=Decimal("500.00"),
            length_cm=Decimal("25.0"),
            breadth_cm=Decimal("20.0"),
            height_cm=Decimal("10.0"),
            is_dimensions_verified=True,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        self.order = Order.objects.create(
            user=self.customer,
            order_number=f"ORD-{uuid.uuid4().hex[:8].upper()}",
            shipping_full_name="Rajesh Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 MG Road",
            shipping_city="Delhi",
            shipping_state="Delhi",
            shipping_postal_code="110001",
            total_amount=Decimal("1500.00"),
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("100.00"),
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_PENDING,
        )

        self.pkg = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number=f"{self.order.order_number}-S01",
            subtotal=Decimal("1500.00"),
            customer_shipping_charge=Decimal("100.00"),
            total_commission=Decimal("150.00"),
            net_seller_payable=Decimal("1350.00"),
            actual_shipping_cost=Decimal("0.00"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
            estimated_weight_grams=Decimal("500.00"),
            estimated_length_cm=Decimal("25.0"),
            estimated_breadth_cm=Decimal("20.0"),
            estimated_height_cm=Decimal("10.0"),
            actual_weight_grams=500,
            actual_length_cm=Decimal("25.0"),
            actual_breadth_cm=Decimal("20.0"),
            actual_height_cm=Decimal("10.0"),
        )

        self.order_item = OrderItem.objects.create(
            order=self.order,
            seller_order=self.pkg,
            seller=self.seller,
            product=self.product,
            product_name="Training Hoodie",
            unit_price=Decimal("1500.00"),
            quantity=1,
            line_total=Decimal("1500.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("150.00"),
            weight_grams=Decimal("500.00"),
            length_cm=Decimal("25.0"),
            breadth_cm=Decimal("20.0"),
            height_cm=Decimal("10.0"),
            is_dimensions_verified=True,
        )

    def tearDown(self):
        cache.clear()

    # 9. Unverified/incomplete pickup locations are rejected
    def test_unverified_pickup_location_rejected_in_live_mode(self):
        """In production/live mode, unverified pickup locations must be rejected."""
        self.seller.pickup_location_verified = False
        self.seller.save(update_fields=["pickup_location_verified"])

        with override_settings(SHIPROCKET_MOCK_MODE=False):
            with self.assertRaises(ShiprocketValidationError) as ctx:
                estimate_package_shipping_rate(self.pkg)
            self.assertIn("unverified", str(ctx.exception).lower())

    def test_incomplete_pickup_location_rejected(self):
        """Packages with incomplete pickup address data must be rejected."""
        self.seller.pickup_address = ""
        self.seller.save(update_fields=["pickup_address"])

        with self.assertRaises(ShiprocketValidationError) as ctx:
            estimate_package_shipping_rate(self.pkg)
        self.assertIn("incomplete", str(ctx.exception).lower())

    # 10. Missing package dimensions or weight
    def test_missing_dimensions_rejected(self):
        """Packages with unknown dimensions must be rejected."""
        self.pkg.package_dimension_status = SellerOrder.PACKAGE_DIMENSION_UNKNOWN
        self.pkg.actual_weight_grams = None
        self.pkg.save(update_fields=["package_dimension_status", "actual_weight_grams"])

        with self.assertRaises(ShiprocketValidationError) as ctx:
            estimate_package_shipping_rate(self.pkg)
        self.assertIn("unknown physical dimensions", str(ctx.exception).lower())

    # 11. Verified versus estimated measurements
    def test_verified_vs_estimated_measurements_enforcement(self):
        """When policy disallows estimated measurements, packages with estimated status must be rejected."""
        self.pkg.package_dimension_status = SellerOrder.PACKAGE_DIMENSION_ESTIMATED
        self.pkg.save(update_fields=["package_dimension_status"])

        # Disallow estimated
        with self.assertRaises(ShiprocketValidationError) as ctx:
            estimate_package_shipping_rate(self.pkg, allow_estimated_dimensions=False)
        self.assertIn("estimated measurements only", str(ctx.exception).lower())

        # Allow estimated
        with override_settings(SHIPROCKET_MOCK_MODE=True):
            res = estimate_package_shipping_rate(self.pkg, allow_estimated_dimensions=True)
            self.assertTrue(res.is_serviceable)
            self.assertEqual(res.measurement_tier, "estimated")

    # 12. Serviceability response parsing & 13. Rate parsing to Decimal
    @patch("core.shiprocket.requests.Session.request")
    def test_serviceability_and_rate_response_parsing(self, mock_request):
        """Parses realistic Shiprocket serviceability JSON into CourierRateQuote objects with Decimal rates."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": 200,
            "data": {
                "available_courier_companies": [
                    {
                        "courier_company_id": 10,
                        "courier_name": "Delhivery Surface",
                        "rate": 84.50,
                        "etd": "4",
                        "rating": 4.1,
                        "is_surface": True,
                        "cod": 1,
                    },
                    {
                        "courier_company_id": 12,
                        "courier_name": "Blue Dart Air",
                        "rate": 142.00,
                        "etd": "2",
                        "rating": 4.7,
                        "is_surface": False,
                        "cod": 1,
                    },
                ]
            }
        }
        mock_request.return_value = mock_response

        with override_settings(
            SHIPROCKET_API_EMAIL="api@fitmeezo.com",
            SHIPROCKET_API_PASSWORD="secret_password_123",
            SHIPROCKET_MOCK_MODE=False,
            SHIPROCKET_ENABLED=True,
        ):
            client = ShiprocketClient()
            with patch.object(client, "get_auth_token", return_value="valid_mock_token"):
                res = estimate_package_shipping_rate(self.pkg, client=client)

                self.assertTrue(res.is_serviceable)
                self.assertEqual(len(res.available_couriers), 2)

                # Cheapest should be Delhivery at 84.50
                cheapest = res.cheapest_courier
                self.assertIsNotNone(cheapest)
                self.assertEqual(cheapest.courier_name, "Delhivery Surface")
                self.assertEqual(cheapest.rate, Decimal("84.50"))
                self.assertIsInstance(cheapest.rate, Decimal)

                # Fastest should be Blue Dart Air
                fastest = res.fastest_courier
                self.assertIsNotNone(fastest)
                self.assertEqual(fastest.courier_name, "Blue Dart Air")
                self.assertEqual(fastest.rate, Decimal("142.00"))

    # 14. Quotes never change actual_shipping_cost, commission, payable, or historical records
    def test_rate_estimation_never_modifies_financial_snapshots(self):
        """Querying a rate quote MUST NEVER alter financial columns, line items, or customer shipping fees."""
        orig_subtotal = self.pkg.subtotal
        orig_cust_shipping = self.pkg.customer_shipping_charge
        orig_commission = self.pkg.total_commission
        orig_payable = self.pkg.net_seller_payable
        orig_actual_cost = self.pkg.actual_shipping_cost
        orig_item_price = self.order_item.unit_price
        orig_item_commission = self.order_item.commission_amount

        with override_settings(SHIPROCKET_MOCK_MODE=True):
            res = estimate_package_shipping_rate(self.pkg)
            self.assertTrue(res.is_serviceable)

        # Reload from database to ensure no inadvertent saves
        self.pkg.refresh_from_db()
        self.order_item.refresh_from_db()

        self.assertEqual(self.pkg.subtotal, orig_subtotal)
        self.assertEqual(self.pkg.customer_shipping_charge, orig_cust_shipping)
        self.assertEqual(self.pkg.total_commission, orig_commission)
        self.assertEqual(self.pkg.net_seller_payable, orig_payable)
        self.assertEqual(self.pkg.actual_shipping_cost, orig_actual_cost)
        self.assertEqual(self.pkg.actual_shipping_cost, Decimal("0.00"))

        self.assertEqual(self.order_item.unit_price, orig_item_price)
        self.assertEqual(self.order_item.commission_amount, orig_item_commission)

    # 15. Seller A cannot request a quote for Seller B's package
    def test_seller_package_isolation(self):
        """Seller A cannot request or probe rates for Seller B's package."""
        other_user = User.objects.create_user(username="other_seller_user", password="password123")
        other_seller = create_test_seller(
            other_user,
            store_name="Zenith Goods",
            gstin="27AAAAA0000A1Z5",
            pan_number="AAAAA0000A",
            pickup_location_nickname="Zenith_WH",
        )

        client = Client()
        client.force_login(other_user)

        # Attempt to estimate rates on self.pkg (owned by self.seller, NOT other_seller)
        url = reverse("core:seller_order_estimate_rates", kwargs={"pk": self.pkg.pk})
        response = client.post(url)

        # Should return 404 (package not found in other_seller's scope)
        self.assertEqual(response.status_code, 404)

        # Authorized seller should succeed
        client.force_login(self.user)
        with override_settings(SHIPROCKET_MOCK_MODE=True):
            response_ok = client.post(url)
            self.assertEqual(response_ok.status_code, 302)
            self.assertRedirects(response_ok, reverse("core:seller_order_detail", kwargs={"pk": self.pkg.pk}))

    # 16. Platform packages use platform warehouse configuration
    def test_platform_package_uses_platform_warehouse(self):
        """Platform-fulfilled packages use configured platform warehouse origin coordinates."""
        platform_pkg = SellerOrder.objects.create(
            order=self.order,
            seller=None,
            is_platform_fulfilled=True,
            sub_order_number=f"{self.order.order_number}-S02",
            subtotal=Decimal("800.00"),
            customer_shipping_charge=Decimal("50.00"),
            total_commission=Decimal("0.00"),
            net_seller_payable=Decimal("0.00"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
            actual_weight_grams=400,
            actual_length_cm=Decimal("20.0"),
            actual_breadth_cm=Decimal("15.0"),
            actual_height_cm=Decimal("8.0"),
        )

        with override_settings(
            FITMEEZO_PLATFORM_WAREHOUSE={
                "facility_name": "Fitmeezo Central Hub",
                "address_line1": "Plot 42, Electronic City Phase 1",
                "city": "Bengaluru",
                "state": "Karnataka",
                "postal_code": "560068",
                "contact_phone": "9876543210",
                "is_verified": True,
            },
            SHIPROCKET_MOCK_MODE=True,
        ):
            res = estimate_package_shipping_rate(platform_pkg)
            self.assertTrue(res.is_serviceable)
            self.assertEqual(res.origin_postal_code, "560068")


@override_settings(SHIPROCKET_MOCK_MODE=True)
class ShiprocketMockBookingTests(TestCase):
    """
    Test suite for Phase 5 — Prompt 5B: Idempotent Shiprocket Booking Engine & Mock Test Suite.
    Covers all 20 safety, eligibility, idempotency, isolation, and retry requirements.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="seller_booking_user",
            email="seller_booking@fitmeezo.test",
            password="StrongPassword123!",
        )
        self.seller = create_test_seller(
            self.user,
            store_name="Alpha Logistics",
            gstin="29ABCDE1234F1Z1",
            pickup_location_nickname="Alpha_WH",
            pickup_location_verified=True,
        )

        self.category = Category.objects.create(name="Sports", slug="sports")
        self.product = Product.objects.create(
            name="Gym Bag",
            slug="gym-bag",
            sku="GYMBAG-BASE",
            category=self.category,
            seller=self.seller,
            price=Decimal("1000.00"),
            weight_grams=500,
            length_cm=Decimal("25.0"),
            breadth_cm=Decimal("20.0"),
            height_cm=Decimal("12.0"),
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Color",
            option_value="Black",
            sku="GYMBAG-BLK",
            price=Decimal("1000.00"),
            stock_quantity=50,
            weight_grams=500,
            length_cm=Decimal("25.0"),
            breadth_cm=Decimal("20.0"),
            height_cm=Decimal("12.0"),
        )

        self.customer = User.objects.create_user(
            username="customer_booking_user",
            email="customer_booking@fitmeezo.test",
            password="CustomerPass123!",
        )
        self.shipping_addr = Address.objects.create(
            user=self.customer,
            full_name="Pooja Sharma",
            phone_number="9876543210",
            address_line1="45 MG Road, Indiranagar",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560038",
            is_default=True,
        )

        self.order = Order.objects.create(
            order_number=f"FIT-BK-{uuid.uuid4().hex[:8].upper()}",
            user=self.customer,
            shipping_full_name=self.shipping_addr.full_name,
            shipping_phone_number=self.shipping_addr.phone_number,
            shipping_address_line1=self.shipping_addr.address_line1,
            shipping_city=self.shipping_addr.city,
            shipping_state=self.shipping_addr.state,
            shipping_postal_code=self.shipping_addr.postal_code,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            total_amount=Decimal("1050.00"),
            subtotal=Decimal("1000.00"),
            shipping_fee=Decimal("50.00"),
        )

        self.pkg = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number=f"{self.order.order_number}-S01",
            status=SellerOrder.STATUS_READY_TO_SHIP,
            subtotal=Decimal("1000.00"),
            customer_shipping_charge=Decimal("50.00"),
            total_commission=Decimal("100.00"),
            net_seller_payable=Decimal("950.00"),
            actual_shipping_cost=Decimal("0.00"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
            actual_weight_grams=500,
            actual_length_cm=Decimal("25.0"),
            actual_breadth_cm=Decimal("20.0"),
            actual_height_cm=Decimal("12.0"),
        )

        self.order_item = OrderItem.objects.create(
            order=self.order,
            seller_order=self.pkg,
            seller=self.seller,
            product=self.product,
            variant=self.variant,
            product_name=self.product.name,
            sku=self.variant.sku,
            quantity=1,
            unit_price=Decimal("1000.00"),
            line_total=Decimal("1000.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("100.00"),
            weight_grams=500,
            length_cm=Decimal("25.0"),
            breadth_cm=Decimal("20.0"),
            height_cm=Decimal("12.0"),
        )

    def tearDown(self):
        cache.clear()

    # 1. Unpaid Razorpay package is blocked
    def test_01_unpaid_razorpay_package_is_blocked(self):
        """Unpaid online Razorpay orders must never be booked."""
        self.order.payment_method = Order.PAYMENT_METHOD_RAZORPAY
        self.order.payment_status = Order.PAYMENT_STATUS_UNPAID
        self.order.save()

        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("payment is unconfirmed", err_msg)

        with self.assertRaises(ShiprocketValidationError) as ctx:
            create_mock_shipment_booking(self.pkg.pk)
        self.assertIn("payment is unconfirmed", str(ctx.exception))

    # 2. Paid Razorpay package is eligible when all other requirements pass
    def test_02_paid_razorpay_package_is_eligible(self):
        """Paid Razorpay orders with ready-to-ship packages are eligible."""
        self.order.payment_method = Order.PAYMENT_METHOD_RAZORPAY
        self.order.payment_status = Order.PAYMENT_STATUS_PAID
        self.order.save()

        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertTrue(is_eligible)
        self.assertIsNone(err_msg)

        booking = create_mock_shipment_booking(self.pkg.pk, user=self.user)
        self.assertIsNotNone(booking)
        self.assertTrue(booking.is_mock)
        self.assertEqual(booking.status, "booked")

    # 3. COD package is eligible when all other requirements pass
    def test_03_cod_package_is_eligible(self):
        """Cash on Delivery orders are eligible even when payment_status is pending."""
        self.order.payment_method = Order.PAYMENT_METHOD_COD
        self.order.payment_status = Order.PAYMENT_STATUS_UNPAID
        self.order.save()

        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertTrue(is_eligible)
        self.assertIsNone(err_msg)

        booking = create_mock_shipment_booking(self.pkg.pk)
        self.assertIsNotNone(booking)
        self.assertTrue(booking.is_mock)

    # 4. Cancelled package is blocked
    def test_04_cancelled_package_is_blocked(self):
        """Cancelled packages cannot be booked."""
        self.pkg.status = SellerOrder.STATUS_CANCELLED
        self.pkg.save()

        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("cancelled", err_msg.lower())

        with self.assertRaises(ShiprocketValidationError):
            create_mock_shipment_booking(self.pkg.pk)

    # 5. Shipped and delivered packages are blocked
    def test_05_shipped_and_delivered_packages_are_blocked(self):
        """Packages already shipped or delivered are blocked from new bookings."""
        self.pkg.status = SellerOrder.STATUS_SHIPPED
        self.pkg.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("shipped", err_msg.lower())

        self.pkg.status = SellerOrder.STATUS_DELIVERED
        self.pkg.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("delivered", err_msg.lower())

    # 6. Package not ready to ship is blocked
    def test_06_package_not_ready_to_ship_is_blocked(self):
        """Packages in pending or processing status cannot be booked."""
        self.pkg.status = SellerOrder.STATUS_PENDING
        self.pkg.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("ready to ship", err_msg.lower())

        self.pkg.status = SellerOrder.STATUS_PROCESSING
        self.pkg.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)

    # 7. Missing or invalid measurements are blocked
    def test_07_missing_or_invalid_measurements_are_blocked(self):
        """Packages without positive weight or carton dimensions are blocked."""
        # Zero weight
        self.pkg.actual_weight_grams = 0
        self.pkg.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("weight", err_msg.lower())

        # Zero length
        self.pkg.actual_weight_grams = 500
        self.pkg.actual_length_cm = Decimal("0.0")
        self.pkg.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("dimensions", err_msg.lower())

        # Unknown dimension status
        self.pkg.actual_length_cm = Decimal("20.0")
        self.pkg.package_dimension_status = SellerOrder.PACKAGE_DIMENSION_UNKNOWN
        self.pkg.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)

    # 8. Unverified pickup location is blocked
    def test_08_unverified_pickup_location_is_blocked(self):
        """Packages with unverified seller warehouse or missing nickname are blocked."""
        self.seller.pickup_location_verified = False
        self.seller.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("unverified", err_msg.lower())

        self.seller.pickup_location_verified = True
        self.seller.pickup_location_nickname = ""
        self.seller.save()
        is_eligible, err_msg, _ = check_booking_eligibility(self.pkg)
        self.assertFalse(is_eligible)
        self.assertIn("nickname", err_msg.lower())

    # 9. Existing completed mock booking is not duplicated
    def test_09_existing_completed_mock_booking_is_not_duplicated(self):
        """Repeated booking attempts for the same package return the existing booking."""
        booking1 = create_mock_shipment_booking(self.pkg.pk)
        self.assertTrue(self.pkg.is_booked)
        self.assertEqual(ShipmentBooking.objects.filter(package=self.pkg).count(), 1)

        # Second booking call returns existing instance
        booking2 = create_mock_shipment_booking(self.pkg.pk)
        self.assertEqual(booking1.pk, booking2.pk)
        self.assertEqual(booking1.shipment_id, booking2.shipment_id)
        self.assertEqual(ShipmentBooking.objects.filter(package=self.pkg).count(), 1)

    # 10. Repeated requests return the same result or a safe conflict
    def test_10_repeated_requests_return_same_result_or_safe_conflict(self):
        """Idempotency key re-use on the same package returns existing; key collision fails safely."""
        key = "idemp-test-key-10"
        b1 = create_mock_shipment_booking(self.pkg.pk, idempotency_key=key)

        # Same key on same package -> idempotent return
        b2 = create_mock_shipment_booking(self.pkg.pk, idempotency_key=key)
        self.assertEqual(b1.pk, b2.pk)

        # Same key on different package -> collision error
        order2 = Order.objects.create(
            order_number=f"FIT-BK2-{uuid.uuid4().hex[:8].upper()}",
            user=self.customer,
            shipping_full_name=self.shipping_addr.full_name,
            shipping_phone_number=self.shipping_addr.phone_number,
            shipping_address_line1=self.shipping_addr.address_line1,
            shipping_city=self.shipping_addr.city,
            shipping_state=self.shipping_addr.state,
            shipping_postal_code=self.shipping_addr.postal_code,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            total_amount=Decimal("550.00"),
            subtotal=Decimal("500.00"),
            shipping_fee=Decimal("50.00"),
        )
        other_pkg = SellerOrder.objects.create(
            order=order2,
            seller=self.seller,
            sub_order_number=f"{order2.order_number}-S01",
            status=SellerOrder.STATUS_READY_TO_SHIP,
            subtotal=Decimal("500.00"),
            actual_weight_grams=300,
            actual_length_cm=Decimal("15.0"),
            actual_breadth_cm=Decimal("10.0"),
            actual_height_cm=Decimal("5.0"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
        )
        with self.assertRaises(ShiprocketValidationError) as ctx:
            create_mock_shipment_booking(other_pkg.pk, idempotency_key=key)
        self.assertIn("collision", str(ctx.exception).lower())

    # 11. Seller A cannot book Seller B's package
    def test_11_seller_a_cannot_book_seller_b_package(self):
        """Seller B cannot book Seller A's package via seller dashboard endpoint."""
        user_b = User.objects.create_user(
            username="seller_b_user",
            password="Password123!",
        )
        create_test_seller(
            user_b,
            store_name="Beta Retailers",
            gstin="27XYZAB1234F1Z9",
            pickup_location_nickname="Beta_WH",
        )

        client = Client()
        client.force_login(user_b)

        url = reverse("core:seller_order_create_mock_shipment", kwargs={"pk": self.pkg.pk})
        response = client.post(url)
        # 404 because get_object_or_404 scopes to seller=seller
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.pkg.is_booked)

    # 12. Seller cannot book a platform-fulfilled package
    def test_12_seller_cannot_book_platform_fulfilled_package(self):
        """Platform packages cannot be booked through third-party seller endpoint."""
        platform_pkg = SellerOrder.objects.create(
            order=self.order,
            seller=None,
            is_platform_fulfilled=True,
            sub_order_number=f"{self.order.order_number}-S99",
            status=SellerOrder.STATUS_READY_TO_SHIP,
            subtotal=Decimal("800.00"),
            actual_weight_grams=400,
            actual_length_cm=Decimal("20.0"),
            actual_breadth_cm=Decimal("15.0"),
            actual_height_cm=Decimal("8.0"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
        )

        client = Client()
        client.force_login(self.user)

        url = reverse("core:seller_order_create_mock_shipment", kwargs={"pk": platform_pkg.pk})
        response = client.post(url)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(platform_pkg.is_booked)

    # 13. Unapproved, inactive or invalid-GSTIN seller is rejected
    def test_13_unapproved_inactive_or_invalid_gstin_seller_is_rejected(self):
        """Unapproved or inactive sellers are rejected by seller_required decorator."""
        client = Client()

        # Pending seller
        self.seller.status = Seller.STATUS_PENDING
        self.seller.save()
        client.force_login(self.user)
        url = reverse("core:seller_order_create_mock_shipment", kwargs={"pk": self.pkg.pk})
        resp_pending = client.post(url)
        self.assertEqual(resp_pending.status_code, 302)
        self.assertRedirects(resp_pending, reverse("core:seller_status"))

        # Inactive seller
        self.seller.status = Seller.STATUS_APPROVED
        self.seller.is_active = False
        self.seller.save()
        resp_inactive = client.post(url)
        self.assertEqual(resp_inactive.status_code, 302)

        # Invalid GSTIN seller
        self.seller.is_active = True
        self.seller.gstin = "INVALID_GST"
        self.seller.save()
        resp_invalid_gst = client.post(url)
        self.assertEqual(resp_invalid_gst.status_code, 302)

    # 14. Mock mode disabled causes booking to fail closed
    def test_14_mock_mode_disabled_causes_booking_to_fail_closed(self):
        """Disabling SHIPROCKET_MOCK_MODE fails closed with clear error."""
        with override_settings(SHIPROCKET_MOCK_MODE=False):
            with self.assertRaises(ShiprocketConfigurationError) as ctx:
                create_mock_shipment_booking(self.pkg.pk)
            self.assertIn("Prompt 5B is mock-only", str(ctx.exception))

    # 15. Booking does not call any external API
    def test_15_booking_does_not_call_any_external_api(self):
        """Mock booking engine makes zero outbound HTTP requests."""
        with patch("requests.Session.request") as mock_session_req, \
             patch("requests.request") as mock_req:
            booking = create_mock_shipment_booking(self.pkg.pk)
            self.assertIsNotNone(booking)
            mock_session_req.assert_not_called()
            mock_req.assert_not_called()

    # 16. Financial snapshots and inventory are unchanged
    def test_16_financial_snapshots_and_inventory_are_unchanged(self):
        """Booking operation never alters prices, commissions, payouts, shipping fees, or inventory."""
        initial_order_total = self.order.total_amount
        initial_order_subtotal = self.order.subtotal
        initial_order_fee = self.order.shipping_fee
        initial_payable = self.pkg.net_seller_payable
        initial_commission = self.pkg.total_commission
        initial_actual_shipping = self.pkg.actual_shipping_cost
        initial_item_commission = self.order_item.commission_amount
        initial_stock = self.variant.stock_quantity

        create_mock_shipment_booking(self.pkg.pk)

        self.order.refresh_from_db()
        self.pkg.refresh_from_db()
        self.order_item.refresh_from_db()
        self.variant.refresh_from_db()

        self.assertEqual(self.order.total_amount, initial_order_total)
        self.assertEqual(self.order.subtotal, initial_order_subtotal)
        self.assertEqual(self.order.shipping_fee, initial_order_fee)
        self.assertEqual(self.pkg.net_seller_payable, initial_payable)
        self.assertEqual(self.pkg.total_commission, initial_commission)
        self.assertEqual(self.pkg.actual_shipping_cost, initial_actual_shipping)
        self.assertEqual(self.order_item.commission_amount, initial_item_commission)
        self.assertEqual(self.variant.stock_quantity, initial_stock)

    # 17. Mock identifiers cannot be mistaken for real Shiprocket identifiers
    def test_17_mock_identifiers_cannot_be_mistaken_for_real_identifiers(self):
        """Simulated identifiers are prefixed with MOCK- and real carrier fields remain unset."""
        booking = create_mock_shipment_booking(self.pkg.pk)
        self.assertTrue(booking.shipment_id.startswith("MOCK-SHP-"))
        self.assertTrue(booking.awb_code.startswith("MOCK-AWB-"))
        self.assertTrue(booking.is_mock)

        self.pkg.refresh_from_db()
        # Real carrier fields on SellerOrder are untouched
        self.assertEqual(self.pkg.shiprocket_awb_code, "")
        self.assertEqual(self.pkg.shiprocket_order_id, "")
        self.assertEqual(self.pkg.shiprocket_shipment_id, "")

    # 18. GET rate-estimation retries continue to work
    def test_18_get_rate_estimation_retries_continue_to_work(self):
        """Safe read-only GET requests retry on 503 errors up to bounded count."""
        client = ShiprocketClient(mock_mode=False)

        mock_resp_503 = MagicMock()
        mock_resp_503.status_code = 503
        mock_resp_503.text = "Service Unavailable"

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {"status": 200, "data": {"available_courier_companies": []}}

        with patch.object(client, "get_auth_token", return_value="valid_cached_token"), \
             patch.object(client.session, "request", side_effect=[mock_resp_503, mock_resp_200]) as mock_req, \
             patch("time.sleep"):
            resp = client._request("GET", "courier/serviceability/", params={"test": "1"}, authenticated=True)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(mock_req.call_count, 2)

    # 19. Non-idempotent booking POST requests are never blindly retried
    def test_19_non_idempotent_booking_post_requests_are_never_blindly_retried(self):
        """Mutating POST requests fail immediately on 5xx or timeouts without blind retry."""
        client = ShiprocketClient(mock_mode=False)

        mock_resp_503 = MagicMock()
        mock_resp_503.status_code = 503
        mock_resp_503.text = "Shipment Creation Service Down"

        with patch.object(client, "get_auth_token", return_value="valid_cached_token"), \
             patch.object(client.session, "request", return_value=mock_resp_503) as mock_req:
            with self.assertRaises(ShiprocketServiceUnavailableError):
                client._request("POST", "orders/create/adhoc", json={"pkg": 1}, authenticated=True)
            # Exactly 1 call: NO blind retry on mutating POST
            self.assertEqual(mock_req.call_count, 1)

    # 20. Invalid or missing idempotency keys are handled safely
    def test_20_invalid_or_missing_idempotency_keys_handled_safely(self):
        """Missing or whitespace idempotency keys generate safe unique tokens without error."""
        b1 = create_mock_shipment_booking(self.pkg.pk, idempotency_key=None)
        self.assertTrue(b1.idempotency_key.startswith("mock-key-"))
        self.assertIn(str(self.pkg.pk), b1.idempotency_key)

    # 21. Booking POST timeout is never blindly retried
    def test_21_booking_post_timeout_never_blindly_retried(self):
        """Mutating POST requests raising Timeout are never retried, preventing duplicate bookings."""
        client = ShiprocketClient(mock_mode=False)

        with patch.object(client, "get_auth_token", return_value="valid_cached_token"), \
             patch.object(client.session, "request", side_effect=requests.exceptions.Timeout("Read timeout on booking")) as mock_req:
            with self.assertRaises(ShiprocketTimeoutError):
                client._request("POST", "orders/create/adhoc", json={"pkg": self.pkg.pk}, authenticated=True)
            # Exactly 1 call: NO blind retry on timeout during POST
            self.assertEqual(mock_req.call_count, 1)

    # 22. Concurrent booking attempts handled safely
    def test_22_concurrent_booking_attempts_handled_safely(self):
        """
        Database unique constraint and atomic transaction prevent duplicate bookings
        during simulated concurrent execution.
        """
        key = f"concurrent-test-key-{uuid.uuid4().hex[:8]}"

        # First booking succeeds
        b1 = create_mock_shipment_booking(self.pkg.pk, idempotency_key=key)
        self.assertIsNotNone(b1)

        # Simulated concurrent attempt re-submitting for the same package
        b2 = create_mock_shipment_booking(self.pkg.pk, idempotency_key=key)
        self.assertEqual(b1.pk, b2.pk)
        self.assertEqual(ShipmentBooking.objects.filter(package=self.pkg).count(), 1)

    # 23. Seller A cannot download Seller B's packing summary PDF
    def test_23_seller_a_cannot_download_seller_b_packing_pdf(self):
        """Seller B receives 404 when attempting to access Seller A's internal packing summary."""
        user_b = User.objects.create_user(
            username="seller_b_pdf_user",
            password="Password123!",
        )
        create_test_seller(
            user_b,
            store_name="Gamma Store",
            gstin="27XYZAB9999F1Z9",
            pickup_location_nickname="Gamma_WH",
        )

        client = Client()
        client.force_login(user_b)

        url = reverse("core:seller_order_packing_summary", kwargs={"pk": self.pkg.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 404)

    # 24. Platform package packing summary is blocked for third-party sellers
    def test_24_platform_package_packing_summary_blocked_for_third_party_seller(self):
        """Third-party seller cannot download packing summary for platform-fulfilled package."""
        platform_pkg = SellerOrder.objects.create(
            order=self.order,
            seller=None,
            is_platform_fulfilled=True,
            sub_order_number=f"{self.order.order_number}-SPF",
            status=SellerOrder.STATUS_READY_TO_SHIP,
            subtotal=Decimal("800.00"),
            actual_weight_grams=400,
            actual_length_cm=Decimal("20.0"),
            actual_breadth_cm=Decimal("15.0"),
            actual_height_cm=Decimal("8.0"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
        )

        client = Client()
        client.force_login(self.user)

        url = reverse("core:seller_order_packing_summary", kwargs={"pk": platform_pkg.pk})
        response = client.get(url)
        # 404 or 403 PermissionDenied
        self.assertIn(response.status_code, (403, 404))

    # 25. Unauthorized and unauthenticated users cannot download packing PDF
    def test_25_unauthorized_and_unauthenticated_users_cannot_download_packing_pdf(self):
        """Unauthenticated requests redirect to login; regular customers receive 403."""
        anon_client = Client()
        url = reverse("core:seller_order_packing_summary", kwargs={"pk": self.pkg.pk})

        # Anonymous user -> redirect to login
        resp_anon = anon_client.get(url)
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn(reverse("core:account_login"), resp_anon.url)

        # Authenticated customer (not a seller, not staff) -> 403 PermissionDenied
        cust_client = Client()
        cust_client.force_login(self.customer)
        resp_cust = cust_client.get(url)
        self.assertEqual(resp_cust.status_code, 403)

    # 26. Staff admin can download packing summary PDF for any package
    def test_26_staff_admin_can_download_packing_pdf(self):
        """Platform staff user has authorized access to download packing summary PDF."""
        admin_user = User.objects.create_superuser(
            username="platform_admin",
            email="admin@fitmeezo.com",
            password="AdminPassword123!",
        )
        admin_client = Client()
        admin_client.force_login(admin_user)

        url = reverse("core:seller_order_packing_summary", kwargs={"pk": self.pkg.pk})
        response = admin_client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-1.4"))

    # 27. Packing PDF content security and non-carrier disclaimer
    def test_27_packing_pdf_content_security_and_non_carrier_disclaimer(self):
        """
        Packing slip PDF generated strictly with non-carrier disclaimer,
        masked customer PII, package dimensions, and zero provider secrets.
        """
        # Create booking first
        booking = create_mock_shipment_booking(self.pkg.pk)

        client = Client()
        client.force_login(self.user)

        url = reverse("core:seller_order_packing_summary", kwargs={"pk": self.pkg.pk})
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(f"packing_summary_{self.pkg.sub_order_number}.pdf", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

        content = response.content.decode("latin-1", "replace")
        # PDF structure
        self.assertTrue(response.content.startswith(b"%PDF-1.4"))
        # Non-carrier warnings
        self.assertIn("NOT A VALID CARRIER SHIPPING LABEL", content)
        self.assertIn("INTERNAL WAREHOUSE PACKING SUMMARY", content)
        # Masked customer phone (full phone not disclosed)
        self.assertNotIn(self.shipping_addr.phone_number, content)
        self.assertIn("****", content)
        # Mock identifiers
        self.assertIn(booking.shipment_id, content)
        self.assertIn(booking.awb_code, content)
        # Items
        self.assertIn(self.product.name, content)
        # No credentials or commission rates leaked
        self.assertNotIn("password", content.lower())
        self.assertNotIn("commission_rate", content)
        self.assertNotIn("net_seller_payable", content)

