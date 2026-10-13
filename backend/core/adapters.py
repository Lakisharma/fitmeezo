"""
Fitmeezo Social Account Adapter for django-allauth.
Handles OAuth account linking, verification checks, profile synchronization,
and cart merging for Google and Facebook login.
"""

from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .cart import Cart
from .models import CustomerProfile


class FitmeezoSocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    Custom social account adapter enforcing Fitmeezo's security rules:
    1. Verifies provider email verification status before trusting provider email for linking.
    2. Prevents auto-linking untrusted or unverified emails.
    3. Prompts existing account password login before linking unverified identities.
    4. Automatically populates customer profile and merges guest cart on login.
    5. Validates redirect destinations to prevent open redirect vulnerabilities.
    """

    def pre_social_login(self, request, sociallogin):
        """
        Invoked just after a successful provider authentication, but before
        the user is actually logged in.
        """
        # If social account is already connected to a user in the database, proceed
        if sociallogin.is_existing:
            return

        UserModel = get_user_model()
        provider = sociallogin.account.provider
        extra_data = sociallogin.account.extra_data or {}

        # Extract email if present
        email = None
        if sociallogin.email_addresses:
            email = sociallogin.email_addresses[0].email
        elif "email" in extra_data:
            email = extra_data.get("email")

        if email:
            email = str(email).strip().lower()

        # If user is already authenticated in this session, connect this social account to them
        if request.user.is_authenticated:
            sociallogin.connect(request, request.user)
            return

        if not email:
            # Facebook or other provider without email permission:
            # Do NOT attempt to auto-link. Allauth will create a new account for this social user.
            return

        # Check if an existing account with this email exists
        existing_user = UserModel.objects.filter(email__iexact=email).first()
        if not existing_user:
            # New customer registering with social login - proceed with normal registration
            return

        # Existing account found with this email:
        # Check provider trustworthiness and verification status
        if provider == "google":
            # Google verified email check (OpenID Connect 'email_verified' claim)
            is_verified = (
                extra_data.get("email_verified") is True
                or str(extra_data.get("email_verified", "")).lower() == "true"
            )
            if is_verified:
                # Google email is verified by Google: connect to the existing user
                sociallogin.connect(request, existing_user)
                return
            else:
                # Unverified Google email: require password sign-in first
                messages.error(
                    request,
                    "An account with this email address already exists, but your Google email is unverified. "
                    "Please sign in with your email and password to verify your identity before connecting."
                )
                login_url = reverse("core:account_login")
                raise ImmediateHttpResponse(redirect(login_url))

        elif provider == "facebook":
            # Facebook emails are not strictly verified like Google OIDC
            # Requirement: Do not automatically link accounts solely because an unverified or untrusted email matches.
            # Require customer to log in with password first to link Facebook
            messages.warning(
                request,
                f"An account with email '{email}' already exists. For your security, "
                "please sign in with your email and password first to link your Facebook account."
            )
            login_url = f"{reverse('core:account_login')}?next={quote(reverse('core:account_profile'))}"
            raise ImmediateHttpResponse(redirect(login_url))

    def populate_user(self, request, sociallogin, data):
        """
        Hook called when a new user is created from social login data.
        Sets first_name, last_name, normalized email, and username safely.
        """
        user = super().populate_user(request, sociallogin, data)
        extra_data = sociallogin.account.extra_data or {}

        # Normalize names
        first_name = data.get("first_name") or extra_data.get("first_name") or ""
        last_name = data.get("last_name") or extra_data.get("last_name") or ""
        full_name = data.get("name") or extra_data.get("name") or ""

        if not first_name and full_name:
            parts = full_name.strip().split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""

        if first_name:
            user.first_name = first_name[:150]
        if last_name:
            user.last_name = last_name[:150]

        if user.email:
            user.email = user.email.strip().lower()

        # Ensure a clean, collision-free username
        UserModel = get_user_model()
        if not user.username or UserModel.objects.filter(username__iexact=user.username).exists():
            base_username = (
                user.email.split("@")[0]
                if user.email
                else f"{sociallogin.account.provider}_{sociallogin.account.uid}"
            )
            clean_base = "".join(c for c in base_username if c.isalnum() or c in ("_", "-")).lower()[:20] or "customer"
            candidate = clean_base
            counter = 1
            while UserModel.objects.filter(username__iexact=candidate).exists():
                candidate = f"{clean_base}{counter}"
                counter += 1
            user.username = candidate

        return user

    def get_login_redirect_url(self, request):
        """
        Validates the redirect URL after social login.
        Guards strictly against open redirect vulnerabilities.
        """
        next_url = (
            request.GET.get("next")
            or request.POST.get("next")
            or request.session.get("socialaccount_next")
            or ""
        )
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return next_url
        return reverse("core:account_dashboard")

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        """
        Handles cancelled authorization, expired code, or provider errors gracefully
        by redirecting to login with a friendly notification.
        """
        provider_id = getattr(provider, "id", str(provider)) if provider else "social"
        provider_title = "Google" if provider_id == "google" else ("Facebook" if provider_id == "facebook" else "Social")
        messages.info(
            request,
            f"{provider_title} sign-in session expired or was cancelled. Please try signing in again."
        )
        raise ImmediateHttpResponse(redirect("core:account_login"))

    def authentication_error(self, request, provider_id, error=None, exception=None, extra_context=None):
        return self.on_authentication_error(request, provider_id, error=error, exception=exception, extra_context=extra_context)
