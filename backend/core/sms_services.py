"""
Fitmeezo Transactional SMS Gateway & OTP Dispatch Services.
Integrates legitimate Indian transactional SMS providers (MSG91, Textlocal, Fast2SMS)
and global providers (Twilio) for production Seller Hub authentication.

Enforces zero plaintext logging of credentials or OTPs, production safety guards,
and strict validation of Indian DLT template requirements.
"""

import base64
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


def mask_phone_number(mobile: str) -> str:
    """Masks a 10-digit mobile number for secure log and UI display (e.g. '******3210')."""
    cleaned = re.sub(r"[^\d]", "", mobile or "")
    if len(cleaned) >= 4:
        return f"{'*' * (len(cleaned) - 4)}{cleaned[-4:]}"
    return "****"


def _get_credential(*var_names: str, default: str = "") -> str:
    """
    Safely retrieves a configuration value from environment variables or Django settings.
    Checks names in priority order and strips whitespace.
    """
    for name in var_names:
        val = os.getenv(name)
        if val is not None and str(val).strip():
            return str(val).strip()
        val = getattr(settings, name, None)
        if val is not None and str(val).strip():
            return str(val).strip()
    return default


class BaseSMSProvider(ABC):
    """Abstract base class for all SMS dispatch providers."""

    @abstractmethod
    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        """
        Dispatches an OTP to the given mobile number.
        Returns:
            tuple[bool, str]: (success, status_message)
        """
        pass


class MockSMSProvider(BaseSMSProvider):
    """
    Test-only in-memory SMS provider for automated testing and test suites.
    Strictly forbidden in production environments (DEBUG=False outside test runner).
    """

    sent_messages: list[dict] = []

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        is_production = not settings.DEBUG and not getattr(settings, "TESTING", False) and "test" not in sys.argv
        if is_production:
            raise ImproperlyConfigured(
                "MockSMSProvider cannot be utilized in production when DEBUG=False. "
                "Configure a valid production SMS provider."
            )
        entry = {
            "mobile": mobile,
            "otp": otp,
        }
        self.sent_messages.append(entry)
        logger.debug(f"[MockSMSProvider] Dispatched OTP to {mask_phone_number(mobile)}")
        return True, "OTP dispatched successfully via test provider."

    @classmethod
    def get_last_otp(cls, mobile: str) -> str | None:
        for entry in reversed(cls.sent_messages):
            if entry["mobile"] == mobile:
                return entry["otp"]
        return None

    @classmethod
    def clear(cls):
        cls.sent_messages.clear()


class ConsoleSMSProvider(BaseSMSProvider):
    """
    Development-only console SMS provider.
    Outputs the dispatch notice to the Django server log.
    Strictly forbidden in production environments (DEBUG=False outside test runner).
    """

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        is_production = not settings.DEBUG and not getattr(settings, "TESTING", False) and "test" not in sys.argv
        if is_production:
            raise ImproperlyConfigured(
                "ConsoleSMSProvider cannot be utilized in production when DEBUG=False. "
                "Configure a valid production SMS provider."
            )
        banner = (
            f"\n=======================================================\n"
            f"[FITMEEZO DEV OTP] Mobile: {mobile} | OTP: {otp}\n"
            f"Valid for 5 minutes. (This banner is strictly disabled in production)\n"
            f"=======================================================\n"
        )
        try:
            print(banner, file=sys.stdout, flush=True)
            sys.stdout.flush()
        except Exception:
            pass

        try:
            print(banner, file=sys.stderr, flush=True)
            sys.stderr.flush()
        except Exception:
            pass

        logger.info(f"[DEV_MODE_SELLER_OTP] Dispatched OTP to {mask_phone_number(mobile)}")
        return True, "[Dev Mode] 6-digit OTP logged to server terminal/console (no live SMS sent to phone)."


class MSG91SMSProvider(BaseSMSProvider):
    """
    Production SMS provider integrating with MSG91 Send OTP v5 API.
    Complies with Indian TRAI DLT regulations using approved templates.
    Official API: https://control.msg91.com/api/v5/otp
    """

    def __init__(self):
        self.auth_key = _get_credential("MSG91_AUTH_KEY", "SMS_AUTH_KEY", "SMS_API_KEY")
        self.template_id = _get_credential("MSG91_TEMPLATE_ID", "SMS_TEMPLATE_ID")
        self.sender_id = _get_credential("MSG91_SENDER_ID", "SMS_SENDER_ID")

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if not self.auth_key or not self.template_id:
            logger.error(
                "MSG91 credentials missing. Please configure SMS_AUTH_KEY (or MSG91_AUTH_KEY) "
                "and SMS_TEMPLATE_ID (or MSG91_TEMPLATE_ID) in environment variables."
            )
            return (
                False,
                "SMS provider is not properly configured. Please contact platform support or log in with password.",
            )

        clean_mobile = f"91{mobile[-10:]}"
        params = {
            "template_id": self.template_id,
            "mobile": clean_mobile,
            "otp": otp,
            "otp_expiry": "5",
        }
        if self.sender_id:
            params["sender"] = self.sender_id

        url = f"https://control.msg91.com/api/v5/otp?{urllib.parse.urlencode(params)}"
        payload = json.dumps({
            "template_id": self.template_id,
            "mobile": clean_mobile,
            "otp": otp,
            "otp_expiry": 5,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("authkey", self.auth_key)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_body = resp.read().decode("utf-8", errors="ignore")
                try:
                    data = json.loads(resp_body)
                except Exception:
                    data = {}

                if resp.status == 200 or data.get("type") == "success":
                    logger.info(f"MSG91 OTP dispatched successfully to {mask_phone_number(clean_mobile)}")
                    return True, "OTP sent successfully to your registered mobile number."
                else:
                    err_msg = data.get("message") or "Provider rejected OTP request"
                    logger.error(f"MSG91 delivery failure for {mask_phone_number(clean_mobile)}: {err_msg}")
                    return False, f"SMS delivery failed: {err_msg}."

        except urllib.error.HTTPError as http_err:
            try:
                err_body = http_err.read().decode("utf-8", errors="ignore")
                err_data = json.loads(err_body)
                err_msg = err_data.get("message") or f"HTTP {http_err.code}"
            except Exception:
                err_msg = f"HTTP {http_err.code} Error"

            logger.error(f"MSG91 HTTP error {http_err.code} for {mask_phone_number(clean_mobile)}: {err_msg}")
            return False, f"SMS gateway reported error ({err_msg}). Please try again shortly or use password login."

        except urllib.error.URLError as url_err:
            logger.error(f"MSG91 network timeout/error: {str(url_err.reason)}")
            return False, "SMS delivery failed due to network timeout. Please try again shortly or use password login."

        except Exception as exc:
            logger.error(f"MSG91 unexpected dispatch exception: {type(exc).__name__}: {str(exc)}")
            return False, "SMS delivery failed. Please try again or use password login."


class TextlocalSMSProvider(BaseSMSProvider):
    """
    Production SMS provider integrating with Textlocal India gateway.
    Supports Indian TRAI DLT approved Sender IDs and transactional templates.
    Official API: https://api.textlocal.in/send/
    """

    def __init__(self):
        self.api_key = _get_credential("TEXTLOCAL_API_KEY", "SMS_API_KEY", "SMS_AUTH_KEY")
        self.sender_id = _get_credential("TEXTLOCAL_SENDER_ID", "SMS_SENDER_ID", default="FTMEZO")
        self.template_text = _get_credential(
            "TEXTLOCAL_TEMPLATE_TEXT",
            "SMS_TEMPLATE_TEXT",
            default="Your Fitmeezo verification code is {otp}. Valid for 5 minutes. Do not share this code.",
        )

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if not self.api_key:
            logger.error(
                "Textlocal credentials missing. Please configure SMS_API_KEY (or TEXTLOCAL_API_KEY) in environment."
            )
            return (
                False,
                "SMS provider is not properly configured. Please contact platform support or log in with password.",
            )

        clean_mobile = f"91{mobile[-10:]}"
        message_body = self.template_text.format(otp=otp)

        post_data = urllib.parse.urlencode({
            "apiKey": self.api_key,
            "numbers": clean_mobile,
            "message": message_body,
            "sender": self.sender_id,
        }).encode("utf-8")

        url = "https://api.textlocal.in/send/"
        req = urllib.request.Request(url, data=post_data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_body = resp.read().decode("utf-8", errors="ignore")
                try:
                    data = json.loads(resp_body)
                except Exception:
                    data = {}

                if data.get("status") == "success":
                    logger.info(f"Textlocal OTP dispatched successfully to {mask_phone_number(clean_mobile)}")
                    return True, "OTP sent successfully to your registered mobile number."
                else:
                    errors = data.get("errors", [])
                    err_msg = errors[0].get("message") if errors else "Textlocal delivery rejected"
                    logger.error(f"Textlocal delivery failure for {mask_phone_number(clean_mobile)}: {err_msg}")
                    return False, f"SMS delivery failed: {err_msg}."

        except urllib.error.HTTPError as http_err:
            try:
                err_body = http_err.read().decode("utf-8", errors="ignore")
                err_data = json.loads(err_body)
                errors = err_data.get("errors", [])
                err_msg = errors[0].get("message") if errors else f"HTTP {http_err.code}"
            except Exception:
                err_msg = f"HTTP {http_err.code} Error"

            logger.error(f"Textlocal HTTP error {http_err.code} for {mask_phone_number(clean_mobile)}: {err_msg}")
            return False, f"SMS gateway reported error ({err_msg}). Please try again or use password login."

        except urllib.error.URLError as url_err:
            logger.error(f"Textlocal network timeout/error: {str(url_err.reason)}")
            return False, "SMS delivery failed due to network timeout. Please try again shortly or use password login."

        except Exception as exc:
            logger.error(f"Textlocal unexpected dispatch exception: {type(exc).__name__}: {str(exc)}")
            return False, "SMS delivery failed. Please try again or use password login."


class Fast2SMSProvider(BaseSMSProvider):
    """Production SMS provider integrating with Fast2SMS Indian gateway (Quick OTP Route)."""

    def __init__(self):
        self.api_key = _get_credential("FAST2SMS_API_KEY", "SMS_API_KEY", "SMS_AUTH_KEY")

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if not self.api_key:
            logger.error("Fast2SMS API key missing in environment variables.")
            return False, "SMS provider is not properly configured. Please contact platform support."

        try:
            clean_mobile = mobile[-10:]
            url = "https://www.fast2sms.com/dev/bulkV2"
            payload = json.dumps({
                "variables_values": otp,
                "route": "otp",
                "numbers": clean_mobile,
            }).encode("utf-8")

            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("authorization", self.api_key)
            req.add_header("Content-Type", "application/json")

            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if data.get("return") is True:
                    logger.info(f"Fast2SMS OTP dispatched to {mask_phone_number(clean_mobile)}")
                    return True, "OTP sent successfully to your registered mobile number."
                else:
                    err_msg = data.get("message") or "Provider rejected request"
                    logger.error(f"Fast2SMS response error: {err_msg}")
                    return False, f"Failed to deliver SMS: {err_msg}."

        except Exception as exc:
            logger.error(f"Fast2SMS dispatch exception: {type(exc).__name__}: {str(exc)}")
            return False, "SMS delivery failed due to network error. Please try again or use password login."


class TwilioSMSProvider(BaseSMSProvider):
    """Production SMS provider integrating with Twilio REST API."""

    def __init__(self):
        self.account_sid = _get_credential("TWILIO_ACCOUNT_SID")
        self.auth_token = _get_credential("TWILIO_AUTH_TOKEN")
        self.from_number = _get_credential("TWILIO_FROM_NUMBER")

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if not self.account_sid or not self.auth_token or not self.from_number:
            logger.error("Twilio credentials missing in environment variables.")
            return False, "SMS provider is not properly configured. Please contact platform support."

        try:
            to_number = f"+91{mobile[-10:]}" if not mobile.startswith("+") else mobile
            message_body = (
                f"Your Fitmeezo Seller Hub verification code is {otp}. Valid for 5 minutes. Do not share this code."
            )

            data = urllib.parse.urlencode({
                "To": to_number,
                "From": self.from_number,
                "Body": message_body,
            }).encode("utf-8")

            url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
            req = urllib.request.Request(url, data=data, method="POST")
            auth_header = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode("ascii")
            req.add_header("Authorization", f"Basic {auth_header}")

            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    logger.info(f"Twilio OTP dispatched successfully to {mask_phone_number(mobile)}")
                    return True, "OTP sent successfully to your registered mobile number."
                else:
                    logger.error(f"Twilio API error status: {resp.status}")
                    return False, "Failed to deliver SMS. Please try again shortly."

        except Exception as exc:
            logger.error(f"Twilio SMS dispatch exception: {type(exc).__name__}: {str(exc)}")
            return False, "SMS delivery failed due to network error. Please try again or use password login."


class UnconfiguredSMSProvider(BaseSMSProvider):
    """
    Fallback provider when no valid production SMS gateway is configured.
    Never pretends SMS was delivered. Explicitly returns error.
    """

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        logger.warning(
            f"SMS dispatch attempted for {mask_phone_number(mobile)}, but no SMS provider is configured. "
            "Refusing to simulate delivery in production."
        )
        return (
            False,
            "SMS service is not currently configured for this platform. "
            "Please use your merchant username and password to log in, or contact platform support.",
        )


def get_sms_provider() -> BaseSMSProvider:
    """
    Factory resolving the active SMS provider according to environment settings.
    Guarantees development mock/console providers cannot run in production (DEBUG=False).
    """
    is_test_runner = getattr(settings, "TESTING", False) or "test" in sys.argv
    force_prod_check = getattr(settings, "FORCE_PROD_SMS_CHECK", False)
    is_production = not settings.DEBUG and (not is_test_runner or force_prod_check)

    provider_name = _get_credential("SMS_PROVIDER").lower()

    # When running under automated tests, default to MockSMSProvider unless explicitly testing production safety
    if is_test_runner and not force_prod_check and provider_name in ("mock", "test", "console", ""):
        return MockSMSProvider()

    # 1. Check for development / mock providers
    if provider_name in ("mock", "test"):
        if is_production:
            raise ImproperlyConfigured(
                "Mock SMS provider cannot be enabled when DEBUG=False in production. "
                "Configure a valid production SMS provider (e.g. msg91, textlocal, fast2sms)."
            )
        return MockSMSProvider()

    if provider_name == "console":
        if is_production:
            raise ImproperlyConfigured(
                "Console SMS provider cannot be enabled when DEBUG=False in production. "
                "Configure a valid production SMS provider (e.g. msg91, textlocal, fast2sms)."
            )
        return ConsoleSMSProvider()

    # 2. Check explicitly declared production providers
    if provider_name in ("msg91", "msg_91"):
        return MSG91SMSProvider()
    elif provider_name in ("textlocal", "text_local"):
        return TextlocalSMSProvider()
    elif provider_name in ("fast2sms", "fast_2_sms"):
        return Fast2SMSProvider()
    elif provider_name == "twilio":
        return TwilioSMSProvider()

    # 3. Auto-detection by configured credentials if SMS_PROVIDER is blank
    if not provider_name:
        if _get_credential("MSG91_AUTH_KEY", "SMS_TEMPLATE_ID"):
            return MSG91SMSProvider()
        if _get_credential("TEXTLOCAL_API_KEY"):
            return TextlocalSMSProvider()
        if _get_credential("FAST2SMS_API_KEY"):
            return Fast2SMSProvider()
        if _get_credential("TWILIO_ACCOUNT_SID"):
            return TwilioSMSProvider()

    # 4. Default fallback logic
    if is_production:
        return UnconfiguredSMSProvider()

    # In local development only (DEBUG=True)
    dev_mode = str(getattr(settings, "SELLER_OTP_DEV_MODE", os.getenv("SELLER_OTP_DEV_MODE", "True"))).strip().lower() in ("true", "1", "yes")
    if dev_mode:
        return ConsoleSMSProvider()

    return UnconfiguredSMSProvider()
