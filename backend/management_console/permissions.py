"""
Authentication and Role-Based Permissions for Management Console.
Enforces strict server-side staff/superuser checks, model-level add/change/delete/view permissions,
and protects sensitive operations.
"""

from functools import wraps
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse


def console_auth_required(view_func):
    """
    Decorator for views in the management console.
    Requires that the user is authenticated, active, and has staff or superuser status.
    Unauthenticated users are redirected to the management console login page.
    Unauthorized users receive a 403 Forbidden page.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            login_url = reverse("management_console:login")
            return redirect(f"{login_url}?{REDIRECT_FIELD_NAME}={request.get_full_path()}")

        if not request.user.is_active or not (request.user.is_staff or request.user.is_superuser):
            return render(
                request,
                "management_console/403.html",
                {
                    "error_title": "Staff Authorization Required",
                    "error_message": "Access to the Fitmeezo Management Console is restricted to active staff and administrative accounts.",
                    "user": request.user,
                },
                status=403,
            )

        return view_func(request, *args, **kwargs)

    return _wrapped_view


def check_model_permission(request, model_admin, action, obj=None):
    """
    Checks if request.user has permission for a specific action on a model or object.
    action can be: 'view', 'add', 'change', 'delete'.
    Delegates to the model_admin's own permission methods.
    """
    if not request.user.is_authenticated or not request.user.is_active:
        return False

    if request.user.is_superuser:
        return True

    if not (request.user.is_staff or request.user.is_superuser):
        return False

    if action == "view":
        if hasattr(model_admin, "has_view_permission"):
            return model_admin.has_view_permission(request, obj)
        return model_admin.has_change_permission(request, obj)

    elif action == "add":
        if hasattr(model_admin, "has_add_permission"):
            return model_admin.has_add_permission(request)
        return False

    elif action == "change":
        if hasattr(model_admin, "has_change_permission"):
            return model_admin.has_change_permission(request, obj)
        return False

    elif action == "delete":
        if hasattr(model_admin, "has_delete_permission"):
            return model_admin.has_delete_permission(request, obj)
        return False

    return False
