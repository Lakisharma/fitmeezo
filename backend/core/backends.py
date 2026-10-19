"""
Custom Authentication Backend for Fitmeezo.
Allows customer login via registered mobile number or email address interchangeably,
while preserving full compatibility with Django Admin, usernames, and existing users.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from .otp_services import normalize_mobile_number


class EmailOrUsernameModelBackend(ModelBackend):
    """
    Authenticates against settings.AUTH_USER_MODEL using either:
    1. Registered mobile number (10-digit Indian mobile, with or without +91 / 91 / 0 prefix)
    2. Registered email address (case-insensitive)
    3. Username (case-insensitive fallback for admin and existing accounts)
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            username = (
                kwargs.get("mobile_or_email")
                or kwargs.get("username_or_email")
                or kwargs.get(UserModel.USERNAME_FIELD)
                or kwargs.get("email")
                or kwargs.get("mobile")
            )

        if not username or not password:
            return None

        identifier = str(username).strip()
        user = None

        # 1. If identifier contains '@', authenticate via email address
        if "@" in identifier:
            clean_email = identifier.lower()
            try:
                user = UserModel.objects.filter(
                    Q(email__iexact=clean_email) | Q(username__iexact=clean_email)
                ).first()
            except UserModel.DoesNotExist:
                user = None

        # 2. Check if identifier is a mobile number (Indian mobile with/without country code)
        if not user:
            clean_mobile = normalize_mobile_number(identifier)
            if clean_mobile:
                try:
                    user = UserModel.objects.filter(
                        Q(profile__mobile_number=clean_mobile)
                        | Q(profile__phone_number=clean_mobile)
                        | Q(profile__phone_number__endswith=clean_mobile)
                        | Q(seller_profile__registered_mobile=clean_mobile)
                        | Q(seller_profile__support_phone__endswith=clean_mobile)
                    ).select_related("profile").first()
                except UserModel.DoesNotExist:
                    user = None

        # 3. Fallback for username (e.g. Django admin, staff, or legacy usernames)
        if not user:
            try:
                user = UserModel.objects.filter(username__iexact=identifier).first()
            except UserModel.DoesNotExist:
                user = None

        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user

        return None


# Backward-compatibility alias
EmailOrMobileModelBackend = EmailOrUsernameModelBackend
