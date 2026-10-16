"""
Model Registry Bridge for Management Console.
Introspects Django's admin.site._registry to provide 100% fidelity with registered models,
fields, list_displays, search_fields, list_filters, and permission constraints,
while presenting them through the Fitmeezo Enterprise Console design.
"""

from django.apps import apps
from django.contrib import admin
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.utils.html import escape, format_html, strip_tags
from django.utils.safestring import SafeString, mark_safe

# Logical module groupings for the enterprise sidebar navigation
MODULE_GROUPS = [
    {
        "id": "catalog",
        "title": "Product Catalog",
        "icon": "bi-box-seam",
        "models": [
            ("core", "product"),
            ("core", "category"),
            ("core", "productvariant"),
            ("core", "productreview"),
        ],
        "shortcuts": [
            {"title": "Add Product", "url_name": "management_console:product_add", "icon": "bi-plus-circle"},
            {"title": "Inventory Monitor", "url_name": "management_console:inventory_monitor", "icon": "bi-speedometer2"},
        ]
    },
    {
        "id": "orders",
        "title": "Orders & Logistics",
        "icon": "bi-bag-check",
        "models": [
            ("core", "order"),
            ("core", "sellerorder"),
            ("core", "shipmentbooking"),
            ("core", "codreceiptrecord"),
        ]
    },
    {
        "id": "sellers",
        "title": "Sellers & Operations",
        "icon": "bi-shop",
        "models": [
            ("core", "seller"),
            ("core", "sellerloginotp"),
        ],
        "shortcuts": [
            {"title": "Seller Approvals", "url_name": "management_console:seller_approvals", "icon": "bi-person-check"},
        ]
    },
    {
        "id": "returns",
        "title": "Returns & Refunds",
        "icon": "bi-arrow-left-right",
        "models": [
            ("core", "returnrequest"),
            ("core", "refundrecord"),
            ("core", "returnauditlog"),
        ]
    },
    {
        "id": "financials",
        "title": "Finance & Ledger",
        "icon": "bi-currency-rupee",
        "models": [
            ("core", "invoice"),
            ("core", "invoicesequence"),
            ("core", "sellerledgerentry"),
            ("core", "payoutbatch"),
            ("core", "payoutauditlog"),
        ]
    },
    {
        "id": "customers",
        "title": "Customers & CRM",
        "icon": "bi-people",
        "models": [
            ("core", "customerprofile"),
            ("core", "address"),
            ("core", "cart"),
            ("core", "wishlistitem"),
        ]
    },
    {
        "id": "access",
        "title": "Access & Identity",
        "icon": "bi-shield-lock",
        "models": [
            ("auth", "user"),
            ("auth", "group"),
            ("account", "emailaddress"),
            ("authtoken", "tokenproxy"),
            ("socialaccount", "socialapp"),
            ("socialaccount", "socialtoken"),
            ("socialaccount", "socialaccount"),
        ]
    },
    {
        "id": "system",
        "title": "System & Config",
        "icon": "bi-gear",
        "models": [
            ("sites", "site"),
        ],
        "shortcuts": [
            {"title": "Audit Logs", "url_name": "management_console:audit_logs", "icon": "bi-clock-history"},
        ]
    },
]


def get_registered_model_admin(app_label, model_name):
    """
    Retrieves the registered Model class and its ModelAdmin instance from Django admin registry.
    Returns (model_class, model_admin) or (None, None).
    """
    try:
        model = apps.get_model(app_label, model_name)
    except (LookupError, ValueError):
        return None, None

    if model in admin.site._registry:
        return model, admin.site._registry[model]

    # Also handle proxy models or case differences
    for reg_model, model_admin in admin.site._registry.items():
        if (reg_model._meta.app_label.lower() == app_label.lower() and
                reg_model._meta.model_name.lower() == model_name.lower()):
            return reg_model, model_admin

    return None, None


def get_all_registered_models(request=None):
    """
    Returns list of all models registered in admin.site._registry with metadata,
    filtered by user view permissions if request is provided.
    """
    result = []
    for model, model_admin in admin.site._registry.items():
        if request and hasattr(model_admin, "has_view_permission"):
            try:
                if not model_admin.has_view_permission(request):
                    continue
            except Exception:
                pass

        result.append({
            "app_label": model._meta.app_label,
            "model_name": model._meta.model_name,
            "verbose_name": model._meta.verbose_name,
            "verbose_name_plural": model._meta.verbose_name_plural,
            "model": model,
            "admin": model_admin,
        })
    result.sort(key=lambda x: (x["app_label"], x["verbose_name_plural"].lower()))
    return result


def get_navigation_modules(request=None):
    """
    Constructs the structured navigation menu for the management console sidebar.
    Only includes models and shortcuts the user has permission to see.
    """
    nav = []
    registered_keys = {
        (m._meta.app_label.lower(), m._meta.model_name.lower()): (m, adm)
        for m, adm in admin.site._registry.items()
    }

    handled_models = set()

    for group in MODULE_GROUPS:
        group_items = []
        for app_label, model_name in group["models"]:
            key = (app_label.lower(), model_name.lower())
            if key in registered_keys:
                model, model_admin = registered_keys[key]
                handled_models.add(key)

                # Permission check
                if request and hasattr(model_admin, "has_view_permission"):
                    try:
                        if not model_admin.has_view_permission(request):
                            continue
                    except Exception:
                        pass

                group_items.append({
                    "title": model._meta.verbose_name_plural.title(),
                    "app_label": model._meta.app_label,
                    "model_name": model._meta.model_name,
                    "url_name": "management_console:model_list",
                    "count_query": lambda m=model: m.objects.count() if hasattr(m, 'objects') else None,
                })

        shortcuts = group.get("shortcuts", [])
        if group_items or shortcuts:
            nav.append({
                "id": group["id"],
                "title": group["title"],
                "icon": group["icon"],
                "items": group_items,
                "shortcuts": shortcuts,
            })

    # Catch any remaining registered models not explicitly in MODULE_GROUPS
    other_items = []
    for key, (model, model_admin) in registered_keys.items():
        if key not in handled_models:
            if request and hasattr(model_admin, "has_view_permission"):
                try:
                    if not model_admin.has_view_permission(request):
                        continue
                except Exception:
                    pass
            other_items.append({
                "title": model._meta.verbose_name_plural.title(),
                "app_label": model._meta.app_label,
                "model_name": model._meta.model_name,
                "url_name": "management_console:model_list",
            })

    if other_items:
        nav.append({
            "id": "other_models",
            "title": "Additional Modules",
            "icon": "bi-three-dots",
            "items": other_items,
            "shortcuts": [],
        })

    return nav


def get_field_verbose_name(model, model_admin, field_name):
    """
    Resolves human-readable column title for a list_display item.
    """
    if field_name == "__str__":
        return str(model._meta.verbose_name).title()

    # Check if defined on ModelAdmin
    if hasattr(model_admin, field_name):
        attr = getattr(model_admin, field_name)
        if hasattr(attr, "short_description"):
            return attr.short_description
        if hasattr(attr, "__doc__") and attr.__doc__:
            return attr.__doc__.strip().split("\n")[0]
        return field_name.replace("_", " ").title()

    # Check if defined on Model
    try:
        field = model._meta.get_field(field_name)
        return field.verbose_name.title()
    except FieldDoesNotExist:
        pass

    if hasattr(model, field_name):
        attr = getattr(model, field_name)
        if hasattr(attr, "short_description"):
            return attr.short_description
        return field_name.replace("_", " ").title()

    return field_name.replace("_", " ").title()


def get_field_display_value(instance, model_admin, field_name):
    """
    Renders the representation of a list_display or detail field value.
    Handles callables, boolean icons, relations, foreign keys, badges, and safe HTML.
    """
    value = None

    # 1. Check ModelAdmin callable / method
    if hasattr(model_admin, field_name):
        func = getattr(model_admin, field_name)
        if callable(func):
            try:
                # Some functions accept (self, obj), some accept (obj)
                try:
                    value = func(instance)
                except TypeError:
                    value = func(model_admin, instance)
            except Exception as e:
                return format_html('<span class="text-danger small">Error</span>')

    # 2. Check model instance method or attribute
    elif hasattr(instance, field_name):
        attr = getattr(instance, field_name)
        if callable(attr):
            try:
                value = attr()
            except Exception:
                value = None
        else:
            value = attr

    # 3. Model get_FOO_display (for choice fields)
    display_func_name = f"get_{field_name}_display"
    if value is None and hasattr(instance, display_func_name):
        try:
            value = getattr(instance, display_func_name)()
        except Exception:
            pass

    # 4. Fallback for __str__
    if field_name == "__str__" or value is None and hasattr(instance, "__str__"):
        if field_name == "__str__":
            value = str(instance)

    # Format the rendered value safely
    if value is None or value == "":
        return format_html('<span class="text-muted opacity-50">—</span>')

    # Boolean formatting
    if isinstance(value, bool):
        if value:
            return format_html('<span class="badge bg-success-subtle text-success border border-success-subtle"><i class="bi bi-check-circle-fill me-1"></i>Yes</span>')
        return format_html('<span class="badge bg-secondary-subtle text-muted border border-secondary-subtle"><i class="bi bi-x-circle me-1"></i>No</span>')

    # Already marked safe HTML (e.g. from format_html in admin method)
    if isinstance(value, SafeString):
        return value

    # Models / Foreign Keys
    if hasattr(value, "_meta"):
        return format_html('<span class="fw-semibold text-dark">{}</span>', str(value))

    # Common status strings
    val_str = str(value)
    val_lower = val_str.lower()
    if val_lower in ("active", "paid", "delivered", "approved", "completed", "verified"):
        return format_html('<span class="badge bg-success-subtle text-success border border-success-subtle">{}</span>', val_str.title())
    if val_lower in ("pending", "processing", "draft", "in_review", "hold"):
        return format_html('<span class="badge bg-warning-subtle text-warning border border-warning-subtle">{}</span>', val_str.title())
    if val_lower in ("inactive", "cancelled", "rejected", "failed", "unapproved"):
        return format_html('<span class="badge bg-danger-subtle text-danger border border-danger-subtle">{}</span>', val_str.title())

    return escape(val_str)
