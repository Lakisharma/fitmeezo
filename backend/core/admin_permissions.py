"""
Fitmeezo Custom Admin - Role-Based Access Control (RBAC) & Audit Logging.
Defines roles, permissions, decorators, and audit logging utilities.
"""

from functools import wraps
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse

User = get_user_model()

# ==============================================================================
# ROLE CONSTANTS
# ==============================================================================
ROLE_SUPER_ADMIN = "Super Admin"
ROLE_PRODUCT_MANAGER = "Product Manager"
ROLE_SELLER_MANAGER = "Seller Manager"
ROLE_FINANCE_MANAGER = "Finance Manager"
ROLE_SUPPORT_MANAGER = "Support Manager"

ALL_ROLES = [
    ROLE_SUPER_ADMIN,
    ROLE_PRODUCT_MANAGER,
    ROLE_SELLER_MANAGER,
    ROLE_FINANCE_MANAGER,
    ROLE_SUPPORT_MANAGER,
]

# Role hierarchy and permissions mapping
ROLE_PERMISSIONS = {
    ROLE_SUPER_ADMIN: {
        "can_manage_products": True,
        "can_manage_sellers": True,
        "can_manage_finance": True,
        "can_manage_support": True,
        "can_manage_users": True,
        "can_manage_audit": True,
        "can_manage_settings": True,
        "can_export_reports": True,
    },
    ROLE_PRODUCT_MANAGER: {
        "can_manage_products": True,
        "can_manage_sellers": False,
        "can_manage_finance": False,
        "can_manage_support": False,
        "can_manage_users": False,
        "can_manage_audit": False,
        "can_manage_settings": False,
        "can_export_reports": True,
    },
    ROLE_SELLER_MANAGER: {
        "can_manage_products": False,
        "can_manage_sellers": True,
        "can_manage_finance": False,
        "can_manage_support": False,
        "can_manage_users": False,
        "can_manage_audit": False,
        "can_manage_settings": False,
        "can_export_reports": True,
    },
    ROLE_FINANCE_MANAGER: {
        "can_manage_products": False,
        "can_manage_sellers": False,
        "can_manage_finance": True,
        "can_manage_support": False,
        "can_manage_users": False,
        "can_manage_audit": False,
        "can_manage_settings": False,
        "can_export_reports": True,
    },
    ROLE_SUPPORT_MANAGER: {
        "can_manage_products": False,
        "can_manage_sellers": False,
        "can_manage_finance": False,
        "can_manage_support": True,
        "can_manage_users": False,
        "can_manage_audit": False,
        "can_manage_settings": False,
        "can_export_reports": False,
    },
}


def ensure_admin_groups():
    """
    Idempotently ensures all 5 standard Admin Groups exist in the database.
    """
    for role_name in ALL_ROLES:
        Group.objects.get_or_create(name=role_name)


def get_user_role(user):
    """
    Determines the primary administrative role for a user.
    Superusers automatically have 'Super Admin' role.
    """
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return ROLE_SUPER_ADMIN

    # Check assigned groups in priority order
    user_groups = set(user.groups.values_list("name", flat=True))
    for role in ALL_ROLES:
        if role in user_groups:
            return role

    # Fallback for staff without specific group assigned (default admin privilege)
    if user.is_staff:
        return ROLE_SUPER_ADMIN

    return None


def set_user_role(user, role_name):
    """
    Assigns an administrative role to a staff user, removing conflicting admin groups.
    """
    if role_name not in ALL_ROLES:
        raise ValueError(f"Invalid role: {role_name}. Must be one of {ALL_ROLES}")

    ensure_admin_groups()
    admin_groups = Group.objects.filter(name__in=ALL_ROLES)
    user.groups.remove(*admin_groups)

    target_group = Group.objects.get(name=role_name)
    user.groups.add(target_group)

    # Ensure user has is_staff enabled so they can log into the admin portal
    if not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])


def has_admin_permission(user, allowed_roles=None):
    """
    Validates whether an authenticated user is active, staff/superuser,
    and belongs to one of the authorized roles.
    """
    if not user or not user.is_authenticated or not user.is_active:
        return False

    if not (user.is_staff or user.is_superuser):
        return False

    if user.is_superuser:
        return True

    if not allowed_roles:
        return True

    user_role = get_user_role(user)
    if user_role == ROLE_SUPER_ADMIN:
        return True

    if isinstance(allowed_roles, str):
        allowed_roles = [allowed_roles]

    return user_role in allowed_roles


def admin_role_required(allowed_roles=None):
    """
    Decorator for custom-admin views requiring active staff status and role authorization.
    Unauthenticated users are redirected to custom_admin:login.
    Unauthorized users receive a 403 Forbidden response rendered with custom_admin/403.html.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                login_url = reverse("custom_admin:login")
                return redirect(f"{login_url}?next={request.get_full_path()}")

            if not request.user.is_active or not (request.user.is_staff or request.user.is_superuser):
                return render(
                    request,
                    "custom_admin/403.html",
                    {
                        "required_roles": allowed_roles or ["Staff"],
                        "user_role": "None",
                        "error_message": "You must possess an active Staff or Administrator account to access the administration portal.",
                    },
                    status=403,
                )

            if allowed_roles and not has_admin_permission(request.user, allowed_roles):
                user_role = get_user_role(request.user)
                return render(
                    request,
                    "custom_admin/403.html",
                    {
                        "required_roles": allowed_roles if isinstance(allowed_roles, list) else [allowed_roles],
                        "user_role": user_role,
                        "error_message": f"Your role ({user_role}) does not have permission to access this management section.",
                    },
                    status=403,
                )

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator


def log_admin_action(user, obj, action_flag, change_message):
    """
    Records an administrative action to Django's standard LogEntry audit table.
    """
    try:
        content_type = ContentType.objects.get_for_model(obj)
        object_id = str(obj.pk)
        object_repr = str(obj)[:200]
        LogEntry.objects.create(
            user=user,
            content_type=content_type,
            object_id=object_id,
            object_repr=object_repr,
            action_flag=action_flag,
            change_message=str(change_message)[:500],
        )
    except Exception:
        # Failsafe: logging failure should never crash the primary transaction
        pass
