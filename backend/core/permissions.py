"""
Fitmeezo Access Control & Permission Layer.
Provides server-side decorators, class mixins, and DRF permissions for:
- Seller Authorization (Approved Sellers only)
- Platform Administrator Isolation (Preventing sellers from obtaining staff privileges)
"""

from functools import wraps
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from rest_framework import permissions

from .models import Seller


def is_approved_seller(user) -> bool:
    """
    Returns True if the user is authenticated, has a linked Seller profile,
    the profile status is APPROVED, is_active is True, and has a valid GSTIN.
    """
    if not (user and user.is_authenticated):
        return False
    seller = getattr(user, "seller_profile", None)
    if not seller:
        return False
    return (
        seller.status == Seller.STATUS_APPROVED
        and seller.is_active is True
        and seller.has_valid_gstin
    )


def seller_required(view_func):
    """
    View decorator that enforces:
    1. User is authenticated.
    2. User has an associated Seller profile.
    3. Seller profile status is APPROVED and is_active is True.
    4. Seller has a valid submitted GSTIN.

    If the seller is pending, rejected, suspended, or missing valid GSTIN,
    redirects to the seller status page with an informative flash message.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url="core:account_login")

        seller = getattr(request.user, "seller_profile", None)
        if not seller:
            messages.warning(
                request,
                "You do not have a registered seller account. Please register to become a seller."
            )
            return redirect("core:seller_register")

        if seller.status == Seller.STATUS_PENDING:
            messages.info(
                request,
                f"Your seller application for '{seller.store_name}' is currently pending administrator review."
            )
            return redirect("core:seller_status")

        if seller.status == Seller.STATUS_REJECTED:
            messages.error(
                request,
                f"Your seller application for '{seller.store_name}' was not approved. Please contact platform support."
            )
            return redirect("core:seller_status")

        if seller.status == Seller.STATUS_SUSPENDED:
            messages.error(
                request,
                f"Your seller account '{seller.store_name}' is suspended. Please contact platform administration."
            )
            return redirect("core:seller_status")

        if not seller.is_active:
            messages.warning(
                request,
                f"Your seller account '{seller.store_name}' is currently deactivated."
            )
            return redirect("core:seller_status")

        if not seller.has_valid_gstin:
            messages.error(
                request,
                f"A valid 15-character GSTIN is required to access seller features for '{seller.store_name}'."
            )
            return redirect("core:seller_status")

        if seller.status != Seller.STATUS_APPROVED:
            messages.error(request, "Access restricted to approved sellers only.")
            return redirect("core:seller_status")

        return view_func(request, *args, **kwargs)

    return _wrapped_view


class SellerRequiredMixin:
    """Class-Based View mixin enforcing approved seller status."""

    def dispatch(self, request, *args, **kwargs):
        decorator = seller_required(super().dispatch)
        return decorator(request, *args, **kwargs)


class IsApprovedSeller(permissions.BasePermission):
    """
    DRF Permission class for seller API endpoints.
    Allows access only to authenticated users with an APPROVED, active Seller profile.
    """
    message = "Access restricted to approved, active seller accounts."

    def has_permission(self, request, view):
        return is_approved_seller(request.user)
