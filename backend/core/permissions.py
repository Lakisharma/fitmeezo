"""
Fitmeezo Access Control & Permission Layer.
Provides server-side decorators, class mixins, and DRF permissions for:
- Seller Authorization (Approved Sellers only)
- Platform Administrator Isolation (Preventing sellers from obtaining staff privileges)
"""

from functools import wraps
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden, JsonResponse
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


def is_seller_closed(user) -> bool:
    """
    Returns True if the user has a linked Seller profile in STATUS_CLOSED.
    """
    if not (user and user.is_authenticated):
        return False
    seller = getattr(user, "seller_profile", None)
    return bool(seller and seller.status == Seller.STATUS_CLOSED)


def seller_required(view_func):
    """
    View decorator that enforces:
    1. User is authenticated.
    2. User has an associated Seller profile.
    3. Seller profile status is in APPROVED, CLOSURE_REQUESTED, CLOSING, or CLOSED.
    4. Seller has a valid submitted GSTIN.

    Phase 4 Enforcement for Closed Sellers (STATUS_CLOSED):
    - Retains read-only historical access for safe HTTP methods (GET, HEAD, OPTIONS).
    - Blocks all mutating HTTP methods (POST, PUT, PATCH, DELETE) with HTTP 403 Forbidden.
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

        allowed_portal_statuses = (
            Seller.STATUS_APPROVED,
            Seller.STATUS_CLOSURE_REQUESTED,
            Seller.STATUS_CLOSING,
            Seller.STATUS_CLOSED,
        )

        if not seller.is_active and seller.status not in (
            Seller.STATUS_CLOSURE_REQUESTED,
            Seller.STATUS_CLOSING,
            Seller.STATUS_CLOSED,
        ):
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

        if seller.status not in allowed_portal_statuses:
            messages.error(request, "Access restricted to approved or operational run-off sellers only.")
            return redirect("core:seller_status")

        # Phase 4: Enforce read-only access for closed sellers across all seller_required views
        if seller.status == Seller.STATUS_CLOSED:
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.content_type == "application/json":
                    return JsonResponse(
                        {"error": "This merchant account is permanently closed. Account is restricted to read-only mode."},
                        status=403,
                    )
                return HttpResponseForbidden(
                    "This merchant account is permanently closed. Account is restricted to read-only mode."
                )

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


class IsSellerReadOnlyOrApproved(permissions.BasePermission):
    """
    DRF Permission class allowing read-only access to closed sellers,
    while restricting mutations to active approved sellers.
    """
    message = "Mutations restricted to active approved sellers. Closed accounts have read-only access."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        seller = getattr(request.user, "seller_profile", None)
        if not seller:
            return False
        if request.method in permissions.SAFE_METHODS:
            return seller.status in (
                Seller.STATUS_APPROVED,
                Seller.STATUS_CLOSURE_REQUESTED,
                Seller.STATUS_CLOSING,
                Seller.STATUS_CLOSED,
            )
        return is_approved_seller(request.user)
