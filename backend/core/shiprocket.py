"""
Fitmeezo Shiprocket Integration Layer (Phase 5 — Prompt 4)

Provides a secure, testable, read-only API client and package-level shipping
rate estimation service for Shiprocket API v2.

CRITICAL DIRECTIVES:
- This is a READ-ONLY integration module.
- Absolutely NO shipment booking, order creation in Shiprocket, AWB generation,
  pickup scheduling, label generation, or cancellations are implemented.
- Sensitive credentials, auth tokens, passwords, customer phone numbers,
  and complete street addresses are strictly sanitized and never printed or logged.
- Bounded exponential backoff is used for transient network errors (HTTP 429, 5xx).
  Authentication/validation errors (400, 401, 422) are never retried indefinitely.
- Quote estimates are strictly separated from financial accounting: quotes NEVER
  alter `actual_shipping_cost`, `total_commission`, `net_seller_payable`,
  customer shipping fees, or historical line-item snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import logging
import os
import re
import time
from typing import Any
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
import requests

from core.shipping_dimensions import PIN_CODE_PATTERN

logger = logging.getLogger("core.shiprocket")


# ==============================================================================
# SENSITIVE KEY SANITIZATION & LOGGING HELPERS
# ==============================================================================

SENSITIVE_KEYS = {
    "password",
    "token",
    "authorization",
    "secret",
    "api_key",
    "email",
    "support_email",
    "phone",
    "contact_phone",
    "support_phone",
    "shipping_phone_number",
    "address",
    "address_line1",
    "address_line2",
    "street",
}


def sanitize_log_data(data: Any) -> Any:
    """
    Recursively redacts credentials, authentication tokens, passwords,
    and private contact info from dictionaries or lists before logging.
    """
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            key_lower = str(k).lower()
            if any(sens in key_lower for sens in SENSITIVE_KEYS):
                sanitized[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                sanitized[k] = sanitize_log_data(v)
            else:
                sanitized[k] = v
        return sanitized
    elif isinstance(data, list):
        return [sanitize_log_data(item) for item in data]
    return data


# ==============================================================================
# CUSTOM SHIPROCKET EXCEPTIONS
# ==============================================================================

class ShiprocketError(Exception):
    """Base exception for all Shiprocket client and rate-estimation operations."""
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_data: Any = None,
        user_message: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_data = sanitize_log_data(response_data) if response_data else None
        self.user_message = user_message or "An error occurred while communicating with the shipping provider."


class ShiprocketConfigurationError(ShiprocketError):
    """Raised when mandatory provider credentials or base URLs are missing or invalid."""
    pass


class ShiprocketAuthenticationError(ShiprocketError):
    """Raised when authentication fails (HTTP 401/403 or invalid credentials)."""
    pass


class ShiprocketRateLimitError(ShiprocketError):
    """Raised when provider rate limits are exceeded (HTTP 429) after retry attempts."""
    def __init__(self, message: str, retry_after: int | None = None, response_data: Any = None):
        super().__init__(message, status_code=429, response_data=response_data)
        self.retry_after = retry_after


class ShiprocketServiceUnavailableError(ShiprocketError):
    """Raised when provider server returns 5xx error after all retry attempts."""
    pass


class ShiprocketTimeoutError(ShiprocketError):
    """Raised when network connection or read timeout occurs."""
    pass


class ShiprocketValidationError(ShiprocketError):
    """Raised when request payload or parameters are invalid or unprocessable (HTTP 400/422)."""
    pass


class ShiprocketServiceabilityError(ShiprocketError):
    """Raised when origin/destination route is not serviceable by any courier."""
    pass


# ==============================================================================
# DATA TRANSFER OBJECTS (DATACLASSES)
# ==============================================================================

@dataclass
class CourierRateQuote:
    """Represents an individual courier's estimated delivery timeline and shipping fee."""
    courier_company_id: int
    courier_name: str
    rate: Decimal
    etd_days: int | None = None
    etd_display: str = ""
    cod_available: bool = False
    is_surface: bool = True
    rating: float | None = None
    min_weight_kg: Decimal | None = None
    chargeable_weight_kg: Decimal = Decimal("0.00")
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "courier_company_id": self.courier_company_id,
            "courier_name": self.courier_name,
            "rate": str(self.rate),
            "etd_days": self.etd_days,
            "etd_display": self.etd_display,
            "cod_available": self.cod_available,
            "is_surface": self.is_surface,
            "rating": self.rating,
            "chargeable_weight_kg": str(self.chargeable_weight_kg),
        }


@dataclass
class RateEstimateResult:
    """Represents a validated courier serviceability and rate estimation response."""
    is_serviceable: bool
    origin_postal_code: str
    destination_postal_code: str
    chargeable_weight_kg: Decimal
    measurement_tier: str  # "verified" or "estimated"
    available_couriers: list[CourierRateQuote] = field(default_factory=list)
    cheapest_courier: CourierRateQuote | None = None
    fastest_courier: CourierRateQuote | None = None
    recommended_courier: CourierRateQuote | None = None
    is_mock: bool = False
    message: str = ""
    error: str | None = None
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "is_serviceable": self.is_serviceable,
            "origin_postal_code": self.origin_postal_code,
            "destination_postal_code": self.destination_postal_code,
            "chargeable_weight_kg": str(self.chargeable_weight_kg),
            "measurement_tier": self.measurement_tier,
            "courier_count": len(self.available_couriers),
            "cheapest_courier": self.cheapest_courier.to_dict() if self.cheapest_courier else None,
            "fastest_courier": self.fastest_courier.to_dict() if self.fastest_courier else None,
            "available_couriers": [c.to_dict() for c in self.available_couriers],
            "is_mock": self.is_mock,
            "message": self.message,
            "error": self.error,
        }


# ==============================================================================
# SHIPROCKET API CLIENT
# ==============================================================================

class ShiprocketClient:
    """
    Secure HTTP Client for Shiprocket API v2.
    Supports:
    - Authentication and safe JWT token caching with automatic expiry recovery.
    - Courier serviceability and shipping rate lookup.
    - Bounded exponential backoff on transient 429 and 5xx errors.
    - Offline mock simulation mode for zero-cost testing.
    """

    CACHE_KEY_AUTH_TOKEN = "shiprocket_auth_jwt_token"

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        password: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        max_retries: int | None = None,
        backoff_factor: float | None = None,
        mock_mode: bool | None = None,
        session: requests.Session | None = None,
    ):
        self.base_url = (base_url or getattr(settings, "SHIPROCKET_API_BASE_URL", "https://apiv2.shiprocket.in/v2/console")).rstrip("/")
        self.email = email if email is not None else getattr(settings, "SHIPROCKET_API_EMAIL", "")
        self.password = password if password is not None else getattr(settings, "SHIPROCKET_API_PASSWORD", "")
        self.connect_timeout = connect_timeout or getattr(settings, "SHIPROCKET_CONNECT_TIMEOUT", 5.0)
        self.read_timeout = read_timeout or getattr(settings, "SHIPROCKET_READ_TIMEOUT", 15.0)
        self.max_retries = max_retries if max_retries is not None else getattr(settings, "SHIPROCKET_MAX_RETRIES", 3)
        self.backoff_factor = backoff_factor if backoff_factor is not None else getattr(settings, "SHIPROCKET_BACKOFF_FACTOR", 0.5)
        self.mock_mode = mock_mode if mock_mode is not None else getattr(settings, "SHIPROCKET_MOCK_MODE", False)
        self.token_cache_timeout = getattr(settings, "SHIPROCKET_TOKEN_CACHE_TIMEOUT", 86400 * 7)
        self.session = session or requests.Session()

    @property
    def is_configured(self) -> bool:
        """True if credentials are non-empty and non-placeholder."""
        if not self.email or not self.password:
            return False
        clean_email = self.email.lower()
        clean_pass = self.password.lower()
        return not (
            clean_email.startswith(("replace_", "your_", "todo"))
            or clean_pass.startswith(("replace_", "your_", "todo"))
            or clean_email in ("replace_with_shiprocket_email@example.com",)
        )

    @staticmethod
    def validate_pin_code(pin_code: str) -> bool:
        """Validates that a postal PIN code is a valid 6-digit Indian PIN."""
        if not pin_code or not isinstance(pin_code, str):
            return False
        return bool(PIN_CODE_PATTERN.match(pin_code.strip()))

    # --------------------------------------------------------------------------
    # Authentication & Token Management
    # --------------------------------------------------------------------------

    def invalidate_token_cache(self) -> None:
        """Clears the cached JWT token forcing re-authentication on next request."""
        cache.delete(self.CACHE_KEY_AUTH_TOKEN)

    def get_auth_token(self, force_refresh: bool = False) -> str:
        """
        Retrieves active JWT authentication token.
        Checks Django cache first; fetches fresh token on cache miss or force_refresh.
        Never logs or exposes the acquired token.
        """
        if self.mock_mode:
            return "mock_shiprocket_jwt_token_for_testing"

        if not self.is_configured:
            raise ShiprocketConfigurationError(
                "Shiprocket API credentials are not configured or are set to placeholder values. "
                "Configure SHIPROCKET_API_EMAIL and SHIPROCKET_API_PASSWORD in environment."
            )

        if not force_refresh:
            cached_token = cache.get(self.CACHE_KEY_AUTH_TOKEN)
            if cached_token:
                return cached_token

        return self._authenticate()

    def _authenticate(self) -> str:
        """
        Executes POST /auth/login with provider email and password.
        Caches resulting token safely upon success.
        """
        url = f"{self.base_url}/auth/login"
        payload = {"email": self.email, "password": self.password}

        logger.info("Requesting fresh Shiprocket authentication token from provider.")
        try:
            resp = self.session.post(
                url,
                json=payload,
                timeout=(self.connect_timeout, self.read_timeout),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except requests.exceptions.Timeout as exc:
            logger.error("Shiprocket authentication timed out.")
            raise ShiprocketTimeoutError(f"Connection timeout during Shiprocket authentication: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("Shiprocket authentication connection failure.")
            raise ShiprocketServiceUnavailableError(f"Network error during Shiprocket authentication: {exc}") from exc
        except Exception as exc:
            raise ShiprocketError(f"Unexpected error during Shiprocket authentication: {exc}") from exc

        if resp.status_code in (401, 403):
            logger.warning("Shiprocket authentication failed: HTTP %s.", resp.status_code)
            raise ShiprocketAuthenticationError(
                "Invalid Shiprocket credentials. Provider rejected email/password.",
                status_code=resp.status_code,
            )

        if resp.status_code != 200:
            logger.warning("Shiprocket auth endpoint returned status %s.", resp.status_code)
            raise ShiprocketError(
                f"Authentication failed with status {resp.status_code}.",
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ShiprocketError("Authentication response contained malformed JSON.") from exc

        token = data.get("token")
        if not token:
            raise ShiprocketAuthenticationError("Authentication response did not contain a valid 'token' attribute.")

        cache.set(self.CACHE_KEY_AUTH_TOKEN, token, self.token_cache_timeout)
        logger.info("Successfully acquired and safely cached Shiprocket authentication token.")
        return token

    # --------------------------------------------------------------------------
    # Bounded Exponential Retry Request Engine
    # --------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json: dict | None = None,
        authenticated: bool = True,
        retry_count: int = 0,
    ) -> requests.Response:
        """
        Executes HTTP request with bounded retries for transient errors (429, 5xx, timeouts).
        Automatically handles 401 token invalidation once.
        Sanitizes request logging.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {"Accept": "application/json"}

        if authenticated:
            token = self.get_auth_token()
            headers["Authorization"] = f"Bearer {token}"

        clean_params = sanitize_log_data(params) if params else None
        logger.debug(
            "Executing Shiprocket %s %s (attempt %d/%d) params=%s",
            method,
            endpoint,
            retry_count + 1,
            self.max_retries,
            clean_params,
        )

        try:
            resp = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except requests.exceptions.Timeout as exc:
            is_idempotent = method.upper() in ("GET", "HEAD")
            if is_idempotent and retry_count < self.max_retries:
                backoff = self.backoff_factor * (2 ** retry_count)
                logger.warning("Shiprocket timeout on %s. Retrying in %.2fs (attempt %d).", endpoint, backoff, retry_count + 1)
                time.sleep(backoff)
                return self._request(method, endpoint, params, json, authenticated, retry_count + 1)
            raise ShiprocketTimeoutError(f"Request to {endpoint} timed out after {retry_count + 1} attempts.") from exc
        except requests.exceptions.ConnectionError as exc:
            is_idempotent = method.upper() in ("GET", "HEAD")
            if is_idempotent and retry_count < self.max_retries:
                backoff = self.backoff_factor * (2 ** retry_count)
                logger.warning("Shiprocket connection error on %s. Retrying in %.2fs (attempt %d).", endpoint, backoff, retry_count + 1)
                time.sleep(backoff)
                return self._request(method, endpoint, params, json, authenticated, retry_count + 1)
            raise ShiprocketServiceUnavailableError(f"Network connection failed after {retry_count + 1} attempts.") from exc

        # Handle 401 Unauthorized (single token cache invalidation & retry)
        if resp.status_code == 401 and authenticated and retry_count == 0:
            logger.info("Shiprocket token expired or rejected (HTTP 401). Invalidating cache and retrying.")
            cache.delete(self.CACHE_KEY_AUTH_TOKEN)
            return self._request(method, endpoint, params, json, authenticated=True, retry_count=retry_count + 1)

        # Handle 429 Rate Limit
        if resp.status_code == 429:
            retry_after_hdr = resp.headers.get("Retry-After")
            try:
                retry_after_val = int(retry_after_hdr) if retry_after_hdr else None
            except (ValueError, TypeError):
                retry_after_val = None

            is_idempotent = method.upper() in ("GET", "HEAD")
            if is_idempotent and retry_count < self.max_retries:
                backoff = float(retry_after_val) if retry_after_val is not None else (self.backoff_factor * (2 ** retry_count))
                backoff = min(backoff, 10.0)  # Bound sleep duration
                logger.warning("Shiprocket rate limit (HTTP 429). Backing off for %.2fs.", backoff)
                time.sleep(backoff)
                return self._request(method, endpoint, params, json, authenticated, retry_count + 1)

            raise ShiprocketRateLimitError(
                "Shiprocket API rate limit exceeded after maximum retry attempts.",
                retry_after=retry_after_val,
                response_data=resp.text,
            )

        # Handle 5xx Transient Server Errors
        retry_statuses = getattr(settings, "SHIPROCKET_RETRY_STATUS_CODES", (429, 500, 502, 503, 504))
        if resp.status_code in retry_statuses and resp.status_code >= 500:
            is_idempotent = method.upper() in ("GET", "HEAD")
            if is_idempotent and retry_count < self.max_retries:
                backoff = self.backoff_factor * (2 ** retry_count)
                logger.warning("Shiprocket 5xx error (%s). Retrying in %.2fs.", resp.status_code, backoff)
                time.sleep(backoff)
                return self._request(method, endpoint, params, json, authenticated, retry_count + 1)

            raise ShiprocketServiceUnavailableError(
                f"Shiprocket server error HTTP {resp.status_code} after {retry_count + 1} retries.",
                status_code=resp.status_code,
                response_data=resp.text,
            )

        # Do NOT retry 400 or 422 validation errors indefinitely
        if resp.status_code in (400, 422):
            try:
                err_data = resp.json()
            except Exception:
                err_data = {"error": resp.text}
            raise ShiprocketValidationError(
                f"Shiprocket rejected parameters with status {resp.status_code}: {err_data.get('message', resp.text)}",
                status_code=resp.status_code,
                response_data=err_data,
            )

        return resp

    # --------------------------------------------------------------------------
    # Courier Serviceability & Rate Lookup
    # --------------------------------------------------------------------------

    def check_serviceability(
        self,
        pickup_postcode: str,
        delivery_postcode: str,
        weight_kg: Decimal,
        cod: bool = False,
        length_cm: Decimal | None = None,
        breadth_cm: Decimal | None = None,
        height_cm: Decimal | None = None,
        declared_value: Decimal | None = None,
    ) -> RateEstimateResult:
        """
        Queries courier serviceability and live estimated shipping rates.
        Accepts origin PIN, destination PIN, chargeable weight, payment mode,
        and optional package bounding dimensions.
        """
        # Parameter Validation
        clean_pickup = str(pickup_postcode or "").strip()
        clean_delivery = str(delivery_postcode or "").strip()

        if not PIN_CODE_PATTERN.match(clean_pickup):
            raise ShiprocketValidationError(f"Invalid origin pickup PIN code format: '{clean_pickup}'. Expected 6-digit Indian PIN.")

        if not PIN_CODE_PATTERN.match(clean_delivery):
            raise ShiprocketValidationError(f"Invalid delivery destination PIN code format: '{clean_delivery}'. Expected 6-digit Indian PIN.")

        if weight_kg is None or weight_kg <= Decimal("0.00"):
            raise ShiprocketValidationError(f"Weight must be a positive Decimal value in kilograms, got: {weight_kg}.")

        # Offline / Mock Mode Simulation
        if self.mock_mode:
            return self._generate_mock_serviceability_result(
                pickup_postcode=clean_pickup,
                delivery_postcode=clean_delivery,
                weight_kg=weight_kg,
                cod=cod,
            )

        params: dict[str, str] = {
            "pickup_postcode": clean_pickup,
            "delivery_postcode": clean_delivery,
            "weight": str(weight_kg.quantize(Decimal("0.01"))),
            "cod": "1" if cod else "0",
        }

        if length_cm is not None and breadth_cm is not None and height_cm is not None:
            if length_cm > Decimal("0.00") and breadth_cm > Decimal("0.00") and height_cm > Decimal("0.00"):
                params["length"] = str(length_cm.quantize(Decimal("0.01")))
                params["breadth"] = str(breadth_cm.quantize(Decimal("0.01")))
                params["height"] = str(height_cm.quantize(Decimal("0.01")))

        if declared_value is not None and declared_value > Decimal("0.00"):
            params["declared_value"] = str(declared_value.quantize(Decimal("0.01")))

        resp = self._request("GET", "courier/serviceability/", params=params, authenticated=True)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ShiprocketError("Provider returned malformed non-JSON serviceability response.") from exc

        return self._parse_serviceability_response(
            payload=payload,
            origin_pin=clean_pickup,
            destination_pin=clean_delivery,
            weight_kg=weight_kg,
            measurement_tier="verified" if (length_cm and length_cm > 0) else "estimated",
        )

    def _parse_serviceability_response(
        self,
        payload: dict,
        origin_pin: str,
        destination_pin: str,
        weight_kg: Decimal,
        measurement_tier: str,
    ) -> RateEstimateResult:
        """
        Parses and validates Shiprocket JSON serviceability response.
        Extracts courier list, normalizes rates to Decimal, sorts by cost,
        and identifies cheapest and fastest couriers.
        """
        status_val = payload.get("status")
        data_block = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

        # Check for unserviceable route (e.g. 404 or empty couriers)
        companies = (
            data_block.get("available_courier_companies")
            or data_block.get("shipping_companies")
            or []
        )

        if status_val == 404 or not companies:
            msg = payload.get("message") or "No courier service available for this route."
            return RateEstimateResult(
                is_serviceable=False,
                origin_postal_code=origin_pin,
                destination_postal_code=destination_pin,
                chargeable_weight_kg=weight_kg,
                measurement_tier=measurement_tier,
                available_couriers=[],
                message=msg,
                error=msg,
                raw_data=sanitize_log_data(payload),
            )

        couriers: list[CourierRateQuote] = []
        for item in companies:
            if not isinstance(item, dict):
                continue

            raw_rate = item.get("rate")
            if raw_rate is None:
                continue

            try:
                decimal_rate = Decimal(str(raw_rate)).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                continue

            raw_etd = item.get("estimated_delivery_days") or item.get("etd")
            etd_days = None
            if raw_etd is not None:
                try:
                    # Parse digits from ETD (e.g. "3", "3-4 Days")
                    digits = re.findall(r"\d+", str(raw_etd))
                    if digits:
                        etd_days = int(digits[0])
                except Exception:
                    pass

            quote = CourierRateQuote(
                courier_company_id=int(item.get("courier_company_id") or item.get("id") or 0),
                courier_name=str(item.get("courier_name") or item.get("name") or "Standard Courier").strip(),
                rate=decimal_rate,
                etd_days=etd_days,
                etd_display=str(item.get("etd") or (f"{etd_days} days" if etd_days else "Standard delivery")),
                cod_available=bool(item.get("cod") == 1 or item.get("cod") is True),
                is_surface=bool(item.get("is_surface", True)),
                rating=float(item["rating"]) if item.get("rating") is not None else None,
                chargeable_weight_kg=weight_kg,
                raw_data=sanitize_log_data(item),
            )
            couriers.append(quote)

        if not couriers:
            return RateEstimateResult(
                is_serviceable=False,
                origin_postal_code=origin_pin,
                destination_postal_code=destination_pin,
                chargeable_weight_kg=weight_kg,
                measurement_tier=measurement_tier,
                available_couriers=[],
                message="No valid courier rates could be parsed from provider response.",
                error="Invalid courier rates in response.",
                raw_data=sanitize_log_data(payload),
            )

        # Sort by rate ascending
        couriers.sort(key=lambda c: c.rate)
        cheapest = couriers[0]

        # Identify fastest courier (lowest positive ETD)
        with_etd = [c for c in couriers if c.etd_days is not None and c.etd_days > 0]
        fastest = min(with_etd, key=lambda c: c.etd_days) if with_etd else cheapest

        return RateEstimateResult(
            is_serviceable=True,
            origin_postal_code=origin_pin,
            destination_postal_code=destination_pin,
            chargeable_weight_kg=weight_kg,
            measurement_tier=measurement_tier,
            available_couriers=couriers,
            cheapest_courier=cheapest,
            fastest_courier=fastest,
            recommended_courier=cheapest,
            is_mock=False,
            message=f"Found {len(couriers)} serviceable couriers. Cheapest option: {cheapest.courier_name} (₹{cheapest.rate}).",
            raw_data=sanitize_log_data(payload),
        )

    # --------------------------------------------------------------------------
    # Deterministic Mock Simulation for Offline / Unit Testing
    # --------------------------------------------------------------------------

    def _generate_mock_serviceability_result(
        self,
        pickup_postcode: str,
        delivery_postcode: str,
        weight_kg: Decimal,
        cod: bool,
    ) -> RateEstimateResult:
        """Generates realistic deterministic courier rates for offline testing."""
        # Check if same state (first 2 digits match)
        is_intra_state = pickup_postcode[:2] == delivery_postcode[:2]
        base_rate = Decimal("45.00") if is_intra_state else Decimal("75.00")

        # Weight multiplier: ₹30 per 0.5kg above 0.5kg
        weight_multiplier = max(Decimal("1.00"), (weight_kg / Decimal("0.50")).quantize(Decimal("1.00")))
        rate_surface = (base_rate + (Decimal("25.00") * (weight_multiplier - Decimal("1.00")))).quantize(Decimal("0.01"))
        rate_express = (rate_surface * Decimal("1.45")).quantize(Decimal("0.01"))
        rate_premium = (rate_surface * Decimal("1.80")).quantize(Decimal("0.01"))

        couriers = [
            CourierRateQuote(
                courier_company_id=1,
                courier_name="Delhivery Surface",
                rate=rate_surface,
                etd_days=3 if is_intra_state else 5,
                etd_display="3-5 Days",
                cod_available=True,
                is_surface=True,
                rating=4.2,
                chargeable_weight_kg=weight_kg,
            ),
            CourierRateQuote(
                courier_company_id=2,
                courier_name="Blue Dart Express",
                rate=rate_express,
                etd_days=2 if is_intra_state else 3,
                etd_display="2-3 Days",
                cod_available=True,
                is_surface=False,
                rating=4.6,
                chargeable_weight_kg=weight_kg,
            ),
            CourierRateQuote(
                courier_company_id=3,
                courier_name="DTDC Premium Air",
                rate=rate_premium,
                etd_days=1 if is_intra_state else 2,
                etd_display="1-2 Days",
                cod_available=cod,
                is_surface=False,
                rating=4.4,
                chargeable_weight_kg=weight_kg,
            ),
        ]

        couriers.sort(key=lambda c: c.rate)
        return RateEstimateResult(
            is_serviceable=True,
            origin_postal_code=pickup_postcode,
            destination_postal_code=delivery_postcode,
            chargeable_weight_kg=weight_kg,
            measurement_tier="estimated",
            available_couriers=couriers,
            cheapest_courier=couriers[0],
            fastest_courier=couriers[-1],
            recommended_courier=couriers[0],
            is_mock=True,
            message="[MOCK] Deterministic rate simulation generated for testing.",
        )


# ==============================================================================
# PACKAGE-SPECIFIC RATE ESTIMATION SERVICE
# ==============================================================================

def estimate_package_shipping_rate(
    package: Any,
    allow_estimated_dimensions: bool = True,
    client: ShiprocketClient | None = None,
    cache_ttl_seconds: int = 900,
) -> RateEstimateResult:
    """
    Prepares a safe courier serviceability and rate estimation request for an eligible SellerOrder.

    Inputs utilized:
    - Destination PIN from parent order (`package.order.shipping_postal_code`).
    - Verified pickup facility or central platform warehouse.
    - Dead weight, volumetric weight, and chargeable weight from package measurements.
    - Verified vs estimated measurement tier status.
    - COD status from parent order.

    Strict Constraints & Safeguards:
    - Never modifies `actual_shipping_cost`, `total_commission`, or `net_seller_payable`.
    - Does not alter customer shipping fee or historical financial snapshots.
    - Rejects unverified or incomplete pickup locations.
    - Rejects missing dimensions or non-positive weight.
    - Caches transient rate quotes in Django cache (default 15 minutes).
    """
    from core.models import Order, SellerOrder

    if not isinstance(package, SellerOrder):
        raise ShiprocketValidationError("Target package must be an instance of SellerOrder.")

    # 1. Destination PIN validation
    destination_pin = (package.order.shipping_postal_code or "").strip()
    if not destination_pin or not PIN_CODE_PATTERN.match(destination_pin):
        raise ShiprocketValidationError(
            f"Package #{package.sub_order_number} has an invalid or missing destination postal code: '{destination_pin}'."
        )

    # 2. Pickup facility verification check
    pickup_details = package.get_pickup_location_details()
    if not pickup_details.get("is_complete"):
        errors_str = "; ".join(pickup_details.get("errors", []))
        raise ShiprocketValidationError(
            f"Pickup location for package #{package.sub_order_number} is incomplete: {errors_str}"
        )

    # In production live mode, reject unverified pickup facilities
    is_mock = getattr(settings, "SHIPROCKET_MOCK_MODE", False)
    if not is_mock and pickup_details.get("status") != "verified":
        raise ShiprocketValidationError(
            f"Pickup facility for package #{package.sub_order_number} is unverified ('{pickup_details.get('status')}'). "
            "Shiprocket rate lookup requires verified pickup facility coordinates."
        )

    origin_pin = (pickup_details.get("postal_code") or "").strip()
    if not origin_pin or not PIN_CODE_PATTERN.match(origin_pin):
        raise ShiprocketValidationError(
            f"Origin pickup facility PIN code is invalid: '{origin_pin}'."
        )

    # 3. Dimension & Weight Validation
    status = package.package_dimension_status
    if status == SellerOrder.PACKAGE_DIMENSION_UNKNOWN:
        raise ShiprocketValidationError(
            f"Package #{package.sub_order_number} has unknown physical dimensions. "
            "Please record verified packed measurements or configure product dimensions before rate lookup."
        )

    if status == SellerOrder.PACKAGE_DIMENSION_ESTIMATED and not allow_estimated_dimensions:
        raise ShiprocketValidationError(
            f"Package #{package.sub_order_number} has estimated measurements only, "
            "but rate estimation policy requires verified packed measurements."
        )

    chargeable_weight_kg = package.get_chargeable_weight_kg()
    if chargeable_weight_kg is None or chargeable_weight_kg <= Decimal("0.00"):
        raise ShiprocketValidationError(
            f"Package #{package.sub_order_number} chargeable weight could not be calculated or is non-positive."
        )

    eff_dim = package.get_effective_measurements()
    length_cm = eff_dim.get("length_cm")
    breadth_cm = eff_dim.get("breadth_cm")
    height_cm = eff_dim.get("height_cm")

    is_cod = (package.order.payment_method == Order.PAYMENT_METHOD_COD)
    declared_value = package.subtotal

    # 4. Check cache for recent transient quote
    cache_key = f"shiprocket_rate_estimate_pkg_{package.pk}"
    cached_result: RateEstimateResult | None = cache.get(cache_key)
    if cached_result and not getattr(client, "mock_mode", False):
        logger.debug("Returning cached rate estimate for package #%s.", package.sub_order_number)
        return cached_result

    # 5. Execute Provider Lookup
    active_client = client or ShiprocketClient()
    result = active_client.check_serviceability(
        pickup_postcode=origin_pin,
        delivery_postcode=destination_pin,
        weight_kg=chargeable_weight_kg,
        cod=is_cod,
        length_cm=length_cm,
        breadth_cm=breadth_cm,
        height_cm=height_cm,
        declared_value=declared_value,
    )

    # Update measurement tier to match package reality
    result.measurement_tier = "verified" if status == SellerOrder.PACKAGE_DIMENSION_VERIFIED else "estimated"

    # Cache successful result
    if result.is_serviceable and cache_ttl_seconds > 0:
        cache.set(cache_key, result, cache_ttl_seconds)

    return result


# ==============================================================================
# BOOKING ELIGIBILITY & IDEMPOTENT MOCK BOOKING ENGINE (PHASE 5 PROMPT 5B)
# ==============================================================================

def check_booking_eligibility(package: Any) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Centralized validation gate for package shipment booking eligibility.

    Evaluates 7 mandatory conditions:
    1. Parent order payment: COD or online status confirmed as PAID.
    2. Package lifecycle: not cancelled, shipped, or delivered.
    3. Fulfillment stage: package is in 'ready_to_ship' status.
    4. Packing measurements: physical dimensions and dead weight are valid and > 0.
    5. Pickup facility: pickup location details are complete and marked verified.
    6. Third-party vendor pickup nickname: required for vendor warehouse dispatches.
    7. Existing booking check: package must not already have a completed booking.

    Returns:
        (is_eligible, error_message_or_None, details_dict)
    """
    from core.models import Order, Seller, SellerOrder

    details: dict[str, Any] = {}

    if not isinstance(package, SellerOrder):
        return False, "Target package must be an instance of SellerOrder.", details

    order = package.order
    details["sub_order_number"] = package.sub_order_number
    details["status"] = package.status
    details["payment_method"] = order.payment_method
    details["payment_status"] = order.payment_status

    # 1. Payment Eligibility Check
    is_cod = (order.payment_method == Order.PAYMENT_METHOD_COD)
    is_paid = (order.payment_status == Order.PAYMENT_STATUS_PAID)
    if not (is_cod or is_paid):
        msg = (
            f"Parent order #{order.order_number} payment is unconfirmed "
            f"(Method: {order.get_payment_method_display()}, Status: {order.get_payment_status_display()}). "
            "Only COD orders or verified paid online orders are eligible for shipment booking."
        )
        return False, msg, details

    # 2. Package Lifecycle Guard
    if package.status == SellerOrder.STATUS_CANCELLED:
        return False, "Cannot book shipment for a cancelled package.", details

    if package.status in (SellerOrder.STATUS_SHIPPED, SellerOrder.STATUS_DELIVERED):
        return False, f"Cannot book shipment for a package that is already {package.get_status_display()}.", details

    # 3. Fulfillment Stage Guard (Must be ready_to_ship)
    if package.status != SellerOrder.STATUS_READY_TO_SHIP:
        return (
            False,
            f"Package must be in 'Ready to Ship' status before booking. Current status is '{package.get_status_display()}'.",
            details,
        )

    # 4. Packaging Measurements & Dead Weight Guard
    if package.package_dimension_status == SellerOrder.PACKAGE_DIMENSION_UNKNOWN:
        return (
            False,
            "Package physical dimensions are unknown. Packing measurements must be recorded before booking.",
            details,
        )

    dead_weight_kg = package.get_dead_weight_kg()
    if dead_weight_kg is None or dead_weight_kg <= Decimal("0.000"):
        return False, "Package dead weight must be greater than zero.", details

    eff_dim = package.get_effective_measurements()
    length_cm = eff_dim.get("length_cm")
    breadth_cm = eff_dim.get("breadth_cm")
    height_cm = eff_dim.get("height_cm")

    if any(dim is None or dim <= Decimal("0.00") for dim in (length_cm, breadth_cm, height_cm)):
        return False, "Package carton dimensions (length, breadth, height) must all be greater than zero.", details

    # 5. Pickup Location Verification Guard
    pickup_details = package.get_pickup_location_details()
    if not pickup_details.get("is_complete"):
        errors_str = "; ".join(pickup_details.get("errors", []))
        return False, f"Pickup facility location is incomplete: {errors_str}", details

    if not pickup_details.get("is_verified"):
        return False, "Pickup facility location is unverified. Warehouse coordinates must be verified before booking.", details

    # 6. Seller Compliance & Pickup Location Nickname (For 3rd-party vendors)
    if not package.is_platform_fulfilled and package.seller:
        seller = package.seller
        if seller.status != Seller.STATUS_APPROVED:
            return (
                False,
                f"Seller account '{seller.store_name}' is not approved (status: {seller.get_status_display()}).",
                details,
            )
        if not seller.is_active:
            return False, f"Seller account '{seller.store_name}' is currently inactive.", details
        if not seller.has_valid_gstin:
            return False, f"Seller '{seller.store_name}' must have a valid 15-character GSTIN before shipment booking.", details
        nickname = (seller.pickup_location_nickname or "").strip()
        if not nickname:
            return False, "Seller pickup facility nickname is required before booking.", details

    # 7. Duplicate Booking Guard
    if package.is_booked:
        booking = getattr(package, "shipment_booking", None)
        ref_text = f"under reference {booking.shipment_id} (AWB: {booking.awb_code})" if booking else "already"
        return (
            False,
            f"Package #{package.sub_order_number} is already booked {ref_text}.",
            details,
        )

    return True, None, details


def validate_booking_eligibility(package: Any) -> None:
    """
    Raises ShiprocketValidationError if package is not eligible for booking.
    """
    is_eligible, err_msg, _ = check_booking_eligibility(package)
    if not is_eligible:
        raise ShiprocketValidationError(err_msg or "Package is ineligible for shipment booking.", user_message=err_msg)


def create_mock_shipment_booking(
    package_id: int,
    idempotency_key: str | None = None,
    user: Any = None,
    courier_name: str | None = None,
) -> Any:
    """
    Durable, idempotent mock shipment booking engine (Phase 5 Prompt 5B).

    Guarantees:
    - Strict Mock-Only Safety Gate: Fails closed if SHIPROCKET_MOCK_MODE is False.
    - Database row locking: Enforces atomic select_for_update() on SellerOrder.
    - Re-evaluates payment, package status, measurements, and existing booking after acquiring lock.
    - Strict Idempotency: Re-submitting the same idempotency key or booking a package
      that already has a ShipmentBooking record returns the existing booking safely.
    - Preserves historical financial snapshots: Never modifies parent payment status,
      order totals, commissions, seller payable, or actual courier invoices.
    - Never calls external Shiprocket APIs or real carrier networks.
    - Produces clearly marked simulated identifiers (MOCK-SHP-... and MOCK-AWB-...).
    """
    from core.models import SellerOrder, ShipmentBooking

    # 1. Strict Mock-Only Safety Gate
    if not getattr(settings, "SHIPROCKET_MOCK_MODE", False):
        raise ShiprocketConfigurationError(
            "Live Shiprocket shipment booking is disabled. "
            "Prompt 5B is mock-only; SHIPROCKET_MOCK_MODE must be True."
        )

    clean_key = (idempotency_key or "").strip()
    if clean_key:
        existing_by_key = ShipmentBooking.objects.filter(idempotency_key=clean_key).first()
        if existing_by_key:
            if existing_by_key.package_id != package_id:
                raise ShiprocketValidationError(
                    "Idempotency key collision: This idempotency key was already used for a different package.",
                    user_message="Idempotency key collision detected.",
                )
            logger.info("Returning existing mock booking for idempotency key %s.", clean_key)
            return existing_by_key

    # 2. Atomic Database Transaction with Row Lock
    with transaction.atomic():
        package = (
            SellerOrder.objects.select_for_update()
            .select_related("order", "seller")
            .filter(pk=package_id)
            .first()
        )
        if not package:
            raise ShiprocketValidationError(f"Package #{package_id} was not found.")

        # Re-check idempotency on locked package
        if package.is_booked:
            logger.info("Package #%s already has a shipment booking. Returning existing record.", package.sub_order_number)
            return package.shipment_booking

        if clean_key:
            existing_by_key = ShipmentBooking.objects.filter(idempotency_key=clean_key).first()
            if existing_by_key:
                return existing_by_key

        # Re-check complete eligibility under lock
        validate_booking_eligibility(package)

        final_key = clean_key or f"mock-key-{package.pk}-{uuid.uuid4().hex[:16]}"

        # 3. Deterministic Simulated Identifiers
        simulated_shipment_id = f"MOCK-SHP-{package.sub_order_number}"
        token_hash = hashlib.sha256(f"{package.sub_order_number}:{final_key}".encode()).hexdigest()[:10].upper()
        simulated_awb = f"MOCK-AWB-{token_hash}"

        pickup_details = package.get_pickup_location_details()
        pickup_nick = (
            pickup_details.get("details", {}).get("pickup_location_nickname")
            or pickup_details.get("name")
            or "MOCK_WH"
        )

        chargeable_wt = package.get_chargeable_weight_kg() or Decimal("0.500")
        assigned_courier = (courier_name or "").strip() or "Delhivery Surface (Simulated)"

        # 4. Create Durable ShipmentBooking Record
        try:
            with transaction.atomic():
                booking = ShipmentBooking.objects.create(
                    package=package,
                    idempotency_key=final_key,
                    is_mock=True,
                    status="booked",
                    shipment_id=simulated_shipment_id,
                    awb_code=simulated_awb,
                    courier_name=assigned_courier,
                    chargeable_weight_kg=chargeable_wt,
                    estimated_rate=Decimal("75.00"),
                    pickup_nickname=pickup_nick,
                    metadata={
                        "simulated": True,
                        "simulation_notice": "SIMULATED BOOKING - NO LIVE CARRIER CONTACTED",
                        "booked_by": user.username if user and user.is_authenticated else "system",
                        "booked_at_iso": timezone.now().isoformat(),
                    },
                )
        except IntegrityError:
            # Concurrently created by a parallel request: return existing record safely
            if clean_key:
                existing = ShipmentBooking.objects.filter(idempotency_key=clean_key).first()
                if existing:
                    return existing
            existing = ShipmentBooking.objects.filter(package_id=package.pk).first()
            if existing:
                return existing
            raise

        logger.info(
            "Successfully created mock shipment booking %s (AWB: %s) for package #%s.",
            booking.shipment_id,
            booking.awb_code,
            package.sub_order_number,
        )

        return booking

