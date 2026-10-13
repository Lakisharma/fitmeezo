"""
Custom Authentication Backend for Fitmeezo.
Allows customer login via email address or username interchangeably,
while preserving full compatibility with Django Admin and existing users.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class EmailOrUsernameModelBackend(ModelBackend):
    """
    Authenticates against settings.AUTH_USER_MODEL using either:
    1. Email address (case-insensitive)
    2. Username (case-insensitive)
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD) or kwargs.get("email")

        if not username or not password:
            return None

        username_clean = str(username).strip()

        try:
            # Query by email or username (case-insensitive)
            user = UserModel.objects.filter(
                Q(email__iexact=username_clean) | Q(username__iexact=username_clean)
            ).first()
        except UserModel.DoesNotExist:
            return None

        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user

        return None
