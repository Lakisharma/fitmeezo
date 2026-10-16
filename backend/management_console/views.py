"""
Management Console Views.
Provides an enterprise-grade administration interface with dynamic model management,
real-time dashboard analytics, dedicated e-commerce workflows, and role-based security.
"""

from decimal import Decimal
from django.apps import apps
from django.contrib import messages
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.models import (
    Address,
    Cart,
    Category,
    CustomerProfile,
    Invoice,
    Order,
    OrderItem,
    PayoutBatch,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    RefundRecord,
    ReturnRequest,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
    ShipmentBooking,
)

from .forms import (
    ConsoleLoginForm,
    ConsoleProductForm,
    ConsoleSellerApprovalForm,
    build_model_form,
)
from .permissions import check_model_permission, console_auth_required
from .registry import (
    get_all_registered_models,
    get_field_display_value,
    get_field_verbose_name,
    get_navigation_modules,
    get_registered_model_admin,
)

User = get_user_model()


# ==============================================================================
# AUTHENTICATION VIEWS
# ==============================================================================

def console_login_view(request):
    """
    Staff authentication view for the Management Console.
    Redirects already authenticated staff to the dashboard.
    """
    if request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser):
        return redirect("management_console:dashboard")

    next_url = request.GET.get("next") or reverse("management_console:dashboard")

    if request.method == "POST":
        form = ConsoleLoginForm(request.POST)
        if form.is_valid():
            user = form.user
            login(request, user)
            messages.success(request, f"Welcome back, {user.get_full_name() or user.username}!")
            return redirect(next_url)
    else:
        form = ConsoleLoginForm()

    return render(
        request,
        "management_console/login.html",
        {
            "form": form,
            "next": next_url,
        },
    )


def console_logout_view(request):
    """
    Logs out the staff user and redirects to login.
    """
    logout(request)
    messages.info(request, "You have been securely signed out of the Management Console.")
    return redirect("management_console:login")


# ==============================================================================
# DASHBOARD VIEW
# ==============================================================================

@console_auth_required
def console_dashboard_view(request):
    """
    Management Console Dashboard.
    Presents real-time enterprise metrics computed directly from database queries.
    """
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Financial & Order KPI calculations
    revenue_agg = Order.objects.filter(payment_status="paid").aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0.00"), output_field=DecimalField())
    )
    total_revenue = revenue_agg["total"]

    total_orders = Order.objects.count()
    today_orders = Order.objects.filter(created_at__gte=today_start).count()
    pending_orders = Order.objects.filter(status__in=["pending", "confirmed", "processing"]).count()

    # Product & Inventory KPIs
    total_products = Product.objects.count()
    active_products = Product.objects.filter(is_active=True).count()
    low_stock_products = Product.objects.filter(stock_quantity__gt=0, stock_quantity__lt=10).count()
    out_of_stock_products = Product.objects.filter(stock_quantity=0).count()

    # Seller KPIs
    total_sellers = Seller.objects.count()
    active_sellers = Seller.objects.filter(is_active=True, status="approved").count()
    pending_sellers = Seller.objects.filter(status="pending").count()

    # Returns & Logistics KPIs
    pending_returns = ReturnRequest.objects.filter(status__in=["pending", "under_review"]).count()
    pending_payouts = PayoutBatch.objects.filter(status__in=["draft", "pending", "in_review", "approved"]).count()

    # Recent Data Tables
    recent_orders = (
        Order.objects.select_related("user")
        .order_by("-created_at")[:7]
    )
    recent_audit_logs = (
        LogEntry.objects.select_related("user", "content_type")
        .order_by("-action_time")[:7]
    )
    pending_sellers_list = (
        Seller.objects.filter(status="pending")
        .order_by("-created_at")[:5]
    )

    context = {
        "nav_modules": get_navigation_modules(request),
        "total_revenue": total_revenue,
        "total_orders": total_orders,
        "today_orders": today_orders,
        "pending_orders": pending_orders,
        "total_products": total_products,
        "active_products": active_products,
        "low_stock_products": low_stock_products,
        "out_of_stock_products": out_of_stock_products,
        "total_sellers": total_sellers,
        "active_sellers": active_sellers,
        "pending_sellers": pending_sellers,
        "pending_returns": pending_returns,
        "pending_payouts": pending_payouts,
        "recent_orders": recent_orders,
        "recent_audit_logs": recent_audit_logs,
        "pending_sellers_list": pending_sellers_list,
        "active_nav": "dashboard",
    }
    return render(request, "management_console/dashboard.html", context)


# ==============================================================================
# DYNAMIC MODEL LIST VIEW
# ==============================================================================

@console_auth_required
def console_model_list_view(request, app_label, model_name):
    """
    Generic Model List View matching original Django Admin list_display, search_fields,
    and list_filter for ANY registered model.
    """
    model, model_admin = get_registered_model_admin(app_label, model_name)
    if not model or not model_admin:
        raise Http404(f"Model {app_label}.{model_name} is not registered in the administration registry.")

    if not check_model_permission(request, model_admin, "view"):
        return render(
            request,
            "management_console/403.html",
            {
                "error_title": "Permission Denied",
                "error_message": f"You do not possess permission to view {model._meta.verbose_name_plural}.",
            },
            status=403,
        )

    # Base Queryset from ModelAdmin
    try:
        qs = model_admin.get_queryset(request)
    except Exception:
        qs = model.objects.all()

    # Search Logic
    search_query = request.GET.get("q", "").strip()
    search_fields = getattr(model_admin, "search_fields", ()) or ()
    if search_query and search_fields:
        search_filter = Q()
        for field in search_fields:
            # Handle lookups like =field, ^field, etc.
            lookup_field = field.lstrip("^=@")
            search_filter |= Q(**{f"{lookup_field}__icontains": search_query})
        qs = qs.filter(search_filter)

    # List Filter logic
    list_filters_config = getattr(model_admin, "list_filter", ()) or ()
    active_filters = {}
    filter_options = []

    for item in list_filters_config:
        filter_name = item if isinstance(item, str) else getattr(item, "parameter_name", None)
        if not filter_name:
            continue

        selected_val = request.GET.get(filter_name, "")
        if selected_val:
            active_filters[filter_name] = selected_val
            # Apply filter
            if selected_val.lower() in ("true", "1"):
                qs = qs.filter(**{filter_name: True})
            elif selected_val.lower() in ("false", "0"):
                qs = qs.filter(**{filter_name: False})
            else:
                try:
                    qs = qs.filter(**{filter_name: selected_val})
                except Exception:
                    pass

        # Discover choices for this filter
        try:
            field_obj = model._meta.get_field(filter_name)
            if hasattr(field_obj, "choices") and field_obj.choices:
                filter_options.append({
                    "name": filter_name,
                    "title": field_obj.verbose_name.title(),
                    "selected": selected_val,
                    "choices": field_obj.choices,
                })
            elif field_obj.get_internal_type() == "BooleanField":
                filter_options.append({
                    "name": filter_name,
                    "title": field_obj.verbose_name.title(),
                    "selected": selected_val,
                    "choices": [("true", "Yes"), ("false", "No")],
                })
        except Exception:
            pass

    # Sorting
    order_by = request.GET.get("o")
    if order_by:
        try:
            qs = qs.order_by(order_by)
        except Exception:
            pass

    # Pagination
    paginator = Paginator(qs, 25)
    page_num = request.GET.get("page", 1)
    try:
        page_obj = paginator.get_page(page_num)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.get_page(1)

    # Determine List Display Columns
    list_display = getattr(model_admin, "list_display", ("__str__",))
    if not list_display:
        list_display = ("__str__",)

    columns = [
        {
            "name": col,
            "title": get_field_verbose_name(model, model_admin, col),
            "sortable": hasattr(model, col) and not callable(getattr(model, col, None)),
        }
        for col in list_display
    ]

    # Pre-render row items
    can_change = check_model_permission(request, model_admin, "change")
    can_delete = check_model_permission(request, model_admin, "delete")
    rows = []
    for obj in page_obj:
        cells = []
        for col in list_display:
            val = get_field_display_value(obj, model_admin, col)
            cells.append(val)
        rows.append({
            "obj": obj,
            "pk": obj.pk,
            "str": str(obj),
            "cells": cells,
            "change_url": reverse("management_console:model_change", args=[app_label, model_name, obj.pk]),
            "delete_url": reverse("management_console:model_delete", args=[app_label, model_name, obj.pk]) if can_delete else None,
        })

    can_add = check_model_permission(request, model_admin, "add")

    context = {
        "nav_modules": get_navigation_modules(request),
        "app_label": app_label,
        "model_name": model_name,
        "verbose_name": model._meta.verbose_name,
        "verbose_name_plural": model._meta.verbose_name_plural,
        "columns": columns,
        "rows": rows,
        "page_obj": page_obj,
        "paginator": paginator,
        "total_count": paginator.count,
        "search_query": search_query,
        "search_fields": search_fields,
        "filter_options": filter_options,
        "can_add": can_add,
        "can_change": can_change,
        "can_delete": can_delete,
        "add_url": reverse("management_console:model_add", args=[app_label, model_name]) if can_add else None,
        "active_nav": f"{app_label}_{model_name}",
    }
    return render(request, "management_console/model_list.html", context)


# ==============================================================================
# DYNAMIC MODEL ADD VIEW
# ==============================================================================

@console_auth_required
def console_model_add_view(request, app_label, model_name):
    """
    Generic Model Add View with field validation, CSRF protection, and audit logging.
    """
    model, model_admin = get_registered_model_admin(app_label, model_name)
    if not model or not model_admin:
        raise Http404(f"Model {app_label}.{model_name} is not registered.")

    if not check_model_permission(request, model_admin, "add"):
        return render(
            request,
            "management_console/403.html",
            {
                "error_title": "Permission Denied",
                "error_message": f"You do not possess permission to add a new {model._meta.verbose_name}.",
            },
            status=403,
        )

    FormClass, readonly_fields = build_model_form(model, model_admin, request)

    if request.method == "POST":
        form = FormClass(request.POST, request.FILES)
        if form.is_valid():
            with transaction.atomic():
                instance = form.save()
                # Audit logging
                try:
                    LogEntry.objects.create(
                        user=request.user,
                        content_type=ContentType.objects.get_for_model(model),
                        object_id=str(instance.pk),
                        object_repr=str(instance)[:200],
                        action_flag=ADDITION,
                        change_message="Created via Fitmeezo Management Console.",
                    )
                except Exception:
                    pass

            messages.success(request, f"Successfully created {model._meta.verbose_name} '{instance}'.")
            if "_addanother" in request.POST:
                return redirect("management_console:model_add", app_label=app_label, model_name=model_name)
            return redirect("management_console:model_list", app_label=app_label, model_name=model_name)
        else:
            messages.error(request, "Please correct the errors indicated below.")
    else:
        form = FormClass()

    context = {
        "nav_modules": get_navigation_modules(request),
        "app_label": app_label,
        "model_name": model_name,
        "verbose_name": model._meta.verbose_name,
        "verbose_name_plural": model._meta.verbose_name_plural,
        "form": form,
        "is_add": True,
        "readonly_fields": readonly_fields,
        "back_url": reverse("management_console:model_list", args=[app_label, model_name]),
        "active_nav": f"{app_label}_{model_name}",
    }
    return render(request, "management_console/model_form.html", context)


# ==============================================================================
# DYNAMIC MODEL CHANGE & DETAIL VIEW
# ==============================================================================

@console_auth_required
def console_model_change_view(request, app_label, model_name, pk):
    """
    Generic Model Detail & Change View.
    Renders read-only controls if user only has view permission.
    """
    model, model_admin = get_registered_model_admin(app_label, model_name)
    if not model or not model_admin:
        raise Http404(f"Model {app_label}.{model_name} is not registered.")

    instance = get_object_or_404(model, pk=pk)

    can_view = check_model_permission(request, model_admin, "view", instance)
    can_change = check_model_permission(request, model_admin, "change", instance)
    can_delete = check_model_permission(request, model_admin, "delete", instance)

    if not can_view and not can_change:
        return render(
            request,
            "management_console/403.html",
            {
                "error_title": "Permission Denied",
                "error_message": f"You do not possess permission to view this {model._meta.verbose_name}.",
            },
            status=403,
        )

    FormClass, readonly_fields = build_model_form(model, model_admin, request, instance)

    if request.method == "POST":
        if not can_change:
            return HttpResponseForbidden("You do not possess permission to modify this record.")

        form = FormClass(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            with transaction.atomic():
                instance = form.save()
                # Audit logging
                try:
                    LogEntry.objects.create(
                        user=request.user,
                        content_type=ContentType.objects.get_for_model(model),
                        object_id=str(instance.pk),
                        object_repr=str(instance)[:200],
                        action_flag=CHANGE,
                        change_message="Updated via Fitmeezo Management Console.",
                    )
                except Exception:
                    pass

            messages.success(request, f"Successfully updated {model._meta.verbose_name} '{instance}'.")
            if "_continue" in request.POST:
                return redirect("management_console:model_change", app_label=app_label, model_name=model_name, pk=pk)
            return redirect("management_console:model_list", app_label=app_label, model_name=model_name)
        else:
            messages.error(request, "Please correct the errors indicated below.")
    else:
        form = FormClass(instance=instance)
        if not can_change:
            for field in form.fields.values():
                field.disabled = True

    # Audit history for this instance
    object_history = (
        LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(model),
            object_id=str(instance.pk),
        )
        .select_related("user")
        .order_by("-action_time")[:10]
    )

    context = {
        "nav_modules": get_navigation_modules(request),
        "app_label": app_label,
        "model_name": model_name,
        "verbose_name": model._meta.verbose_name,
        "verbose_name_plural": model._meta.verbose_name_plural,
        "instance": instance,
        "form": form,
        "is_add": False,
        "can_change": can_change,
        "can_delete": can_delete,
        "readonly_fields": readonly_fields,
        "object_history": object_history,
        "back_url": reverse("management_console:model_list", args=[app_label, model_name]),
        "delete_url": reverse("management_console:model_delete", args=[app_label, model_name, pk]) if can_delete else None,
        "active_nav": f"{app_label}_{model_name}",
    }
    return render(request, "management_console/model_form.html", context)


# ==============================================================================
# DYNAMIC MODEL DELETE CONFIRMATION VIEW
# ==============================================================================

@console_auth_required
def console_model_delete_view(request, app_label, model_name, pk):
    """
    Generic Model Delete Confirmation View with cascade relationship warnings and POST-only deletion.
    """
    model, model_admin = get_registered_model_admin(app_label, model_name)
    if not model or not model_admin:
        raise Http404(f"Model {app_label}.{model_name} is not registered.")

    instance = get_object_or_404(model, pk=pk)

    if not check_model_permission(request, model_admin, "delete", instance):
        return render(
            request,
            "management_console/403.html",
            {
                "error_title": "Permission Denied",
                "error_message": f"You do not possess permission to delete this {model._meta.verbose_name}.",
            },
            status=403,
        )

    if request.method == "POST":
        object_repr = str(instance)[:200]
        content_type = ContentType.objects.get_for_model(model)
        with transaction.atomic():
            instance.delete()
            try:
                LogEntry.objects.create(
                    user=request.user,
                    content_type=content_type,
                    object_id=str(pk),
                    object_repr=object_repr,
                    action_flag=DELETION,
                    change_message="Deleted via Fitmeezo Management Console.",
                )
            except Exception:
                pass

        messages.success(request, f"Successfully deleted {model._meta.verbose_name} '{object_repr}'.")
        return redirect("management_console:model_list", app_label=app_label, model_name=model_name)

    context = {
        "nav_modules": get_navigation_modules(request),
        "app_label": app_label,
        "model_name": model_name,
        "verbose_name": model._meta.verbose_name,
        "instance": instance,
        "back_url": reverse("management_console:model_change", args=[app_label, model_name, pk]),
        "active_nav": f"{app_label}_{model_name}",
    }
    return render(request, "management_console/model_delete.html", context)


# ==============================================================================
# DEDICATED E-COMMERCE WORKFLOW VIEWS
# ==============================================================================

@console_auth_required
def console_product_add_view(request):
    """
    Dedicated rich Add Product screen supporting variant creation and gallery inlines.
    """
    if request.method == "POST":
        form = ConsoleProductForm(request.POST, request.FILES)
        if form.is_valid():
            with transaction.atomic():
                product = form.save()

                # Process optional inline variants from POST
                variant_sku = request.POST.get("variant_sku", "").strip()
                variant_val = request.POST.get("variant_value", "").strip()
                variant_type = request.POST.get("variant_type", "Size").strip()
                variant_price = request.POST.get("variant_price", "").strip()
                variant_stock = request.POST.get("variant_stock", "").strip()

                if variant_sku and variant_val and variant_price:
                    try:
                        ProductVariant.objects.create(
                            product=product,
                            sku=variant_sku,
                            option_type=variant_type,
                            option_value=variant_val,
                            price=Decimal(variant_price),
                            stock_quantity=int(variant_stock or 0),
                            is_active=True,
                        )
                    except Exception:
                        pass

                # Audit Log
                try:
                    LogEntry.objects.create(
                        user=request.user,
                        content_type=ContentType.objects.get_for_model(Product),
                        object_id=str(product.pk),
                        object_repr=str(product)[:200],
                        action_flag=ADDITION,
                        change_message="Product created via Management Console Product Studio.",
                    )
                except Exception:
                    pass

            messages.success(request, f"Product '{product.name}' was created successfully!")
            return redirect("management_console:model_list", app_label="core", model_name="product")
        else:
            messages.error(request, "Validation failed. Please review the errors below.")
    else:
        form = ConsoleProductForm()

    context = {
        "nav_modules": get_navigation_modules(request),
        "form": form,
        "categories": Category.objects.filter(is_active=True),
        "sellers": Seller.objects.filter(is_active=True),
        "active_nav": "product_add",
    }
    return render(request, "management_console/product_add.html", context)


@console_auth_required
def console_inventory_monitor_view(request):
    """
    Inventory and Stock Monitor.
    Displays stock levels across products and variants, with one-click adjustments.
    """
    products = Product.objects.select_related("category", "seller").prefetch_related("variants").order_by("stock_quantity")
    
    # Filtering
    stock_status = request.GET.get("status")
    if stock_status == "out":
        products = products.filter(stock_quantity=0)
    elif stock_status == "low":
        products = products.filter(stock_quantity__gt=0, stock_quantity__lt=10)
    elif stock_status == "in":
        products = products.filter(stock_quantity__gte=10)

    context = {
        "nav_modules": get_navigation_modules(request),
        "products": products[:50],
        "stock_status": stock_status,
        "active_nav": "inventory_monitor",
    }
    return render(request, "management_console/inventory.html", context)


@console_auth_required
def console_seller_approvals_view(request):
    """
    Seller Approvals and Verification Workbench.
    Allows administrators to inspect pending sellers, approve/reject, and adjust commission rates.
    """
    sellers = Seller.objects.order_by("-created_at")
    filter_status = request.GET.get("status", "pending")
    if filter_status and filter_status != "all":
        sellers = sellers.filter(status=filter_status)

    if request.method == "POST":
        seller_id = request.POST.get("seller_id")
        action = request.POST.get("action")
        seller = get_object_or_404(Seller, pk=seller_id)

        with transaction.atomic():
            if action == "approve":
                seller.status = "approved"
                seller.is_active = True
                seller.approved_at = timezone.now()
                seller.save(update_fields=["status", "is_active", "approved_at", "updated_at"])
                messages.success(request, f"Seller '{seller.store_name}' has been approved and activated.")
            elif action == "reject":
                seller.status = "rejected"
                seller.is_active = False
                seller.admin_notes = request.POST.get("reason", "Rejected during admin verification.")
                seller.save(update_fields=["status", "is_active", "admin_notes", "updated_at"])
                messages.warning(request, f"Seller '{seller.store_name}' was marked as rejected.")
            elif action == "update_commission":
                rate = request.POST.get("commission_rate")
                if rate:
                    seller.commission_rate = Decimal(rate)
                    seller.save(update_fields=["commission_rate", "updated_at"])
                    messages.success(request, f"Commission rate for '{seller.store_name}' updated to {rate}%.")

        return redirect("management_console:seller_approvals")

    context = {
        "nav_modules": get_navigation_modules(request),
        "sellers": sellers,
        "filter_status": filter_status,
        "active_nav": "seller_approvals",
    }
    return render(request, "management_console/sellers_approvals.html", context)


@console_auth_required
def console_order_detail_view(request, pk):
    """
    Dedicated Order Detail and Fulfillment Console.
    """
    order = get_object_or_404(
        Order.objects.select_related("user").prefetch_related("items__product", "seller_orders__seller"),
        pk=pk,
    )

    if request.method == "POST":
        action = request.POST.get("action")
        with transaction.atomic():
            if action == "update_status":
                new_status = request.POST.get("status")
                if new_status in dict(Order.STATUS_CHOICES):
                    order.status = new_status
                    order.save(update_fields=["status", "updated_at"])
                    messages.success(request, f"Order status updated to '{order.get_status_display()}'.")
            elif action == "update_payment":
                new_payment = request.POST.get("payment_status")
                if new_payment in dict(Order.PAYMENT_STATUS_CHOICES):
                    order.payment_status = new_payment
                    if new_payment == "paid" and not order.paid_at:
                        order.paid_at = timezone.now()
                    order.save(update_fields=["payment_status", "paid_at", "updated_at"])
                    messages.success(request, f"Payment status updated to '{order.get_payment_status_display()}'.")

        return redirect("management_console:order_detail", pk=pk)

    context = {
        "nav_modules": get_navigation_modules(request),
        "order": order,
        "items": order.items.all(),
        "seller_orders": order.seller_orders.all(),
        "status_choices": Order.STATUS_CHOICES,
        "payment_choices": Order.PAYMENT_STATUS_CHOICES,
        "active_nav": "core_order",
    }
    return render(request, "management_console/order_detail.html", context)


@console_auth_required
def console_audit_logs_view(request):
    """
    Enterprise Audit Log Viewer.
    """
    logs = LogEntry.objects.select_related("user", "content_type").order_by("-action_time")
    action_filter = request.GET.get("action")
    if action_filter:
        try:
            logs = logs.filter(action_flag=int(action_filter))
        except ValueError:
            pass

    user_filter = request.GET.get("user")
    if user_filter:
        logs = logs.filter(user__username=user_filter)

    paginator = Paginator(logs, 30)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    context = {
        "nav_modules": get_navigation_modules(request),
        "page_obj": page_obj,
        "action_filter": action_filter,
        "user_filter": user_filter,
        "active_nav": "audit_logs",
    }
    return render(request, "management_console/audit_logs.html", context)


# ==============================================================================
# GLOBAL DATABASE SEARCH VIEW
# ==============================================================================

@console_auth_required
def console_global_search_view(request):
    """
    Database-backed multi-model search.
    Searches across Products, Orders, Customers, Sellers, Categories, and Invoices.
    """
    query = request.GET.get("q", "").strip()
    results = {
        "products": [],
        "orders": [],
        "sellers": [],
        "customers": [],
        "categories": [],
        "invoices": [],
    }
    total_found = 0

    if query and len(query) >= 2:
        # 1. Products
        products = Product.objects.filter(
            Q(name__icontains=query) | Q(sku__icontains=query) | Q(description__icontains=query)
        )[:10]
        results["products"] = products
        total_found += len(products)

        # 2. Orders
        orders = Order.objects.filter(
            Q(order_number__icontains=query) | Q(shipping_full_name__icontains=query) | Q(shipping_phone_number__icontains=query)
        )[:10]
        results["orders"] = orders
        total_found += len(orders)

        # 3. Sellers
        sellers = Seller.objects.filter(
            Q(store_name__icontains=query) | Q(legal_business_name__icontains=query) | Q(support_email__icontains=query)
        )[:10]
        results["sellers"] = sellers
        total_found += len(sellers)

        # 4. Customers
        customers = User.objects.filter(
            Q(username__icontains=query) | Q(email__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query)
        )[:10]
        results["customers"] = customers
        total_found += len(customers)

        # 5. Categories
        categories = Category.objects.filter(name__icontains=query)[:10]
        results["categories"] = categories
        total_found += len(categories)

        # 6. Invoices
        invoices = Invoice.objects.filter(
            Q(invoice_number__icontains=query) | Q(recipient_name__icontains=query)
        )[:10]
        results["invoices"] = invoices
        total_found += len(invoices)

    context = {
        "nav_modules": get_navigation_modules(request),
        "query": query,
        "results": results,
        "total_found": total_found,
        "active_nav": "search",
    }
    return render(request, "management_console/search_results.html", context)
