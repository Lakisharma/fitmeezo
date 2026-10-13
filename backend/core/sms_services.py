import logging
import os
import re
import sys
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
        # Note: In development mode only, display for developer testing with explicit flush
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
            # Also mirror to sys.stderr so servers that only pipe stderr to console display it
            print(banner, file=sys.stderr, flush=True)
            sys.stderr.flush()
        except Exception:
            pass

        logger.info(f"[DEV_MODE_SELLER_OTP] Dispatched OTP to {mask_phone_number(mobile)}")
        return True, "[Dev Mode] 6-digit OTP logged to server terminal/console (no live SMS sent to phone)."


class TwilioSMSProvider(BaseSMSProvider):
    """Production SMS provider integrating with Twilio REST API."""

    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        self.from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if not self.account_sid or not self.auth_token or not self.from_number:
            logger.error("Twilio credentials missing in environment variables.")
            return False, "SMS provider is not properly configured. Please contact platform support."

        try:
            import urllib.parse
            import urllib.request
            import base64

            to_number = f"+91{mobile}" if not mobile.startswith("+") else mobile
            message_body = f"Your Fitmeezo Seller Hub verification code is {otp}. Valid for 5 minutes. Do not share this code."

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


class Fast2SMSProvider(BaseSMSProvider):
    """Production SMS provider integrating with Fast2SMS Indian gateway."""

    def __init__(self):
        self.api_key = os.getenv("FAST2SMS_API_KEY", "").strip()

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if not self.api_key:
            logger.error("Fast2SMS API key missing in environment variables.")
            return False, "SMS provider is not properly configured. Please contact platform support."

        try:
            import json
            import urllib.request

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
                data = json.loads(resp.read().decode())
                if data.get("return") is True:
                    logger.info(f"Fast2SMS OTP dispatched to {mask_phone_number(clean_mobile)}")
                    return True, "OTP sent successfully to your registered mobile number."
                else:
                    logger.error(f"Fast2SMS response error: {data.get('message')}")
                    return False, "Failed to deliver SMS. Please try again shortly."
        except Exception as exc:
            logger.error(f"Fast2SMS dispatch exception: {type(exc).__name__}: {str(exc)}")
            return False, "SMS delivery failed due to network error. Please try again or use password login."


class MSG91SMSProvider(BaseSMSProvider):
    """Production SMS provider integrating with MSG91 gateway."""

    def __init__(self):
        self.auth_key = os.getenv("MSG91_AUTH_KEY", "").strip()
        self.template_id = os.getenv("MSG91_TEMPLATE_ID", "").strip()

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        if not self.auth_key or not self.template_id:
            logger.error("MSG91 credentials missing in environment variables.")
            return False, "SMS provider is not properly configured. Please contact platform support."

        try:
            import json
            import urllib.request

            clean_mobile = f"91{mobile[-10:]}"
            url = f"https://control.msg91.com/api/v5/otp?template_id={self.template_id}&mobile={clean_mobile}&otp={otp}"
            req = urllib.request.Request(url, method="POST")
            req.add_header("authkey", self.auth_key)
            req.add_header("Content-Type", "application/json")

            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get("type") == "success":
                    logger.info(f"MSG91 OTP dispatched to {mask_phone_number(clean_mobile)}")
                    return True, "OTP sent successfully to your registered mobile number."
                else:
                    logger.error(f"MSG91 error: {data.get('message')}")
                    return False, "Failed to deliver SMS. Please try again shortly."
        except Exception as exc:
            logger.error(f"MSG91 dispatch exception: {type(exc).__name__}: {str(exc)}")
            return False, "SMS delivery failed due to network error. Please try again or use password login."


class UnconfiguredSMSProvider(BaseSMSProvider):
    """
    Fallback provider when no valid production SMS gateway is configured.
    Never pretends SMS was delivered. Explicitly returns error.
    """

    def send_otp(self, mobile: str, otp: str) -> tuple[bool, str]:
        logger.warning(
            "SMS dispatch attempted but no valid SMS provider is configured in environment. "
            "Refusing to pretend OTP was delivered."
        )
        return (
            False,
            "SMS service is not currently configured for this platform. "
            "Please use your merchant username and password to log in, or contact platform support."
        )


def get_sms_provider() -> BaseSMSProvider:
    """
    Factory resolving the active SMS provider according to environment settings.
    Guarantees development mock/console providers cannot run in production (DEBUG=False).
    """
    is_test_runner = getattr(settings, "TESTING", False) or "test" in sys.argv
    force_prod_check = getattr(settings, "FORCE_PROD_SMS_CHECK", False)
    is_production = not settings.DEBUG and (not is_test_runner or force_prod_check)

    provider_name = getattr(settings, "SMS_PROVIDER", "") or os.getenv("SMS_PROVIDER", "").strip().lower()

    # When running under automated tests, default to MockSMSProvider unless explicitly testing production safety
    if is_test_runner and not force_prod_check and provider_name in ("mock", "test", "console", ""):
        return MockSMSProvider()

    # 1. Check for development / mock providers
    if provider_name in ("mock", "test"):
        if is_production:
            raise ImproperlyConfigured(
                "Mock SMS provider cannot be enabled when DEBUG=False in production. "
                "Configure a valid production SMS provider (e.g. twilio, msg91, fast2sms)."
            )
        return MockSMSProvider()

    if provider_name == "console":
        if is_production:
            raise ImproperlyConfigured(
                "Console SMS provider cannot be enabled when DEBUG=False in production. "
                "Configure a valid production SMS provider (e.g. twilio, msg91, fast2sms)."
            )
        return ConsoleSMSProvider()

    # 2. Check for supported production providers
    if provider_name == "twilio":
        return TwilioSMSProvider()
    elif provider_name == "fast2sms":
        return Fast2SMSProvider()
    elif provider_name == "msg91":
        return MSG91SMSProvider()

    # 3. Default fallback logic
    if is_production:
        return UnconfiguredSMSProvider()

    # In local development only (DEBUG=True)
    dev_mode = os.getenv("SELLER_OTP_DEV_MODE", "True").strip().lower() in ("true", "1", "yes")
    if dev_mode:
        return ConsoleSMSProvider()

    return UnconfiguredSMSProvider()

