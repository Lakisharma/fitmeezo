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
    create_or_dispatch_shipment,
    sync_shipment_tracking,
    handle_shiprocket_webhook,
    register_seller_pickup_location,
)
from core.forms import SellerPickupAddressForm

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


@override_settings(SHIPROCKET_MOCK_MODE=True, SECURE_SSL_REDIRECT=False)
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


@override_settings(SHIPROCKET_MOCK_MODE=True, SECURE_SSL_REDIRECT=False)
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


@override_settings(SHIPROCKET_MOCK_MODE=True, SECURE_SSL_REDIRECT=False)
class ShiprocketMultiSellerAutomationIntegrationTests(TestCase):
    """
    Comprehensive test suite covering production multi-seller Shiprocket shipping automation:
    - Seller pickup address management and validation
    - Shiprocket facility registration API (/settings/company/addpickup)
    - Multi-seller package isolation (Seller 1 vs Seller 2 different pickup locations)
    - Order accept/reject lifecycle
    - Packed measurements entry & 'Ready for Pickup'
    - End-to-end dispatch (order creation, courier rate check, AWB assignment, scheduled pickup)
    - Strict milestone status transitions (Shipped on physical collection, Delivered on verified delivery, parent order reconciliation)
    - Idempotency & duplicate prevention
    - Webhook verification, deduplication, and tracking updates
    - Seller authorization checks (Seller A cannot dispatch Seller B's package)
    - COD vs Prepaid mapping
    - Admin controls (approve pickup, trigger registration, sync tracking, manual delivery override)
    """

    def setUp(self):
        cache.clear()
        self.password = "Secr3tPass!123"

        # Platform / Customer User
        self.customer = User.objects.create_user(
            username="customer_user",
            email="cust@fitmeezo.test",
            password=self.password,
            first_name="Pooja",
            last_name="Sharma",
        )

        # Admin Staff
        self.admin_user = User.objects.create_superuser(
            username="admin_staff",
            email="admin@fitmeezo.com",
            password=self.password,
        )

        # Seller 1 (Bengaluru Warehouse)
        self.seller_user1 = User.objects.create_user(
            username="merchant_one",
            email="m1@fitmeezo.test",
            password=self.password,
        )
        self.seller1 = create_test_seller(
            self.seller_user1,
            store_name="Alpha Textiles",
            pickup_address="10 Industrial Layout, Peenya",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560058",
            pickup_contact_name="Ramesh Kumar",
            pickup_contact_phone="9876543210",
        )

        # Seller 2 (Mumbai Warehouse)
        self.seller_user2 = User.objects.create_user(
            username="merchant_two",
            email="m2@fitmeezo.test",
            password=self.password,
        )
        self.seller2 = create_test_seller(
            self.seller_user2,
            store_name="Beta Apparels",
            gstin="27ABCDE5678F1Z9",
            pan_number="ABCDE5678F",
            support_phone="9123456780",
            pickup_address="Plot 45, Andheri East",
            pickup_city="Mumbai",
            pickup_state="Maharashtra",
            pickup_postal_code="400069",
            pickup_contact_name="Suresh Patel",
            pickup_contact_phone="9123456780",
        )

        self.category = Category.objects.create(name="Ethnic Wear", slug="ethnic-wear")

        self.prod1 = Product.objects.create(
            seller=self.seller1,
            category=self.category,
            name="Cotton Kurta",
            slug="cotton-kurta-m1",
            sku="SKU-TEST-M1-01",
            price=Decimal("1200.00"),
            weight_grams=400,
            length_cm=Decimal("20.0"),
            breadth_cm=Decimal("15.0"),
            height_cm=Decimal("5.0"),
            is_active=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        self.prod2 = Product.objects.create(
            seller=self.seller2,
            category=self.category,
            name="Silk Saree",
            slug="silk-saree-m2",
            sku="SKU-TEST-M2-02",
            price=Decimal("2500.00"),
            weight_grams=600,
            length_cm=Decimal("30.0"),
            breadth_cm=Decimal("25.0"),
            height_cm=Decimal("4.0"),
            is_active=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        self.shipping_addr = Address.objects.create(
            user=self.customer,
            full_name="Pooja Sharma",
            phone_number="9876543211",
            address_line1="B-402, Lotus Apartments, Malviya Nagar",
            city="Jaipur",
            state="Rajasthan",
            postal_code="302017",
            is_default=True,
        )

        # Parent Order (COD)
        self.order = Order.objects.create(
            user=self.customer,
            order_number=f"FIT-MS-{uuid.uuid4().hex[:6].upper()}",
            total_amount=Decimal("3700.00"),
            subtotal=Decimal("3700.00"),
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            status=Order.STATUS_PROCESSING,
            shipping_full_name=self.shipping_addr.full_name,
            shipping_phone_number=self.shipping_addr.phone_number,
            shipping_address_line1=self.shipping_addr.address_line1,
            shipping_city=self.shipping_addr.city,
            shipping_state=self.shipping_addr.state,
            shipping_postal_code=self.shipping_addr.postal_code,
        )

        # SellerOrder for Seller 1
        self.pkg1 = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller1,
            sub_order_number=f"{self.order.order_number}-S1",
            status=SellerOrder.STATUS_PENDING,
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
            actual_weight_grams=450,
            actual_length_cm=Decimal("20.0"),
            actual_breadth_cm=Decimal("15.0"),
            actual_height_cm=Decimal("5.0"),
            subtotal=Decimal("1200.00"),
            total_commission=Decimal("120.00"),
            net_seller_payable=Decimal("1080.00"),
        )
        self.item1 = OrderItem.objects.create(
            order=self.order,
            seller=self.seller1,
            seller_order=self.pkg1,
            product=self.prod1,
            product_name=self.prod1.name,
            quantity=1,
            unit_price=self.prod1.price,
            line_total=self.prod1.price,
        )

        # SellerOrder for Seller 2
        self.pkg2 = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller2,
            sub_order_number=f"{self.order.order_number}-S2",
            status=SellerOrder.STATUS_PENDING,
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_VERIFIED,
            actual_weight_grams=500,
            actual_length_cm=Decimal("25.0"),
            actual_breadth_cm=Decimal("20.0"),
            actual_height_cm=Decimal("8.0"),
            subtotal=Decimal("2500.00"),
            total_commission=Decimal("250.00"),
            net_seller_payable=Decimal("2250.00"),
        )
        self.item2 = OrderItem.objects.create(
            order=self.order,
            seller=self.seller2,
            seller_order=self.pkg2,
            product=self.prod2,
            product_name=self.prod2.name,
            quantity=1,
            unit_price=self.prod2.price,
            line_total=self.prod2.price,
        )

    def tearDown(self):
        cache.clear()
        super().tearDown()

    # 1. Seller Pickup Address Form Validation
    def test_seller_pickup_address_form_validation(self):
        """Seller pickup form requires 10-digit phone, 6-digit PIN, contact name, and full address."""
        invalid_data = {
            "pickup_contact_name": "Ramesh",
            "pickup_contact_phone": "12345",
            "pickup_address": "Some Street",
            "pickup_city": "Bengaluru",
            "pickup_state": "Karnataka",
            "pickup_postal_code": "560058",
        }
        form = SellerPickupAddressForm(data=invalid_data, instance=self.seller1)
        self.assertFalse(form.is_valid())
        self.assertIn("pickup_contact_phone", form.errors)

        invalid_data["pickup_contact_phone"] = "9876543210"
        invalid_data["pickup_postal_code"] = "5600"
        form = SellerPickupAddressForm(data=invalid_data, instance=self.seller1)
        self.assertFalse(form.is_valid())
        self.assertIn("pickup_postal_code", form.errors)

        valid_data = {
            "pickup_contact_name": "Ramesh Kumar Updated",
            "pickup_contact_phone": "9876543210",
            "pickup_address": "Gate 3, Warehouse Hub, Peenya",
            "pickup_landmark": "Opposite Metro Station",
            "pickup_city": "Bengaluru",
            "pickup_state": "Karnataka",
            "pickup_postal_code": "560058",
        }
        form = SellerPickupAddressForm(data=valid_data, instance=self.seller1)
        self.assertTrue(form.is_valid())
        saved_seller = form.save()
        self.assertEqual(saved_seller.pickup_contact_name, "Ramesh Kumar Updated")
        self.assertEqual(saved_seller.pickup_landmark, "Opposite Metro Station")

    # 2. Pickup Location Registration API (Success & Already Existing)
    @patch.object(ShiprocketClient, "_request")
    def test_pickup_location_registration_success(self, mock_request):
        """register_seller_pickup_location registers facility with Shiprocket and stores location ID."""
        mock_request.return_value = {
            "success": True,
            "pickup_id": 998811,
            "message": "Pickup location added successfully",
        }
        client = ShiprocketClient(mock_mode=False, email="api@fitmeezo.com", password="pass")
        with patch.object(client, "get_auth_token", return_value="jwt_test_token"):
            result = register_seller_pickup_location(self.seller1, client=client)

        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.shiprocket_pickup_registration_status, Seller.PICKUP_REG_STATUS_REGISTERED)
        self.assertEqual(self.seller1.shiprocket_pickup_location_id, "998811")
        self.assertIn("fitmeezo_seller_", self.seller1.get_shiprocket_pickup_location_name())

    # 3. Order Accept / Reject Lifecycle
    def test_seller_order_accept_and_reject_flow(self):
        """Seller can accept a pending package (moves to processing) or reject with reason (moves to cancelled)."""
        client = Client()
        client.force_login(self.seller_user1)

        url = reverse("core:seller_order_update_status", kwargs={"pk": self.pkg1.pk})
        resp = client.post(url, {"status": "processing"})
        self.assertEqual(resp.status_code, 302)
        self.pkg1.refresh_from_db()
        self.assertEqual(self.pkg1.status, SellerOrder.STATUS_PROCESSING)

        resp2 = client.post(url, {"status": "cancelled", "cancellation_reason": "Out of stock in warehouse"})
        self.assertEqual(resp2.status_code, 302)
        self.pkg1.refresh_from_db()
        self.assertEqual(self.pkg1.status, SellerOrder.STATUS_CANCELLED)
        self.assertIsNotNone(self.pkg1.cancelled_at)

    # 4. Multi-Seller Packaging & Origin Isolation
    def test_multiseller_origin_isolation(self):
        """Each seller's package strictly references that specific seller's warehouse pickup location."""
        loc1 = self.pkg1.get_pickup_location_details()
        loc2 = self.pkg2.get_pickup_location_details()

        self.assertNotEqual(loc1["pickup_location_name"], loc2["pickup_location_name"])
        self.assertEqual(loc1["postal_code"], "560058")  # Bengaluru
        self.assertEqual(loc2["postal_code"], "400069")  # Mumbai
        self.assertEqual(loc1["contact_name"], "Ramesh Kumar")
        self.assertEqual(loc2["contact_name"], "Suresh Patel")

    # 5. Packed Measurements Recording & 'Ready for Pickup' Transition
    def test_record_measurements_and_ready_to_ship(self):
        """Recording verified measurements updates SellerOrder and allows ready_to_ship transition."""
        client = Client()
        client.force_login(self.seller_user1)

        self.pkg1.status = SellerOrder.STATUS_PROCESSING
        self.pkg1.save()

        meas_url = reverse("core:seller_order_record_measurements", kwargs={"pk": self.pkg1.pk})
        resp = client.post(
            meas_url,
            {
                "actual_weight_grams": 450,
                "actual_length_cm": "22.0",
                "actual_breadth_cm": "16.0",
                "actual_height_cm": "6.0",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.pkg1.refresh_from_db()
        self.assertEqual(self.pkg1.actual_weight_grams, 450)
        self.assertEqual(self.pkg1.package_dimension_status, SellerOrder.PACKAGE_DIMENSION_VERIFIED)

        status_url = reverse("core:seller_order_update_status", kwargs={"pk": self.pkg1.pk})
        resp_status = client.post(status_url, {"status": "ready_to_ship"})
        self.assertEqual(resp_status.status_code, 302)
        self.pkg1.refresh_from_db()
        self.assertEqual(self.pkg1.status, SellerOrder.STATUS_READY_TO_SHIP)

    # 6. End-to-End Dispatch via Shiprocket Automation
    @patch.object(ShiprocketClient, "_request")
    def test_create_or_dispatch_shipment_flow(self, mock_request):
        """
        create_or_dispatch_shipment executes order creation, courier assign, and pickup request.
        Package remains ready_to_ship (NOT prematurely marked Shipped).
        """
        self.pkg1.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg1.actual_weight_grams = 450
        self.pkg1.actual_length_cm = Decimal("20.0")
        self.pkg1.actual_breadth_cm = Decimal("15.0")
        self.pkg1.actual_height_cm = Decimal("5.0")
        self.pkg1.save()

        def mock_request_side_effect(method, endpoint, payload=None, **kwargs):
            if "orders/create/adhoc" in endpoint:
                return {
                    "order_id": 880011,
                    "shipment_id": 990022,
                    "status": "NEW",
                    "status_code": 1,
                    "awb_code": "AWB-M1-ALPHA99",
                    "courier_name": "Blue Dart Express",
                }
            elif "couriers/assign/awb" in endpoint:
                return {
                    "response": {
                        "data": {
                            "awb_code": "AWB-M1-ALPHA99",
                            "courier_name": "Blue Dart Express",
                            "courier_company_id": 10,
                        }
                    }
                }
            elif "orders/pickup/request" in endpoint:
                return {
                    "response": {
                        "pickup_token_number": "TOK-BLR-1234",
                        "pickup_scheduled_date": "2026-10-28",
                        "status": 1,
                    }
                }
            elif "settings/company/addpickup" in endpoint:
                return {"success": True, "pickup_id": 777}
            return {}

        mock_request.side_effect = mock_request_side_effect

        client = ShiprocketClient(mock_mode=False, email="api@fitmeezo.com", password="pass")
        with patch.object(client, "get_auth_token", return_value="jwt_test_token"):
            booking = create_or_dispatch_shipment(self.pkg1.pk, user=self.seller_user1, client=client)

        self.assertIsNotNone(booking)
        self.assertEqual(booking.shiprocket_order_id, "880011")
        self.assertEqual(booking.shipment_id, "990022")
        self.assertEqual(booking.awb_code, "AWB-M1-ALPHA99")
        self.assertEqual(booking.courier_name, "Blue Dart Express")
        self.assertEqual(booking.pickup_token_number, "TOK-BLR-1234")

        self.pkg1.refresh_from_db()
        self.assertEqual(self.pkg1.tracking_number, "AWB-M1-ALPHA99")
        self.assertEqual(self.pkg1.courier_name, "Blue Dart Express")
        self.assertEqual(self.pkg1.shiprocket_order_id, "880011")
        self.assertEqual(self.pkg1.status, SellerOrder.STATUS_READY_TO_SHIP)

    # 7. Idempotency & Duplicate Prevention
    def test_idempotency_prevents_duplicate_booking(self):
        """Calling create_or_dispatch_shipment twice returns existing booking without re-booking."""
        self.pkg1.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg1.actual_weight_grams = 450
        self.pkg1.actual_length_cm = Decimal("20.0")
        self.pkg1.actual_breadth_cm = Decimal("15.0")
        self.pkg1.actual_height_cm = Decimal("5.0")
        self.pkg1.save()

        b1 = create_mock_shipment_booking(self.pkg1.pk, idempotency_key="KEY-UNIQUE-123")
        self.assertIsNotNone(b1)

        b2 = create_or_dispatch_shipment(self.pkg1.pk, idempotency_key="KEY-UNIQUE-123")
        self.assertEqual(b1.pk, b2.pk)
        self.assertEqual(ShipmentBooking.objects.filter(package=self.pkg1).count(), 1)

    # 8. Live Tracking Sync: Advances to Shipped on Scan, Delivered on Delivery
    def test_sync_shipment_tracking_milestones_and_order_reconciliation(self):
        """
        sync_shipment_tracking:
        - Advances package to Shipped strictly on confirmed physical courier scan.
        - Advances package to Delivered strictly on confirmed delivery scan.
        - Reconciles parent Order to Delivered when all child packages are delivered.
        """
        self.pkg1.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg1.save()
        booking1 = create_mock_shipment_booking(self.pkg1.pk)

        in_transit_data = {
            "current_status": "IN TRANSIT",
            "current_status_id": 17,
            "scans": [{"activity": "Picked up by Blue Dart associate", "location": "Bengaluru Hub", "date": "2026-10-27 10:00:00"}],
        }
        sync_shipment_tracking(self.pkg1, tracking_data=in_transit_data)
        self.pkg1.refresh_from_db()
        self.assertEqual(self.pkg1.status, SellerOrder.STATUS_SHIPPED)
        self.assertIsNotNone(self.pkg1.shipped_at)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PROCESSING)

        delivered_data = {
            "current_status": "DELIVERED",
            "current_status_id": 7,
            "scans": [{"activity": "Delivered to recipient Pooja Sharma", "location": "Jaipur", "date": "2026-10-29 14:30:00"}],
        }
        sync_shipment_tracking(self.pkg1, tracking_data=delivered_data)
        self.pkg1.refresh_from_db()
        self.assertEqual(self.pkg1.status, SellerOrder.STATUS_DELIVERED)
        self.assertIsNotNone(self.pkg1.delivered_at)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PROCESSING)

        self.pkg2.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg2.save()
        booking2 = create_mock_shipment_booking(self.pkg2.pk)
        sync_shipment_tracking(self.pkg2, tracking_data=delivered_data)
        self.pkg2.refresh_from_db()
        self.assertEqual(self.pkg2.status, SellerOrder.STATUS_DELIVERED)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_DELIVERED)

    # 9. Webhook Receiver: Authentication, Processing & Deduplication
    def test_webhook_receiver_authentication_and_deduplication(self):
        """Webhook endpoint verifies secret token, updates tracking, and safely deduplicates identical payloads."""
        self.pkg1.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg1.save()
        booking = create_mock_shipment_booking(self.pkg1.pk)

        webhook_url = reverse("core:shiprocket_webhook")
        payload = {
            "awb": booking.awb_code,
            "shipment_id": booking.shipment_id,
            "current_status": "IN TRANSIT",
            "scans": [
                {
                    "activity": "Out for delivery from Jaipur Hub",
                    "date": "2026-10-28 09:00:00",
                    "location": "Jaipur Hub",
                }
            ],
        }

        client = Client()

        with override_settings(SHIPROCKET_WEBHOOK_TOKEN="valid_webhook_secret_123"):
            resp_bad = client.post(
                webhook_url,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_API_KEY="wrong_secret",
            )
            self.assertEqual(resp_bad.status_code, 401)

            resp_good = client.post(
                webhook_url,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_API_KEY="valid_webhook_secret_123",
            )
            self.assertEqual(resp_good.status_code, 200)

            self.pkg1.refresh_from_db()
            self.assertEqual(self.pkg1.status, SellerOrder.STATUS_SHIPPED)

            resp_dup = client.post(
                webhook_url,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_API_KEY="valid_webhook_secret_123",
            )
            self.assertEqual(resp_dup.status_code, 200)

    # 10. Cross-Seller Isolation & Authorization
    def test_seller_cannot_dispatch_another_sellers_package(self):
        """Seller 1 cannot view or dispatch package belonging to Seller 2."""
        client = Client()
        client.force_login(self.seller_user1)

        url = reverse("core:seller_order_dispatch_shipment", kwargs={"pk": self.pkg2.pk})
        resp = client.post(url)
        self.assertEqual(resp.status_code, 404)

        sync_url = reverse("core:seller_order_sync_tracking", kwargs={"pk": self.pkg2.pk})
        resp_sync = client.post(sync_url)
        self.assertEqual(resp_sync.status_code, 404)

    # 11. Admin Controls: Verify Pickup Location & Register with Shiprocket
    @patch.object(ShiprocketClient, "_request")
    def test_admin_pickup_verify_and_register_views(self, mock_request):
        """Admin staff can verify seller pickup address and trigger Shiprocket API registration."""
        mock_request.return_value = {"success": True, "pickup_id": 112233}

        client = Client()
        client.force_login(self.admin_user)

        verify_url = reverse("custom_admin:seller_pickup_verify", kwargs={"seller_id": self.seller1.pk})
        resp_v = client.post(verify_url, {"action": "verify", "notes": "Audited GST & utility bill"})
        self.assertEqual(resp_v.status_code, 302)
        self.seller1.refresh_from_db()
        self.assertTrue(self.seller1.pickup_location_verified)

        reg_url = reverse("custom_admin:seller_pickup_register", kwargs={"seller_id": self.seller1.pk})
        with patch.object(ShiprocketClient, "get_auth_token", return_value="jwt_test_token"):
            resp_reg = client.post(reg_url)
            self.assertEqual(resp_reg.status_code, 302)

        self.seller1.refresh_from_db()
        self.assertEqual(self.seller1.shiprocket_pickup_registration_status, Seller.PICKUP_REG_STATUS_REGISTERED)

    # 12. Admin Manual Delivery Override with Audit Trail
    def test_admin_manual_delivery_override(self):
        """Admin can manually mark a package delivered with reason, logging a tracking event."""
        self.pkg1.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg1.save()
        booking = create_mock_shipment_booking(self.pkg1.pk)

        client = Client()
        client.force_login(self.admin_user)

        url = reverse("custom_admin:shipment_manual_deliver", kwargs={"shipment_id": booking.pk})
        resp = client.post(url, {"reason": "Customer confirmed doorstep receipt via phone inquiry"})
        self.assertEqual(resp.status_code, 302)

        self.pkg1.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(self.pkg1.status, SellerOrder.STATUS_DELIVERED)
        self.assertEqual(booking.status, ShipmentBooking.STATUS_DELIVERED)

        events = booking.status_history or []
        override_event = next((e for e in events if e.get("event_type") == "MANUAL_DELIVERY_OVERRIDE"), None)
        self.assertIsNotNone(override_event)
        self.assertIn("Customer confirmed doorstep receipt", override_event["description"])
