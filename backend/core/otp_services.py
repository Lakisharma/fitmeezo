import datetime
import logging
import re
import secrets
from django.conf import settings
from django.utils import timezone
from core.models import Seller, SellerLoginOTP
from core.sms_services import get_sms_provider, mask_phone_number

logger = logging.getLogger(__name__)

OTP_VALIDITY_MINUTES = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_HOURLY_REQUESTS_PER_MOBILE = 5
MAX_HOURLY_REQUESTS_PER_IP = 20


def normalize_mobile_number(raw_mobile: str) -> str | None:
    """
    Normalizes an Indian mobile number to a clean 10-digit format starting with 6, 7, 8, or 9.
    Returns None if the format is invalid.
    """
    if not raw_mobile:
        return None
    cleaned = re.sub(r"[^\d]", "", str(raw_mobile).strip())
    if len(cleaned) >= 10:
        cleaned = cleaned[-10:]
    if len(cleaned) == 10 and cleaned[0] in "6789":
        return cleaned
    return None


def get_client_ip(request) -> str:
    """Extracts client IP address from standard and forwarded proxy headers."""
    if not request:
        return "127.0.0.1"
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
        return ip
    return request.META.get("REMOTE_ADDR", "127.0.0.1")


def get_client_user_agent(request) -> str:
    """Extracts client User-Agent header truncated to 255 chars."""
    if not request:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")[:255]


def check_otp_resend_cooldown(mobile: str) -> tuple[bool, int]:
    """
    Checks if a resend cooldown is active for this mobile number.
    Returns:
        tuple[bool, int]: (can_resend, remaining_seconds)
    """
    recent_otp = SellerLoginOTP.objects.filter(mobile=mobile).order_by("-created_at").first()
    if not recent_otp:
        return True, 0

    elapsed = (timezone.now() - recent_otp.created_at).total_seconds()
    if elapsed < RESEND_COOLDOWN_SECONDS:
        remaining = int(RESEND_COOLDOWN_SECONDS - elapsed)
        return False, remaining
    return True, 0


def check_otp_rate_limit(mobile: str, ip_address: str = "") -> tuple[bool, str]:
    """
    Enforces server-side hourly rate limits:
    - Max 5 OTP requests per mobile number per hour.
    - Max 20 OTP requests per IP address per hour.
    Returns:
        tuple[bool, str]: (is_allowed, error_message)
    """
    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)

    mobile_count = SellerLoginOTP.objects.filter(
        mobile=mobile,
        created_at__gte=one_hour_ago,
    ).count()
    if mobile_count >= MAX_HOURLY_REQUESTS_PER_MOBILE:
        logger.warning(f"Rate limit exceeded for mobile {mask_phone_number(mobile)} ({mobile_count} requests in 1hr)")
        return False, "Too many OTP requests for this mobile number. Please wait an hour or log in with your password."

    if ip_address:
        ip_count = SellerLoginOTP.objects.filter(
            ip_address=ip_address,
            created_at__gte=one_hour_ago,
        ).count()
        if ip_count >= MAX_HOURLY_REQUESTS_PER_IP:
            logger.warning(f"Rate limit exceeded for IP {ip_address} ({ip_count} requests in 1hr)")
            return False, "Too many verification requests from your network. Please try again later."

    return True, ""


def generate_and_send_seller_otp(
    mobile: str,
    ip_address: str = "",
    user_agent: str = "",
) -> tuple[bool, str, int]:
    """
    Validates the seller account, enforces throttling and rate limits,
    generates a secure 6-digit OTP, stores its PBKDF2 hash, and dispatches via configured SMS provider.
    Returns:
        tuple[bool, str, int]: (success, message, cooldown_seconds)
    """
    clean_mobile = normalize_mobile_number(mobile)
    if not clean_mobile:
        return False, "Please enter a valid 10-digit Indian mobile number (e.g. 9876543210).", 0

    # 1. Look up existing Seller account uniquely mapped to this mobile number
    seller = Seller.objects.filter(registered_mobile=clean_mobile).select_related("user").first()
    if not seller:
        # Check if seller exists with support_phone matching this mobile
        seller = Seller.objects.filter(support_phone__endswith=clean_mobile).select_related("user").first()
        if seller and not seller.registered_mobile:
            # Safely associate registered_mobile
            seller.registered_mobile = clean_mobile
            seller.save(update_fields=["registered_mobile", "updated_at"])

    if not seller:
        logger.info(f"OTP login attempted for unregistered mobile: {mask_phone_number(clean_mobile)}")
        return False, "No seller account found with this registered mobile number. Please check the number or register your store.", 0

    # 2. Strict Account & Authorization Validation
    user = seller.user
    if not user.is_active:
        return False, "Your account is inactive. Please contact platform administration.", 0

    if user.is_staff or user.is_superuser:
        return False, "Staff and platform administrators cannot log in through the vendor portal.", 0

    if seller.status == Seller.STATUS_PENDING:
        return False, f"Your seller application for '{seller.store_name}' is currently pending administrator review.", 0
    elif seller.status == Seller.STATUS_REJECTED:
        return False, f"Your seller application for '{seller.store_name}' was not approved. Please contact platform support.", 0
    elif seller.status == Seller.STATUS_SUSPENDED:
        return False, f"Your seller account '{seller.store_name}' is currently suspended. Please contact platform administration.", 0
    elif not seller.is_active:
        return False, f"Your seller account '{seller.store_name}' is inactive. Please contact platform support.", 0

    # 3. Enforce 60-second resend cooldown
    can_resend, remaining_cd = check_otp_resend_cooldown(clean_mobile)
    if not can_resend:
        return False, f"Please wait {remaining_cd} seconds before requesting a new OTP.", remaining_cd

    # 4. Enforce hourly rate limits
    allowed, rate_msg = check_otp_rate_limit(clean_mobile, ip_address=ip_address)
    if not allowed:
        return False, rate_msg, 0

    # 5. Invalidate any previous unused OTPs for this mobile
    SellerLoginOTP.objects.filter(mobile=clean_mobile, is_used=False).update(is_used=True)

    # 6. Generate cryptographically strong 6-digit OTP
    raw_otp = "".join(secrets.choice("0123456789") for _ in range(6))
    now = timezone.now()
    expires_at = now + datetime.timedelta(minutes=OTP_VALIDITY_MINUTES)

    # 7. Create OTP record with PBKDF2 hash (NEVER plaintext)
    otp_record = SellerLoginOTP(
        seller=seller,
        mobile=clean_mobile,
        expires_at=expires_at,
        ip_address=ip_address or None,
        user_agent=user_agent or "",
    )
    otp_record.set_otp(raw_otp)
    otp_record.save()

    # 8. Dispatch via configured SMS provider
    provider = get_sms_provider()
    sent_ok, send_msg = provider.send_otp(clean_mobile, raw_otp)

    if not sent_ok:
        # Invalidate OTP record if dispatch failed
        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])
        return False, send_msg, 0

    logger.info(f"Dispatched login OTP to seller '{seller.store_name}' ({mask_phone_number(clean_mobile)})")
    return True, send_msg, RESEND_COOLDOWN_SECONDS


def verify_seller_otp_code(
    mobile: str,
    raw_otp: str,
) -> tuple[bool, str, Seller | None]:
    """
    Verifies the submitted OTP against the latest active record for the given mobile.
    Returns:
        tuple[bool, str, Seller | None]: (success, message, seller_instance)
    """
    clean_mobile = normalize_mobile_number(mobile)
    if not clean_mobile:
        return False, "Invalid mobile number format.", None

    clean_otp = re.sub(r"[^\d]", "", str(raw_otp).strip())
    if len(clean_otp) != 6:
        return False, "Please enter a valid 6-digit verification code.", None

    otp_record = (
        SellerLoginOTP.objects.filter(
            mobile=clean_mobile,
            is_used=False,
        )
        .select_related("seller", "seller__user")
        .order_by("-created_at")
        .first()
    )

    if not otp_record:
        used_otp = (
            SellerLoginOTP.objects.filter(
                mobile=clean_mobile,
                is_used=True,
            )
            .order_by("-created_at")
            .first()
        )
        if used_otp and (timezone.now() - used_otp.created_at).total_seconds() < 300:
            return False, "This OTP has already been used. Please request a new OTP.", None
        return False, "No active OTP found for this mobile number. Please request a new OTP.", None

    # Call record-level verification (enforces expiry, attempts count, and hash check)
    is_valid, msg = otp_record.verify_otp(clean_otp)
    if not is_valid:
        return False, msg, None

    seller = otp_record.seller
    # Re-verify that the seller remains approved and active
    if (
        seller.status != Seller.STATUS_APPROVED
        or not seller.is_active
        or not seller.user.is_active
        or seller.user.is_staff
        or seller.user.is_superuser
    ):
        return False, "This account is not authorized to access the Seller Hub.", None

    return True, "Verification successful.", seller
