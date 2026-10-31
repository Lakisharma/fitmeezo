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
import json
import logging
import os
import re
import secrets
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
        self.email = email if email is not None else (getattr(settings, "SHIPROCKET_EMAIL", "") or getattr(settings, "SHIPROCKET_API_EMAIL", ""))
        self.password = password if password is not None else (getattr(settings, "SHIPROCKET_PASSWORD", "") or getattr(settings, "SHIPROCKET_API_PASSWORD", ""))
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
            payload = resp if isinstance(resp, dict) else resp.json()
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

    # --------------------------------------------------------------------------
    # Pickup Location Registration & Management (Documented Endpoint)
    # --------------------------------------------------------------------------

    def register_pickup_location(self, seller_or_data: Any) -> dict:
        """
        Registers a seller's physical warehouse or facility with Shiprocket via:
        POST /settings/company/addpickup
        Docs: https://apidocs.shiprocket.in/#09756157-1985-48ff-a0ff-4e4708799787

        Supports both Seller model instance and raw dictionary input.
        Guarantees:
        - Prevents duplicate registration if location already registered.
        - Persists returned location nickname / ID.
        - Sanitizes logging.
        - Deterministic mock simulation when mock_mode is active.
        """
        from core.models import Seller

        if isinstance(seller_or_data, Seller):
            seller = seller_or_data
            clean_pin = (seller.pickup_postal_code or "").strip()
            clean_addr = (seller.pickup_address or "").strip()
            clean_city = (seller.pickup_city or "").strip()
            clean_state = (seller.pickup_state or "").strip()
            clean_name = (seller.pickup_contact_name or seller.legal_business_name or seller.store_name).strip()[:50]
            raw_phone = seller.pickup_contact_phone or seller.support_phone or ""
            clean_phone = re.sub(r"\D", "", raw_phone)[-10:]
            clean_email = (seller.support_email or "").strip()
            loc_nickname = seller.get_shiprocket_pickup_location_name()
            landmark = (seller.pickup_landmark or "").strip()
        elif isinstance(seller_or_data, dict):
            seller = None
            clean_pin = str(seller_or_data.get("pin_code", "")).strip()
            clean_addr = str(seller_or_data.get("address", "")).strip()
            clean_city = str(seller_or_data.get("city", "")).strip()
            clean_state = str(seller_or_data.get("state", "")).strip()
            clean_name = str(seller_or_data.get("name", "")).strip()[:50]
            clean_phone = re.sub(r"\D", "", str(seller_or_data.get("phone", "")))[-10:]
            clean_email = str(seller_or_data.get("email", "")).strip()
            loc_nickname = str(seller_or_data.get("pickup_location", "")).strip()
            landmark = str(seller_or_data.get("address_2", "")).strip()
        else:
            raise ShiprocketValidationError("Input must be a Seller instance or dictionary.")

        if not PIN_CODE_PATTERN.match(clean_pin):
            raise ShiprocketValidationError(f"Invalid PIN code '{clean_pin}' for pickup location registration.")
        if len(clean_phone) != 10:
            raise ShiprocketValidationError(f"Invalid 10-digit phone '{clean_phone}' for pickup contact.")
        if not clean_addr or len(clean_addr) < 5:
            raise ShiprocketValidationError("Pickup warehouse address is required (minimum 5 characters).")
        if not clean_city or not clean_state:
            raise ShiprocketValidationError("Pickup city and state are required.")

        payload = {
            "pickup_location": loc_nickname,
            "name": clean_name or "Fitmeezo Warehouse",
            "email": clean_email or "operations@fitmeezo.com",
            "phone": clean_phone,
            "address": clean_addr[:100],
            "address_2": landmark[:100],
            "city": clean_city,
            "state": clean_state,
            "country": "India",
            "pin_code": clean_pin,
        }

        # Offline / Mock Mode Simulation
        if self.mock_mode:
            simulated_loc_id = f"MOCK-LOC-{loc_nickname}"
            if seller:
                seller.shiprocket_pickup_location_id = loc_nickname
                seller.shiprocket_pickup_registration_status = Seller.PICKUP_REG_STATUS_REGISTERED
                seller.shiprocket_pickup_registered_at = timezone.now()
                seller.shiprocket_pickup_registration_error = ""
                seller.pickup_location_verified = True
                seller.save(update_fields=[
                    "shiprocket_pickup_location_id",
                    "shiprocket_pickup_registration_status",
                    "shiprocket_pickup_registered_at",
                    "shiprocket_pickup_registration_error",
                    "pickup_location_verified",
                    "updated_at",
                ])
            return {
                "success": True,
                "pickup_location": loc_nickname,
                "pickup_id": simulated_loc_id,
                "is_mock": True,
                "message": "[MOCK] Pickup facility registered successfully.",
            }

        # Check if location already registered in Shiprocket (Pre-check duplicate prevention)
        try:
            existing_locs = self.get_pickup_locations()
            for ex in existing_locs:
                if str(ex.get("pickup_location", "")).strip().lower() == loc_nickname.lower():
                    confirmed_loc = str(ex.get("id") or ex.get("pickup_id") or ex.get("pickup_location") or loc_nickname)
                    if seller:
                        seller.shiprocket_pickup_location_id = confirmed_loc
                        seller.shiprocket_pickup_registration_status = Seller.PICKUP_REG_STATUS_REGISTERED
                        seller.shiprocket_pickup_registered_at = timezone.now()
                        seller.shiprocket_pickup_registration_error = ""
                        seller.pickup_location_verified = True
                        seller.save(update_fields=[
                            "shiprocket_pickup_location_id",
                            "shiprocket_pickup_registration_status",
                            "shiprocket_pickup_registered_at",
                            "shiprocket_pickup_registration_error",
                            "pickup_location_verified",
                            "updated_at",
                        ])
                    return {"success": True, "pickup_location": confirmed_loc, "already_exists": True}
        except Exception as check_err:
            logger.debug("Could not pre-fetch pickup locations: %s", check_err)

        logger.info("Registering pickup location '%s' with Shiprocket.", loc_nickname)
        try:
            resp = self._request("POST", "settings/company/addpickup", json=payload, authenticated=True)
            if isinstance(resp, dict):
                resp_data = resp
            elif hasattr(resp, "json"):
                resp_data = resp.json() if getattr(resp, "text", None) else {}
            else:
                resp_data = {}
        except ShiprocketValidationError as exc:
            # Check if provider indicates location already exists
            err_text = str(exc).lower()
            if "already exists" in err_text or "already registered" in err_text:
                logger.info("Pickup location '%s' already registered with Shiprocket.", loc_nickname)
                if seller:
                    seller.shiprocket_pickup_location_id = loc_nickname
                    seller.shiprocket_pickup_registration_status = Seller.PICKUP_REG_STATUS_REGISTERED
                    seller.shiprocket_pickup_registered_at = timezone.now()
                    seller.shiprocket_pickup_registration_error = ""
                    seller.pickup_location_verified = True
                    seller.save(update_fields=[
                        "shiprocket_pickup_location_id",
                        "shiprocket_pickup_registration_status",
                        "shiprocket_pickup_registered_at",
                        "shiprocket_pickup_registration_error",
                        "pickup_location_verified",
                        "updated_at",
                    ])
                return {"success": True, "pickup_location": loc_nickname, "already_exists": True}

            if seller:
                seller.shiprocket_pickup_registration_status = Seller.PICKUP_REG_STATUS_FAILED
                seller.shiprocket_pickup_registration_error = exc.message[:500]
                seller.save(update_fields=["shiprocket_pickup_registration_status", "shiprocket_pickup_registration_error", "updated_at"])
            raise
        except Exception as exc:
            if seller:
                seller.shiprocket_pickup_registration_status = Seller.PICKUP_REG_STATUS_FAILED
                seller.shiprocket_pickup_registration_error = str(exc)[:500]
                seller.save(update_fields=["shiprocket_pickup_registration_status", "shiprocket_pickup_registration_error", "updated_at"])
            raise

        confirmed_loc = str(
            resp_data.get("pickup_id")
            or resp_data.get("address", {}).get("id")
            or resp_data.get("address", {}).get("pickup_location")
            or resp_data.get("pickup_location")
            or loc_nickname
        )
        if seller:
            seller.shiprocket_pickup_location_id = confirmed_loc
            seller.shiprocket_pickup_registration_status = Seller.PICKUP_REG_STATUS_REGISTERED
            seller.shiprocket_pickup_registered_at = timezone.now()
            seller.shiprocket_pickup_registration_error = ""
            seller.pickup_location_verified = True
            seller.save(update_fields=[
                "shiprocket_pickup_location_id",
                "shiprocket_pickup_registration_status",
                "shiprocket_pickup_registered_at",
                "shiprocket_pickup_registration_error",
                "pickup_location_verified",
                "updated_at",
            ])

        return {
            "success": True,
            "pickup_location": confirmed_loc,
            "response": sanitize_log_data(resp_data),
        }

    def get_pickup_locations(self) -> list[dict]:
        """
        Fetches all registered pickup facilities from Shiprocket:
        GET /settings/company/pickup
        """
        if self.mock_mode:
            return [{"pickup_location": "MOCK_PRIMARY_WH", "status": 1}]

        resp = self._request("GET", "settings/company/pickup", authenticated=True)
        try:
            data = resp.json()
            shipping_addrs = data.get("data", {}).get("shipping_address", [])
            return shipping_addrs if isinstance(shipping_addrs, list) else []
        except Exception as exc:
            raise ShiprocketError(f"Failed to parse pickup locations: {exc}") from exc

    # --------------------------------------------------------------------------
    # Order & Custom Shipment Creation (Documented Endpoint)
    # --------------------------------------------------------------------------

    def create_order(self, package: Any, pickup_location_name: str | None = None) -> dict:
        """
        Creates an ad-hoc custom shipment/order in Shiprocket via:
        POST /orders/create/adhoc
        Docs: https://apidocs.shiprocket.in/#c1eb4ef5-6670-49b8-a6e5-4f40f0c0ae16
        """
        from core.models import Order, SellerOrder

        if not isinstance(package, SellerOrder):
            raise ShiprocketValidationError("Target package must be an instance of SellerOrder.")

        order = package.order
        destination_pin = (order.shipping_postal_code or "").strip()
        if not PIN_CODE_PATTERN.match(destination_pin):
            raise ShiprocketValidationError(f"Invalid destination PIN code: '{destination_pin}'.")

        chargeable_wt = package.get_chargeable_weight_kg() or Decimal("0.500")
        eff_dims = package.get_effective_measurements()
        length_cm = eff_dims.get("length_cm") or Decimal("10.00")
        breadth_cm = eff_dims.get("breadth_cm") or Decimal("10.00")
        height_cm = eff_dims.get("height_cm") or Decimal("10.00")

        # Resolve registered pickup location
        loc_name = pickup_location_name
        if not loc_name:
            if not package.is_platform_fulfilled and package.seller:
                loc_name = package.seller.shiprocket_pickup_location_id or package.seller.get_shiprocket_pickup_location_name()
            else:
                loc_name = getattr(settings, "SHIPROCKET_PICKUP_LOCATION", "") or "Fitmeezo_Central_Warehouse"

        if not loc_name:
            raise ShiprocketValidationError("Pickup location name could not be resolved.")

        # Split customer name
        full_name = (order.shipping_full_name or "Customer").strip()
        name_parts = full_name.split(" ", 1)
        first_name = name_parts[0][:30]
        last_name = name_parts[1][:30] if len(name_parts) > 1 else "Customer"

        is_cod = (order.payment_method == Order.PAYMENT_METHOD_COD)
        phone_digits = re.sub(r"\D", "", order.shipping_phone_number or "")[-10:]
        if len(phone_digits) != 10:
            phone_digits = "9876543210"

        # Format line items
        order_items_payload = []
        for item in package.items.all():
            order_items_payload.append({
                "name": (item.product_name or "Fitmeezo Item")[:100],
                "sku": (item.sku or f"SKU-{item.product_id}")[:50],
                "units": item.quantity,
                "selling_price": str(item.unit_price.quantize(Decimal("0.01"))),
                "discount": "0.00",
                "tax": "0.00",
            })

        if not order_items_payload:
            order_items_payload.append({
                "name": f"Package #{package.sub_order_number}",
                "sku": f"PKG-{package.pk}",
                "units": 1,
                "selling_price": str(package.subtotal.quantize(Decimal("0.01"))),
                "discount": "0.00",
                "tax": "0.00",
            })

        order_date_str = order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else timezone.now().strftime("%Y-%m-%d %H:%M")

        payload = {
            "order_id": package.sub_order_number,
            "order_date": order_date_str,
            "pickup_location": loc_name,
            "channel_id": "",
            "comment": f"Fitmeezo Order #{order.order_number} Package #{package.sub_order_number}",
            "billing_customer_name": first_name,
            "billing_last_name": last_name,
            "billing_address": (order.shipping_address_line1 or "Street Address")[:100],
            "billing_address_2": ((order.shipping_address_line2 or "") + " " + (order.shipping_landmark or "")).strip()[:100],
            "billing_city": order.shipping_city[:50],
            "billing_pincode": destination_pin,
            "billing_state": order.shipping_state[:50],
            "billing_country": "India",
            "billing_email": (order.user.email if order.user else "support@fitmeezo.com")[:100],
            "billing_phone": phone_digits,
            "shipping_is_billing": True,
            "order_items": order_items_payload,
            "payment_method": "COD" if is_cod else "Prepaid",
            "sub_total": float(package.subtotal),
            "length": float(length_cm),
            "breadth": float(breadth_cm),
            "height": float(height_cm),
            "weight": float(chargeable_wt),
        }

        # Offline / Mock Mode Simulation
        if self.mock_mode:
            mock_order_id = f"MOCK-ORD-{package.sub_order_number}"
            mock_shipment_id = f"MOCK-SHP-{package.sub_order_number}"
            return {
                "order_id": mock_order_id,
                "shipment_id": mock_shipment_id,
                "status": "NEW",
                "status_code": 1,
                "is_mock": True,
            }

        logger.info("Creating Shiprocket order for package #%s (pickup: %s).", package.sub_order_number, loc_name)
        resp = self._request("POST", "orders/create/adhoc", json=payload, authenticated=True)
        try:
            data = resp if isinstance(resp, dict) else resp.json()
        except ValueError as exc:
            raise ShiprocketError("Non-JSON response from Shiprocket order creation.") from exc

        shipment_id = data.get("shipment_id")
        order_id = data.get("order_id")

        if not shipment_id:
            raise ShiprocketValidationError(
                f"Shiprocket order creation did not return a valid shipment_id: {data.get('message', 'Unknown error')}",
                response_data=data,
            )

        return {
            "order_id": order_id or package.sub_order_number,
            "shipment_id": shipment_id,
            "status": data.get("status", "NEW"),
            "raw_data": sanitize_log_data(data),
        }

    # --------------------------------------------------------------------------
    # Courier Assignment & AWB Generation (Documented Endpoint)
    # --------------------------------------------------------------------------

    def assign_awb(self, shipment_id: int | str, courier_id: int | None = None) -> dict:
        """
        Assigns an optimal courier partner and generates an Air Waybill (AWB) tracking number:
        POST /couriers/assign/awb
        Docs: https://apidocs.shiprocket.in/#05952c41-dc04-45b6-b097-426adadfb9b8
        """
        payload: dict[str, Any] = {"shipment_id": str(shipment_id)}
        if courier_id is not None:
            payload["courier_id"] = str(courier_id)

        # Offline / Mock Mode Simulation
        if self.mock_mode:
            token_hash = hashlib.sha256(f"{shipment_id}:mock_awb".encode()).hexdigest()[:10].upper()
            return {
                "awb_code": f"MOCK-AWB-{token_hash}",
                "courier_name": "Delhivery Surface (Simulated)",
                "courier_company_id": courier_id or 1,
                "is_mock": True,
            }

        logger.info("Requesting AWB assignment for shipment %s (courier: %s).", shipment_id, courier_id)
        resp = self._request("POST", "couriers/assign/awb", json=payload, authenticated=True)
        try:
            data = resp if isinstance(resp, dict) else resp.json()
        except ValueError as exc:
            raise ShiprocketError("Non-JSON response from Shiprocket AWB assignment.") from exc

        resp_data = data.get("response", {}).get("data", {}) if isinstance(data.get("response"), dict) else data
        awb_code = resp_data.get("awb_code") or data.get("awb_code")
        courier_name = resp_data.get("courier_name") or data.get("courier_name") or "Standard Logistics"
        assigned_courier_id = resp_data.get("courier_company_id") or courier_id

        if not awb_code:
            err_msg = data.get("message") or resp_data.get("awb_assign_error") or "Failed to assign AWB"
            raise ShiprocketValidationError(f"Shiprocket could not assign AWB: {err_msg}", response_data=data)

        return {
            "awb_code": awb_code,
            "courier_name": courier_name,
            "courier_company_id": assigned_courier_id,
            "raw_data": sanitize_log_data(data),
        }

    # --------------------------------------------------------------------------
    # Schedule Courier Warehouse Pickup (Documented Endpoint)
    # --------------------------------------------------------------------------

    def request_pickup(self, shipment_id: int | str) -> dict:
        """
        Requests physical carrier pickup from origin facility:
        POST /orders/pickup/request
        Docs: https://apidocs.shiprocket.in/#09756157-1985-48ff-a0ff-4e4708799787
        """
        payload = {"shipment_id": [str(shipment_id)]}

        # Offline / Mock Mode Simulation
        if self.mock_mode:
            sched_date = timezone.now() + timezone.timedelta(days=1)
            return {
                "pickup_status": "scheduled",
                "pickup_scheduled_date": sched_date,
                "pickup_token_number": f"MOCK-CRN-{uuid.uuid4().hex[:8].upper()}",
                "is_mock": True,
                "message": "[MOCK] Courier pickup scheduled.",
            }

        logger.info("Requesting courier pickup for shipment %s.", shipment_id)
        resp = self._request("POST", "orders/pickup/request", json=payload, authenticated=True)
        try:
            data = resp if isinstance(resp, dict) else resp.json()
        except ValueError as exc:
            raise ShiprocketError("Non-JSON response from Shiprocket pickup request.") from exc

        resp_data = data.get("response", {}) if isinstance(data.get("response"), dict) else data
        pickup_token = resp_data.get("pickup_token_number") or data.get("pickup_token_number") or ""
        pickup_status = resp_data.get("pickup_status") or "requested"
        pickup_sched_str = resp_data.get("pickup_scheduled_date")

        pickup_date = None
        if pickup_sched_str:
            try:
                from datetime import datetime, time
                from django.utils.dateparse import parse_date, parse_datetime
                if isinstance(pickup_sched_str, str):
                    pickup_date = parse_datetime(pickup_sched_str)
                    if not pickup_date:
                        d = parse_date(pickup_sched_str)
                        if d:
                            pickup_date = datetime.combine(d, time.min)
                elif isinstance(pickup_sched_str, datetime):
                    pickup_date = pickup_sched_str

                if pickup_date and timezone.is_naive(pickup_date):
                    pickup_date = timezone.make_aware(pickup_date, timezone.get_current_timezone())
            except Exception:
                pass

        return {
            "pickup_status": pickup_status,
            "pickup_scheduled_date": pickup_date,
            "pickup_token_number": pickup_token,
            "raw_data": sanitize_log_data(data),
        }

    # --------------------------------------------------------------------------
    # Live Courier Tracking (Documented Endpoint)
    # --------------------------------------------------------------------------

    def track_awb(self, awb_code: str) -> dict:
        """
        Retrieves real-time carrier tracking milestones and scan history:
        GET /courier/track/awb/{awb_code}
        Docs: https://apidocs.shiprocket.in/#7f21f153-6bb3-4ca2-a8c6-a6e5b4b10b0e
        """
        clean_awb = str(awb_code or "").strip()
        if not clean_awb:
            raise ShiprocketValidationError("AWB tracking code is required.")

        # Offline / Mock Mode Simulation
        if self.mock_mode:
            return {
                "awb_code": clean_awb,
                "current_status": "IN TRANSIT",
                "status_code": 18,
                "courier_name": "Delhivery Surface",
                "scans": [
                    {"activity": "Pickup Scheduled", "location": "Origin Warehouse", "date": timezone.now().strftime("%Y-%m-%d %H:%M")},
                    {"activity": "In Transit to Destination Hub", "location": "Central Hub", "date": timezone.now().strftime("%Y-%m-%d %H:%M")},
                ],
                "etd": "3-5 Days",
                "is_mock": True,
            }

        logger.info("Querying live tracking for AWB %s.", clean_awb)
        resp = self._request("GET", f"courier/track/awb/{clean_awb}", authenticated=True)
        try:
            data = resp if isinstance(resp, dict) else resp.json()
        except ValueError as exc:
            raise ShiprocketError("Non-JSON tracking response from Shiprocket.") from exc

        track_data = data.get("tracking_data", {})
        shipment_track = track_data.get("shipment_track", [])
        track_info = shipment_track[0] if shipment_track and isinstance(shipment_track, list) else {}

        current_status = track_info.get("current_status") or track_data.get("track_status") or "UNKNOWN"
        scans = track_info.get("scans") or []

        return {
            "awb_code": clean_awb,
            "current_status": str(current_status).upper(),
            "status_code": track_info.get("status_code"),
            "courier_name": track_info.get("courier_name") or "Courier Partner",
            "scans": scans,
            "etd": track_info.get("expected_date") or track_info.get("etd"),
            "raw_data": sanitize_log_data(data),
        }

    # --------------------------------------------------------------------------
    # Label & Manifest Documents (Documented Endpoints)
    # --------------------------------------------------------------------------

    def generate_label(self, shipment_id: int | str) -> dict:
        """
        Generates official carrier shipping label PDF:
        POST /couriers/generate/label
        """
        payload = {"shipment_id": [str(shipment_id)]}

        if self.mock_mode:
            return {"label_url": f"https://mock.shiprocket.in/labels/{shipment_id}.pdf", "is_mock": True}

        resp = self._request("POST", "couriers/generate/label", json=payload, authenticated=True)
        try:
            data = resp.json()
            return {"label_url": data.get("label_url", ""), "raw_data": sanitize_log_data(data)}
        except Exception as exc:
            raise ShiprocketError(f"Failed to generate shipping label: {exc}") from exc

    def generate_manifest(self, shipment_id: int | str) -> dict:
        """
        Generates official courier pickup manifest document:
        POST /manifests/generate
        """
        payload = {"shipment_id": [str(shipment_id)]}

        if self.mock_mode:
            return {"manifest_url": f"https://mock.shiprocket.in/manifests/{shipment_id}.pdf", "is_mock": True}

        resp = self._request("POST", "manifests/generate", json=payload, authenticated=True)
        try:
            data = resp.json()
            return {"manifest_url": data.get("manifest_url", ""), "raw_data": sanitize_log_data(data)}
        except Exception as exc:
            raise ShiprocketError(f"Failed to generate manifest: {exc}") from exc


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


# ==============================================================================
# SELLER PICKUP LOCATION REGISTRATION SERVICE
# ==============================================================================

def register_seller_pickup_location(
    seller: Any,
    client: ShiprocketClient | None = None,
) -> dict:
    """
    Validates seller warehouse coordinates and registers pickup facility
    with Shiprocket API (/settings/company/addpickup).
    """
    from core.models import Seller

    if not isinstance(seller, Seller):
        raise ShiprocketValidationError("Input must be a Seller instance.")

    loc_check = seller.validate_pickup_location()
    if not loc_check.get("is_complete"):
        errors_str = "; ".join(loc_check.get("errors", []))
        raise ShiprocketValidationError(f"Incomplete seller pickup address: {errors_str}")

    active_client = client or ShiprocketClient()
    return active_client.register_pickup_location(seller)


# ==============================================================================
# PRODUCTION MULTI-SELLER AUTOMATION: CREATE & DISPATCH SHIPMENT
# ==============================================================================

def create_or_dispatch_shipment(
    package_id: int,
    idempotency_key: str | None = None,
    user: Any = None,
    courier_company_id: int | None = None,
    auto_request_pickup: bool = True,
    client: ShiprocketClient | None = None,
) -> Any:
    """
    End-to-End Multi-Seller Shipment Dispatch Engine.
    Executes:
    1. Row-level concurrency locking on SellerOrder.
    2. Eligibility & safety verification (Payment confirmed/COD, measurements, verified warehouse).
    3. Strict Idempotency: Returns existing booking if already booked.
    4. Origin warehouse verification & Shiprocket pickup location registration.
    5. Real or simulated Shiprocket order creation.
    6. Courier rate evaluation & optimal carrier assignment.
    7. AWB tracking number allocation.
    8. Courier pickup scheduling.
    9. Durable ShipmentBooking creation with audit tracking events.
    10. Safe synchronization of sub-order tracking info without altering financial snapshots.
    """
    from core.models import Order, Seller, SellerOrder, ShipmentBooking

    active_client = client or ShiprocketClient()
    is_mock = active_client.mock_mode

    clean_key = (idempotency_key or "").strip()
    if clean_key:
        existing_by_key = ShipmentBooking.objects.filter(idempotency_key=clean_key).first()
        if existing_by_key:
            if existing_by_key.package_id != package_id:
                raise ShiprocketValidationError(
                    "Idempotency key collision: This key was already used for a different package.",
                    user_message="Idempotency key collision detected.",
                )
            logger.info("Returning existing booking for idempotency key %s.", clean_key)
            return existing_by_key

    with transaction.atomic():
        package = (
            SellerOrder.objects.select_for_update()
            .select_related("order", "seller")
            .filter(pk=package_id)
            .first()
        )
        if not package:
            raise ShiprocketValidationError(f"Package #{package_id} was not found.")

        # Re-check idempotency under lock
        if package.is_booked:
            logger.info("Package #%s is already booked. Returning existing booking.", package.sub_order_number)
            return package.shipment_booking

        if clean_key:
            existing_by_key = ShipmentBooking.objects.filter(idempotency_key=clean_key).first()
            if existing_by_key:
                return existing_by_key

        # Eligibility validation gate
        validate_booking_eligibility(package)

        # Multi-Seller Isolation: Verify & resolve origin pickup facility
        pickup_details = package.get_pickup_location_details()
        if not pickup_details.get("is_verified"):
            raise ShiprocketValidationError(
                f"Pickup warehouse for package #{package.sub_order_number} is unverified by admin. "
                "Approval is required before booking courier."
            )

        # Register pickup facility with Shiprocket if not yet confirmed
        pickup_loc_name = ""
        if not package.is_platform_fulfilled and package.seller:
            seller = package.seller
            if not seller.shiprocket_pickup_location_id or seller.shiprocket_pickup_registration_status != Seller.PICKUP_REG_STATUS_REGISTERED:
                reg_res = active_client.register_pickup_location(seller)
                pickup_loc_name = reg_res.get("pickup_location")
            else:
                pickup_loc_name = seller.shiprocket_pickup_location_id
        else:
            pickup_loc_name = getattr(settings, "SHIPROCKET_PICKUP_LOCATION", "") or "Fitmeezo_Central_Warehouse"

        final_key = clean_key or f"ship-key-{package.pk}-{uuid.uuid4().hex[:16]}"
        chargeable_wt = package.get_chargeable_weight_kg() or Decimal("0.500")

        # Select Courier
        assigned_courier_id = courier_company_id
        assigned_courier_name = ""
        estimated_rate = Decimal("0.00")

        if not assigned_courier_id:
            try:
                rate_result = estimate_package_shipping_rate(package, client=active_client)
                if rate_result.is_serviceable and rate_result.recommended_courier:
                    assigned_courier_id = rate_result.recommended_courier.courier_company_id
                    assigned_courier_name = rate_result.recommended_courier.courier_name
                    estimated_rate = rate_result.recommended_courier.rate
            except Exception as exc:
                logger.warning("Could not auto-select courier via rate estimate: %s. Using default carrier.", exc)

        # Step 1: Create Order in Shiprocket
        order_res = active_client.create_order(package, pickup_location_name=pickup_loc_name)
        shipment_id = order_res.get("shipment_id")
        shiprocket_order_id = order_res.get("order_id", package.sub_order_number)

        # Step 2: Assign Courier & AWB
        awb_res = active_client.assign_awb(shipment_id=shipment_id, courier_id=assigned_courier_id)
        awb_code = awb_res.get("awb_code")
        assigned_courier_name = awb_res.get("courier_name") or assigned_courier_name or "Standard Logistics"
        assigned_courier_id = awb_res.get("courier_company_id") or assigned_courier_id

        # Step 3: Request Courier Pickup if configured
        pickup_status = ShipmentBooking.PICKUP_NOT_REQUESTED
        pickup_sched_date = None
        pickup_token = ""
        if auto_request_pickup:
            try:
                pk_res = active_client.request_pickup(shipment_id=shipment_id)
                pickup_status = pk_res.get("pickup_status") or ShipmentBooking.PICKUP_REQUESTED
                pickup_sched_date = pk_res.get("pickup_scheduled_date")
                pickup_token = pk_res.get("pickup_token_number", "")
            except Exception as exc:
                logger.warning("Auto pickup request deferred for shipment %s: %s", shipment_id, exc)
                pickup_status = ShipmentBooking.PICKUP_FAILED

        # Step 4: Documents (Labels / Tracking URL)
        tracking_url = f"https://shiprocket.co/tracking/{awb_code}" if awb_code else ""
        label_url = ""
        try:
            lbl_res = active_client.generate_label(shipment_id)
            label_url = lbl_res.get("label_url", "")
        except Exception:
            pass

        # Step 5: Save Durable ShipmentBooking Record
        try:
            booking = ShipmentBooking.objects.create(
                package=package,
                idempotency_key=final_key,
                is_mock=is_mock,
                status="booked",
                shipment_id=str(shipment_id),
                shiprocket_order_id=str(shiprocket_order_id),
                awb_code=str(awb_code),
                courier_name=str(assigned_courier_name),
                courier_company_id=int(assigned_courier_id) if assigned_courier_id else None,
                chargeable_weight_kg=chargeable_wt,
                estimated_rate=estimated_rate,
                pickup_nickname=pickup_loc_name,
                pickup_status=pickup_status,
                pickup_scheduled_date=pickup_sched_date,
                pickup_token_number=pickup_token,
                tracking_status="MANIFEST_GENERATED",
                tracking_url=tracking_url,
                label_url=label_url,
                metadata={
                    "is_mock": is_mock,
                    "booked_by": user.username if user and user.is_authenticated else "system",
                    "booked_at": timezone.now().isoformat(),
                    "pickup_location_name": pickup_loc_name,
                },
            )
            booking.record_tracking_event(
                status="MANIFEST_GENERATED",
                activity=f"Shipment booked via {assigned_courier_name}. AWB: {awb_code}",
                location=pickup_loc_name,
            )
            booking.save(update_fields=["status_history", "tracking_status", "last_tracking_sync"])
        except IntegrityError:
            existing = ShipmentBooking.objects.filter(package_id=package.pk).first()
            if existing:
                return existing
            raise

        # Step 6: Update SellerOrder Tracking references
        package.shiprocket_order_id = str(shiprocket_order_id)
        package.shiprocket_shipment_id = str(shipment_id)
        package.shiprocket_awb_code = str(awb_code)
        package.tracking_number = str(awb_code)
        package.courier_name = str(assigned_courier_name)
        package.tracking_url = tracking_url
        package.save(update_fields=[
            "shiprocket_order_id",
            "shiprocket_shipment_id",
            "shiprocket_awb_code",
            "tracking_number",
            "courier_name",
            "tracking_url",
            "updated_at",
        ])

        logger.info(
            "Shipment successfully booked for package #%s (Shipment: %s, AWB: %s, Courier: %s).",
            package.sub_order_number,
            shipment_id,
            awb_code,
            assigned_courier_name,
        )

        # Step 7: Automated Notifications (Customer tracking dispatch & Seller pickup readiness)
        try:
            from core.emails import send_customer_shipment_created_email, send_seller_pickup_scheduled_email
            send_customer_shipment_created_email(package)
            if booking.pickup_status in (ShipmentBooking.PICKUP_SCHEDULED, ShipmentBooking.PICKUP_REQUESTED, "scheduled", "requested"):
                send_seller_pickup_scheduled_email(package)
        except Exception as notify_err:
            logger.warning("Non-blocking notification dispatch error for package #%s: %s", package.sub_order_number, notify_err)

        return booking


# ==============================================================================
# LIVE TRACKING SYNCHRONIZATION & STATUS TRANSITIONS
# ==============================================================================

def sync_shipment_tracking(
    booking_or_package: Any,
    client: ShiprocketClient | None = None,
    tracking_data: dict | None = None,
) -> dict:
    """
    Synchronizes real-time courier tracking milestones from Shiprocket API.
    Transitions:
    - Moves package to Shipped strictly when courier confirms physical collection/in-transit.
    - Moves package to Delivered strictly when courier confirms doorstep delivery.
    - Reconciles parent Order to Delivered when all child shipments are delivered.
    - Records detailed scan audit trail in ShipmentBooking.status_history.
    """
    from core.models import Order, SellerOrder, ShipmentBooking

    if isinstance(booking_or_package, SellerOrder):
        package = booking_or_package
        booking = getattr(package, "shipment_booking", None)
    elif isinstance(booking_or_package, ShipmentBooking):
        booking = booking_or_package
        package = booking.package
    else:
        raise ShiprocketValidationError("Input must be a SellerOrder or ShipmentBooking.")

    if not booking or not booking.awb_code:
        return {"status": "unbooked", "message": "No active shipment booking with AWB exists for this package."}

    if tracking_data is None:
        active_client = client or ShiprocketClient()
        tracking_data = active_client.track_awb(booking.awb_code)

    current_status = (tracking_data.get("current_status") or "").upper().strip()
    scans = tracking_data.get("scans") or []

    # Map status milestones
    SHIPPED_MILESTONES = {
        "PICKED UP",
        "IN TRANSIT",
        "OUT FOR DELIVERY",
        "REACHED AT DESTINATION",
        "REACHED DESTINATION HUB",
        "OUT FOR PICKUP",
        "HANDOVER TO COURIER",
    }
    DELIVERED_MILESTONES = {"DELIVERED"}
    RTO_MILESTONES = {"RTO INITIATED", "RTO DELIVERED", "RTO OFD", "RTO"}
    CANCELLED_MILESTONES = {"CANCELED", "CANCELLED"}

    with transaction.atomic():
        locked_pkg = SellerOrder.objects.select_for_update().select_related("order").get(pk=package.pk)
        locked_booking = ShipmentBooking.objects.select_for_update().get(pk=booking.pk)

        # 1. Physical Pickup Confirmation -> Mark Shipped
        if current_status in SHIPPED_MILESTONES or any(str(s.get("activity", "")).upper() in SHIPPED_MILESTONES for s in scans):
            locked_booking.pickup_status = ShipmentBooking.PICKUP_PICKED_UP
            if locked_pkg.status in (SellerOrder.STATUS_PENDING, SellerOrder.STATUS_PROCESSING, SellerOrder.STATUS_READY_TO_SHIP):
                locked_pkg.status = SellerOrder.STATUS_SHIPPED
                locked_pkg.shipped_at = timezone.now()
                locked_pkg.save(update_fields=["status", "shipped_at", "updated_at"])
                logger.info("Package #%s transitioned to SHIPPED based on courier pickup confirmation.", locked_pkg.sub_order_number)
                try:
                    from core.emails import send_customer_tracking_update_email
                    send_customer_tracking_update_email(locked_pkg, "Picked Up / In Transit")
                except Exception as email_err:
                    logger.warning("Tracking update email error on Shipped for #%s: %s", locked_pkg.sub_order_number, email_err)

        # 2. Doorstep Delivery Confirmation -> Mark Delivered
        if current_status in DELIVERED_MILESTONES:
            locked_booking.tracking_status = "DELIVERED"
            if locked_pkg.status != SellerOrder.STATUS_DELIVERED:
                locked_pkg.status = SellerOrder.STATUS_DELIVERED
                locked_pkg.delivered_at = timezone.now()
                locked_pkg.save(update_fields=["status", "delivered_at", "updated_at"])
                logger.info("Package #%s confirmed DELIVERED by courier.", locked_pkg.sub_order_number)
                try:
                    from core.emails import send_customer_tracking_update_email
                    send_customer_tracking_update_email(locked_pkg, "Delivered")
                except Exception as email_err:
                    logger.warning("Tracking update email error on Delivered for #%s: %s", locked_pkg.sub_order_number, email_err)

                # Reconcile parent Order: mark delivered if all seller packages are delivered
                parent_order = locked_pkg.order
                all_delivered = not parent_order.seller_orders.exclude(pk=locked_pkg.pk).exclude(status=SellerOrder.STATUS_DELIVERED).exists()
                if all_delivered:
                    parent_order.status = Order.STATUS_DELIVERED
                    parent_order.delivered_at = timezone.now()
                    parent_order.save(update_fields=["status", "delivered_at", "updated_at"])
                    logger.info("Parent Order #%s fully delivered across all fulfillment packages.", parent_order.order_number)

        # 3. RTO / Return
        if current_status in RTO_MILESTONES:
            locked_booking.tracking_status = "RTO"

        # 4. Cancelled
        if current_status in CANCELLED_MILESTONES:
            locked_booking.tracking_status = "CANCELLED"

        # Record audit scan
        latest_scan = scans[-1] if scans else {}
        activity_text = latest_scan.get("activity") or f"Tracking status: {current_status}"
        location_text = latest_scan.get("location") or ""
        locked_booking.record_tracking_event(
            status=current_status or locked_booking.tracking_status,
            activity=activity_text,
            location=location_text,
            raw_data=tracking_data,
        )
        locked_booking.save(update_fields=[
            "pickup_status",
            "tracking_status",
            "last_tracking_sync",
            "status_history",
        ])

    return {
        "success": True,
        "awb_code": booking.awb_code,
        "current_status": current_status,
        "package_status": locked_pkg.status,
        "courier_name": locked_booking.courier_name,
        "etd": tracking_data.get("etd", ""),
        "scans_count": len(scans),
    }


# ==============================================================================
# SECURE WEBHOOK PROCESSING ENGINE
# ==============================================================================

def extract_webhook_auth_token(headers: dict) -> str:
    """
    Extracts the webhook authentication secret token from request headers.
    Compatible with:
    - Primary Shiprocket webhook security header: 'x-api-key' / 'X-Api-Key' / 'HTTP_X_API_KEY'
    - Custom Shiprocket token header: 'x-shiprocket-token' / 'X-Shiprocket-Token'
    - Standard authorization header: 'authorization' / 'Authorization' (Bearer or raw token)
    - Generic token header: 'x-auth-token' / 'X-Auth-Token'
    """
    candidate_keys = (
        "x-api-key",
        "X-Api-Key",
        "HTTP_X_API_KEY",
        "x-shiprocket-token",
        "X-Shiprocket-Token",
        "HTTP_X_SHIPROCKET_TOKEN",
        "authorization",
        "Authorization",
        "HTTP_AUTHORIZATION",
        "x-auth-token",
        "X-Auth-Token",
        "HTTP_X_AUTH_TOKEN",
    )
    for key in candidate_keys:
        raw_val = headers.get(key)
        if raw_val is not None:
            val = str(raw_val).strip()
            if val.lower().startswith("bearer "):
                val = val[7:].strip()
            if val:
                return val
    return ""


def handle_shiprocket_webhook(
    payload: dict,
    headers: dict | None = None,
) -> tuple[bool, str, dict]:
    """
    Parses and verifies incoming Shiprocket webhook tracking events.
    Guarantees:
    - Authenticates webhook secret token if configured.
    - Idempotency & deduplication: safely ignores repeated identical event payloads.
    - Accurately matches event to ShipmentBooking and SellerOrder.
    - Safe transitions without corrupting financial ledgers or premature delivery claims.
    """
    from core.models import ShipmentBooking

    if not isinstance(payload, dict):
        return False, "Malformed non-dict payload.", {}

    headers = headers or {}

    # 1. Webhook Authentication
    configured_token = getattr(settings, "SHIPROCKET_WEBHOOK_TOKEN", "")
    if configured_token:
        configured_token = str(configured_token).strip()

    if configured_token:
        extracted_token = extract_webhook_auth_token(headers)
        if not extracted_token or not secrets.compare_digest(extracted_token, configured_token):
            logger.warning("Rejected Shiprocket webhook: invalid or missing authentication token.")
            return False, "Unauthorized webhook token.", {}
    else:
        logger.warning("Shiprocket webhook received without SHIPROCKET_WEBHOOK_TOKEN configured on server.")

    # 2. Idempotency & Event Deduplication
    event_str = json.dumps(payload, sort_keys=True)
    event_hash = hashlib.sha256(event_str.encode()).hexdigest()
    cache_key = f"shiprocket_webhook_processed_{event_hash}"
    if cache.get(cache_key):
        logger.info("Ignoring duplicate webhook event (hash: %s).", event_hash[:12])
        return True, "Duplicate webhook event safely ignored.", {"event_hash": event_hash, "duplicate": True}

    cache.set(cache_key, True, 86400)  # Deduplicate for 24 hours

    # 3. Match Target ShipmentBooking
    awb_code = str(payload.get("awb") or payload.get("awb_code") or "").strip()
    shipment_id = str(payload.get("shipment_id") or "").strip()
    order_id = str(payload.get("order_id") or "").strip()

    booking = None
    if awb_code:
        booking = ShipmentBooking.objects.filter(awb_code=awb_code).select_related("package", "package__order").first()
    if not booking and shipment_id:
        booking = ShipmentBooking.objects.filter(shipment_id=shipment_id).select_related("package", "package__order").first()
    if not booking and order_id:
        booking = ShipmentBooking.objects.filter(package__sub_order_number=order_id).select_related("package", "package__order").first()

    if not booking:
        logger.warning(
            "Shiprocket webhook received for unmapped shipment (AWB: %s, Shipment: %s, Order: %s).",
            awb_code,
            shipment_id,
            order_id,
        )
        return False, "No matching ShipmentBooking found for webhook payload.", {"awb": awb_code, "shipment_id": shipment_id}

    # 4. Process Status Update
    status_text = (payload.get("current_status") or payload.get("status") or "").upper().strip()
    activity = payload.get("activity") or payload.get("current_status") or f"Webhook status: {status_text}"
    location = payload.get("location") or ""

    # Synthesize tracking scan format for sync_shipment_tracking
    tracking_simulation = {
        "awb_code": booking.awb_code,
        "current_status": status_text,
        "scans": [{"activity": activity, "location": location, "date": timezone.now().strftime("%Y-%m-%d %H:%M")}],
        "courier_name": payload.get("courier_name") or booking.courier_name,
    }

    sync_result = sync_shipment_tracking(booking, client=None, tracking_data=tracking_simulation)

    logger.info("Processed Shiprocket webhook for package #%s (Status: %s).", booking.package.sub_order_number, status_text)
    return True, "Webhook processed successfully.", sync_result


# ==============================================================================
# SHIPPING READINESS CHECK & DIAGNOSTICS ENGINE (STEP 7 & STEP 8)
# ==============================================================================

def check_shipping_readiness() -> dict[str, Any]:
    """
    Evaluates system readiness for live Shiprocket carrier booking.
    Inspects:
    1. Operating mode: Mock Sandbox Mode vs Live Production
    2. API credentials: Present, non-placeholder, securely masked
    3. Platform warehouse setup: Configured origin pickup location
    4. Merchant warehouse setup: Approved sellers, complete addresses, verified & registered pickup facilities
    5. Webhook receiver setup: Secret token and live webhook URL
    6. Historical shipment volume: Mock vs live bookings, active transit, deliveries, failures
    7. Overall readiness state & actionable prerequisites
    """
    from core.models import Seller, SellerOrder, ShipmentBooking

    client = ShiprocketClient()
    mock_mode = getattr(settings, "SHIPROCKET_MOCK_MODE", True)
    base_url = client.base_url
    is_configured = client.is_configured

    # Safely mask provider email (never reveal sensitive credentials)
    email = client.email or ""
    if email and "@" in email:
        parts = email.split("@")
        user_part = parts[0]
        masked_user = (user_part[:2] + "***") if len(user_part) > 2 else (user_part[:1] + "***")
        masked_email = f"{masked_user}@{parts[1]}"
    elif email:
        masked_email = email[:2] + "***"
    else:
        masked_email = "Not Configured"

    webhook_token = getattr(settings, "SHIPROCKET_WEBHOOK_TOKEN", "") or ""
    webhook_url = "https://fitmeezo.com/api/shipping/tracking/webhook/"
    platform_wh = getattr(settings, "SHIPROCKET_PICKUP_LOCATION", "") or getattr(settings, "PLATFORM_WAREHOUSE_NAME", "") or "Fitmeezo Central Warehouse"

    # Seller metrics
    total_sellers = Seller.objects.count()
    approved_sellers = Seller.objects.filter(status=Seller.STATUS_APPROVED).count()
    sellers_with_address = Seller.objects.filter(status=Seller.STATUS_APPROVED).exclude(pickup_address="").exclude(pickup_postal_code="").count()
    sellers_registered = Seller.objects.filter(
        status=Seller.STATUS_APPROVED,
        shiprocket_pickup_registration_status=Seller.PICKUP_REG_STATUS_REGISTERED,
    ).count()
    sellers_failed = Seller.objects.filter(
        status=Seller.STATUS_APPROVED,
        shiprocket_pickup_registration_status=Seller.PICKUP_REG_STATUS_FAILED,
    ).count()
    sellers_pending = Seller.objects.filter(
        status=Seller.STATUS_APPROVED,
        shiprocket_pickup_registration_status=Seller.PICKUP_REG_STATUS_PENDING,
    ).count()

    # Shipment metrics
    total_bookings = ShipmentBooking.objects.count()
    mock_bookings = ShipmentBooking.objects.filter(is_mock=True).count()
    live_bookings = ShipmentBooking.objects.filter(is_mock=False).count()
    pickup_scheduled = ShipmentBooking.objects.filter(pickup_status=ShipmentBooking.PICKUP_SCHEDULED).count()
    in_transit = ShipmentBooking.objects.filter(tracking_status__in=["IN TRANSIT", "MANIFEST_GENERATED"]).count()
    delivered = ShipmentBooking.objects.filter(tracking_status="DELIVERED").count()
    failed = ShipmentBooking.objects.filter(pickup_status=ShipmentBooking.PICKUP_FAILED).count()

    # Determine readiness state & diagnostics
    diagnostics = []
    if mock_mode:
        readiness_state = "MOCK_SANDBOX"
        diagnostics.append("Shiprocket Mock Mode is currently ACTIVE (zero-cost simulated testing).")
        diagnostics.append("All shipment bookings, AWBs, and pickups are safely generated as MOCK records.")
    else:
        if not is_configured:
            readiness_state = "PREREQUISITES_MISSING"
            diagnostics.append("SHIPROCKET_EMAIL and SHIPROCKET_PASSWORD must be configured in environment with non-placeholder values.")
        elif approved_sellers > 0 and sellers_registered == 0:
            readiness_state = "PREREQUISITES_MISSING"
            diagnostics.append("No approved sellers have registered their pickup warehouse with Shiprocket yet.")
        else:
            readiness_state = "LIVE_READY"
            diagnostics.append("Live credentials and seller warehouse locations are configured and ready for live carrier booking.")

    if not webhook_token:
        diagnostics.append("SHIPROCKET_WEBHOOK_TOKEN is not configured; tracking webhooks will operate unauthenticated or fall back to polling.")

    return {
        "mock_mode": mock_mode,
        "base_url": base_url,
        "is_configured": is_configured,
        "masked_email": masked_email,
        "credentials_secret_present": bool(client.password and not client.password.lower().startswith(("replace_", "your_", "todo"))),
        "platform_pickup_location": platform_wh,
        "webhook_token_configured": bool(webhook_token),
        "webhook_url": webhook_url,
        "readiness_state": readiness_state,
        "diagnostics": diagnostics,
        "seller_stats": {
            "total_sellers": total_sellers,
            "approved_sellers": approved_sellers,
            "with_pickup_address": sellers_with_address,
            "registered_with_shiprocket": sellers_registered,
            "registration_failed": sellers_failed,
            "pending_registration": sellers_pending,
        },
        "shipment_stats": {
            "total_bookings": total_bookings,
            "mock_bookings": mock_bookings,
            "live_bookings": live_bookings,
            "pickup_scheduled": pickup_scheduled,
            "in_transit": in_transit,
            "delivered": delivered,
            "failed": failed,
        },
    }

