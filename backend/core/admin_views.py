"""
Fitmeezo Custom Admin Dashboard - Views & Controllers.
Provides full administrative management for store overview, catalogue,
categories, and customer order fulfillment.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
from django.contrib import messages
import csv
from django.contrib.auth import login, logout, get_user_model
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .admin_forms import (
    AdminCategoryForm,
    AdminCODReconcileForm,
    AdminLoginForm,
    AdminManualAdjustmentForm,
    AdminOrderPaymentStatusForm,
    AdminOrderStatusForm,
    AdminPayoutHoldForm,
    AdminPayoutProcessForm,
    AdminPayoutRejectForm,
    AdminPayoutReverseForm,
    AdminProductForm,
    AdminProductImageForm,
    AdminProductVariantForm,
)
from .models import (
    Category,
    CODReceiptRecord,
    Order,
    OrderItem,
    PayoutAuditLog,
    PayoutBatch,
    Product,
    ProductImage,
    ProductVariant,
    RefundRecord,
    ReturnAuditLog,
    ReturnRequest,
    ReturnRequestItem,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
    Invoice,
)
from .return_services import (
    transition_return_status,
    process_simulated_refund,
    VALID_TRANSITIONS,
    ReturnServiceError,
    ReturnValidationError,
    RefundError,
)
from .ledger_services import (
    approve_payout_batch,
    calculate_seller_payout_eligibility,
    create_manual_adjustment,
    create_payout_batch,
    get_cycle_bounds,
    get_seller_ledger_summary,
    hold_payout_batch,
    process_mock_payout_batch,
    reconcile_cod_remittance,
    reject_payout_batch,
    release_payout_batch,
    reverse_payout_batch,
    LedgerServiceError,
)
from .payout_adapters import get_payout_system_status
from .forms import AdminBankVerificationForm
from .seller_services import sanitize_csv_cell
from .services import cancel_order

User = get_user_model()


from django.contrib.admin.models import LogEntry
from .admin_permissions import (
    ALL_ROLES,
    ROLE_FINANCE_MANAGER,
    ROLE_PRODUCT_MANAGER,
    ROLE_SELLER_MANAGER,
    ROLE_SUPER_ADMIN,
    ROLE_SUPPORT_MANAGER,
    admin_role_required,
    get_user_role,
    has_admin_permission,
    log_admin_action,
)


def admin_required(view_func=None, allowed_roles=None):
    """
    Decorator requiring active staff or superuser credentials and optional role check.
    Redirects unauthenticated users to the custom admin login page.
    Returns HTTP 403 Forbidden with custom_admin/403.html to unauthorized users.
    Supports:
        @admin_required
        @admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
    """
    def decorator(fn):
        @wraps(fn)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                login_url = reverse("custom_admin:login")
                return redirect(f"{login_url}?next={request.get_full_path()}")
            if not request.user.is_active or not (request.user.is_staff or request.user.is_superuser):
                return render(
                    request,
                    "custom_admin/403.html",
                    {
                        "required_roles": allowed_roles or ["Staff / Administrator"],
                        "user_role": "None",
                        "error_message": "Access Denied: You must possess an active Staff or Administrator account to access this page.",
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
            return fn(request, *args, **kwargs)

        return _wrapped

    if view_func is not None and callable(view_func):
        return decorator(view_func)
    return decorator


# ==============================================================================
# AUTHENTICATION VIEWS
# ==============================================================================

def admin_login_view(request):
    """
    Dedicated staff login view.
    """
    if request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser):
        return redirect("custom_admin:dashboard")

    next_url = request.GET.get("next") or request.POST.get("next") or reverse("custom_admin:dashboard")

    if request.method == "POST":
        form = AdminLoginForm(request.POST)
        if form.is_valid():
            user = form.cleaned_data["user"]
            remember_me = form.cleaned_data.get("remember_me", False)

            login(request, user)
            if not remember_me:
                request.session.set_expiry(0)  # Session expires on browser close
            else:
                request.session.set_expiry(1209600)  # 2 weeks

            messages.success(request, f"Welcome back, {user.first_name or user.username}!")
            return redirect(next_url)
    else:
        form = AdminLoginForm()

    context = {
        "form": form,
        "next": next_url,
    }
    return render(request, "custom_admin/login.html", context)


@require_POST
def admin_logout_view(request):
    """
    Safely logs out the staff user and redirects to the admin login page.
    """
    logout(request)
    messages.info(request, "You have been successfully signed out of the management dashboard.")
    return redirect("custom_admin:login")


# ==============================================================================
# DASHBOARD OVERVIEW VIEW
# ==============================================================================

@admin_required
def admin_dashboard_view(request):
    """
    Main store performance overview displaying live database metrics.
    """
    period = request.GET.get("period", "all").strip().lower()
    start_date = None
    now = timezone.now()

    if period == "today":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start_date = now - timedelta(days=7)
    elif period == "30d":
        start_date = now - timedelta(days=30)
    elif period == "this_month":
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # 1. Product & catalogue metrics
    total_products = Product.objects.count()
    active_products = Product.objects.filter(is_active=True).count()
    pending_product_approvals = Product.objects.filter(approval_status="pending").count()
    total_categories = Category.objects.count()
    active_categories = Category.objects.filter(is_active=True).count()

    # Accurate total inventory units across products & variants
    variant_stock = ProductVariant.objects.filter(is_active=True).aggregate(
        total=Sum("stock_quantity")
    )["total"] or 0
    standalone_stock = Product.objects.filter(variants__isnull=True).aggregate(
        total=Sum("stock_quantity")
    )["total"] or 0
    total_inventory = variant_stock + standalone_stock

    # 2. Customer & Seller metrics
    total_customers = User.objects.filter(is_staff=False, is_superuser=False).count()
    total_sellers = Seller.objects.count()
    pending_seller_approvals = Seller.objects.filter(status=Seller.STATUS_PENDING).count()
    approved_sellers = Seller.objects.filter(status=Seller.STATUS_APPROVED).count()
    suspended_sellers = Seller.objects.filter(status=Seller.STATUS_SUSPENDED).count()

    # 3. Order metrics (filtered if date range selected)
    orders_qs = Order.objects.all()
    if start_date:
        orders_qs = orders_qs.filter(created_at__gte=start_date)

    total_orders = orders_qs.count()
    pending_orders = orders_qs.filter(status=Order.STATUS_PENDING).count()
    processing_orders = orders_qs.filter(status=Order.STATUS_PROCESSING).count()
    shipped_orders = orders_qs.filter(status=Order.STATUS_SHIPPED).count()
    delivered_orders = orders_qs.filter(status=Order.STATUS_DELIVERED).count()
    cancelled_orders = orders_qs.filter(status=Order.STATUS_CANCELLED).count()

    # Financial revenue: sum of total_amount for non-cancelled orders
    total_revenue = orders_qs.exclude(status=Order.STATUS_CANCELLED).aggregate(
        total=Sum("total_amount")
    )["total"] or Decimal("0.00")

    # 4. Payout metrics
    pending_payouts_count = PayoutBatch.objects.filter(status="pending").count()
    completed_payouts_count = PayoutBatch.objects.filter(status="completed").count()
    failed_payouts_count = PayoutBatch.objects.filter(status="failed").count()
    total_payouts_disbursed = PayoutBatch.objects.filter(status="completed").aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")

    # 5. Recent orders list
    recent_orders = (
        Order.objects.select_related("user")
        .prefetch_related("items")
        .order_by("-created_at")[:8]
    )

    # 6. Recent sellers registered
    recent_sellers = (
        Seller.objects.select_related("user")
        .order_by("-created_at")[:5]
    )

    # 7. Low-stock products (inventory < 10)
    low_stock_products = []
    for prod in Product.objects.filter(is_active=True).prefetch_related("variants")[:50]:
        if prod.available_inventory < 10:
            low_stock_products.append(prod)
        if len(low_stock_products) >= 6:
            break

    pending_bank_verifications = Seller.objects.filter(bank_verification_status="pending").order_by("-updated_at")
    pending_bank_verifications_count = pending_bank_verifications.count()

    # 8. Recent administrative activity logs
    recent_activities = LogEntry.objects.select_related("user", "content_type").order_by("-action_time")[:8]

    # 9. 7-day trend chart data for visualization
    chart_days = []
    chart_counts = []
    chart_revenues = []
    for i in range(6, -1, -1):
        day_date = (now - timedelta(days=i)).date()
        day_orders = Order.objects.filter(created_at__date=day_date)
        count = day_orders.count()
        rev = day_orders.exclude(status="cancelled").aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")
        chart_days.append(day_date.strftime("%b %d"))
        chart_counts.append(count)
        chart_revenues.append(float(rev))

    context = {
        "period": period,
        "total_products": total_products,
        "active_products": active_products,
        "pending_product_approvals": pending_product_approvals,
        "total_categories": total_categories,
        "active_categories": active_categories,
        "total_customers": total_customers,
        "total_sellers": total_sellers,
        "pending_seller_approvals": pending_seller_approvals,
        "approved_sellers": approved_sellers,
        "suspended_sellers": suspended_sellers,
        "total_orders": total_orders,
        "pending_orders": pending_orders,
        "processing_orders": processing_orders,
        "shipped_orders": shipped_orders,
        "delivered_orders": delivered_orders,
        "cancelled_orders": cancelled_orders,
        "total_inventory": total_inventory,
        "total_revenue": total_revenue,
        "pending_payouts_count": pending_payouts_count,
        "completed_payouts_count": completed_payouts_count,
        "failed_payouts_count": failed_payouts_count,
        "total_payouts_disbursed": total_payouts_disbursed,
        "recent_orders": recent_orders,
        "recent_sellers": recent_sellers,
        "recent_activities": recent_activities,
        "low_stock_products": low_stock_products,
        "pending_bank_verifications": pending_bank_verifications,
        "pending_bank_verifications_count": pending_bank_verifications_count,
        "chart_days": chart_days,
        "chart_counts": chart_counts,
        "chart_revenues": chart_revenues,
    }
    return render(request, "custom_admin/dashboard.html", context)


# ==============================================================================
# PRODUCT MANAGEMENT VIEWS
# ==============================================================================

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_product_list_view(request):
    """
    Catalogue listing with search, category/status filters, and pagination.
    """
    products = Product.objects.select_related("category").prefetch_related("variants").order_by("-created_at")

    # Search filter
    q = request.GET.get("q", "").strip()
    if q:
        products = products.filter(
            Q(name__icontains=q) | Q(sku__icontains=q) | Q(category__name__icontains=q)
        )

    # Category filter
    category_slug = request.GET.get("category", "").strip()
    if category_slug:
        products = products.filter(category__slug=category_slug)

    # Active status filter
    status_filter = request.GET.get("status", "").strip()
    if status_filter == "active":
        products = products.filter(is_active=True)
    elif status_filter == "inactive":
        products = products.filter(is_active=False)

    # Inventory level filter
    stock_filter = request.GET.get("stock", "").strip()
    if stock_filter == "out_of_stock":
        products = [p for p in products if p.available_inventory == 0]
    elif stock_filter == "low_stock":
        products = [p for p in products if 0 < p.available_inventory < 10]
    elif stock_filter == "in_stock":
        products = [p for p in products if p.available_inventory >= 10]

    paginator = Paginator(products, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    categories = Category.objects.filter(is_active=True).order_by("name")

    context = {
        "page_obj": page_obj,
        "categories": categories,
        "q": q,
        "category_slug": category_slug,
        "status_filter": status_filter,
        "stock_filter": stock_filter,
    }
    return render(request, "custom_admin/products/list.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_product_add_view(request):
    """
    Creates a new catalogue product.
    """
    if request.method == "POST":
        form = AdminProductForm(request.POST, request.FILES)
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' was created successfully.")
            return redirect("custom_admin:product_detail", pk=product.pk)
    else:
        form = AdminProductForm()

    context = {
        "form": form,
        "is_create": True,
    }
    return render(request, "custom_admin/products/form.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_product_detail_view(request, pk):
    """
    Displays full details, gallery images, variants, and rating metrics for a product.
    """
    product = get_object_or_404(
        Product.objects.select_related("category").prefetch_related("images", "variants"),
        pk=pk,
    )
    image_form = AdminProductImageForm()
    variant_form = AdminProductVariantForm(product=product)

    context = {
        "product": product,
        "image_form": image_form,
        "variant_form": variant_form,
    }
    return render(request, "custom_admin/products/detail.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_product_edit_view(request, pk):
    """
    Modifies an existing product's attributes.
    """
    product = get_object_or_404(Product, pk=pk)

    if request.method == "POST":
        form = AdminProductForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, f"Product '{product.name}' was updated successfully.")
            return redirect("custom_admin:product_detail", pk=product.pk)
    else:
        form = AdminProductForm(instance=product)

    context = {
        "form": form,
        "product": product,
        "is_create": False,
    }
    return render(request, "custom_admin/products/form.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_product_toggle_status_view(request, pk):
    """
    Safely toggles active/inactive state of a product without deleting historical records.
    """
    product = get_object_or_404(Product, pk=pk)
    product.is_active = not product.is_active
    product.save(update_fields=["is_active", "updated_at"])

    status_label = "activated" if product.is_active else "deactivated"
    messages.success(request, f"Product '{product.name}' was safely {status_label}.")

    redirect_url = request.META.get("HTTP_REFERER") or reverse("custom_admin:product_list")
    return redirect(redirect_url)


# ------------------------------------------------------------------------------
# Gallery Images & Variants Sub-actions
# ------------------------------------------------------------------------------

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_product_image_add_view(request, pk):
    """
    Uploads a gallery image to the product showcase.
    """
    product = get_object_or_404(Product, pk=pk)
    form = AdminProductImageForm(request.POST, request.FILES)
    if form.is_valid():
        img = form.save(commit=False)
        img.product = product
        img.save()
        messages.success(request, "Gallery photograph added successfully.")
    else:
        messages.error(request, "Failed to upload image. Please verify file format and size.")

    return redirect("custom_admin:product_detail", pk=product.pk)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_product_image_delete_view(request, pk, image_pk):
    """
    Deletes a specific gallery image.
    """
    product = get_object_or_404(Product, pk=pk)
    img = get_object_or_404(ProductImage, pk=image_pk, product=product)
    img.delete()
    messages.success(request, "Gallery photograph removed.")
    return redirect("custom_admin:product_detail", pk=product.pk)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_product_variant_add_view(request, pk):
    """
    Adds a new purchasable variant option to a product.
    """
    product = get_object_or_404(Product, pk=pk)
    form = AdminProductVariantForm(request.POST, product=product)
    if form.is_valid():
        variant = form.save(commit=False)
        variant.product = product
        variant.save()
        messages.success(request, f"Variant '{variant.option_type}: {variant.option_value}' created.")
    else:
        error_msg = "; ".join([f"{f}: {e[0]}" for f, e in form.errors.items()])
        messages.error(request, f"Could not create variant: {error_msg}")

    return redirect("custom_admin:product_detail", pk=product.pk)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_product_variant_edit_view(request, pk, variant_pk):
    """
    Edits an existing product variant.
    """
    product = get_object_or_404(Product, pk=pk)
    variant = get_object_or_404(ProductVariant, pk=variant_pk, product=product)

    if request.method == "POST":
        form = AdminProductVariantForm(request.POST, instance=variant, product=product)
        if form.is_valid():
            form.save()
            messages.success(request, f"Variant '{variant.option_type}: {variant.option_value}' updated.")
            return redirect("custom_admin:product_detail", pk=product.pk)
    else:
        form = AdminProductVariantForm(instance=variant, product=product)

    context = {
        "form": form,
        "product": product,
        "variant": variant,
    }
    return render(request, "custom_admin/products/variant_edit.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_product_variant_toggle_view(request, pk, variant_pk):
    """
    Toggles variant active status.
    """
    product = get_object_or_404(Product, pk=pk)
    variant = get_object_or_404(ProductVariant, pk=variant_pk, product=product)
    variant.is_active = not variant.is_active
    variant.save(update_fields=["is_active", "updated_at"])
    status_label = "activated" if variant.is_active else "deactivated"
    messages.success(request, f"Variant '{variant.sku}' was {status_label}.")
    return redirect("custom_admin:product_detail", pk=product.pk)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_product_variant_delete_view(request, pk, variant_pk):
    """
    Safely removes a product variant.
    """
    product = get_object_or_404(Product, pk=pk)
    variant = get_object_or_404(ProductVariant, pk=variant_pk, product=product)
    variant_sku = variant.sku
    variant.delete()
    messages.success(request, f"Variant '{variant_sku}' has been deleted.")
    return redirect("custom_admin:product_detail", pk=product.pk)


# ==============================================================================
# CATEGORY MANAGEMENT VIEWS
# ==============================================================================

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_category_list_view(request):
    """
    Lists categories with product counts, active status, and reordering weights.
    """
    categories = Category.objects.annotate(product_count=Count("products")).order_by("display_order", "name")

    q = request.GET.get("q", "").strip()
    if q:
        categories = categories.filter(Q(name__icontains=q) | Q(slug__icontains=q))

    context = {
        "categories": categories,
        "q": q,
    }
    return render(request, "custom_admin/categories/list.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_category_add_view(request):
    """
    Adds a new catalogue category.
    """
    if request.method == "POST":
        form = AdminCategoryForm(request.POST, request.FILES)
        if form.is_valid():
            cat = form.save()
            messages.success(request, f"Category '{cat.name}' created successfully.")
            return redirect("custom_admin:category_list")
    else:
        form = AdminCategoryForm()

    context = {
        "form": form,
        "is_create": True,
    }
    return render(request, "custom_admin/categories/form.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_category_edit_view(request, pk):
    """
    Modifies an existing category.
    """
    category = get_object_or_404(Category, pk=pk)

    if request.method == "POST":
        form = AdminCategoryForm(request.POST, request.FILES, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, f"Category '{category.name}' updated successfully.")
            return redirect("custom_admin:category_list")
    else:
        form = AdminCategoryForm(instance=category)

    context = {
        "form": form,
        "category": category,
        "is_create": False,
    }
    return render(request, "custom_admin/categories/form.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_category_toggle_status_view(request, pk):
    """
    Toggles category active status.
    """
    category = get_object_or_404(Category, pk=pk)
    category.is_active = not category.is_active
    category.save(update_fields=["is_active", "updated_at"])

    status_label = "activated" if category.is_active else "deactivated"
    messages.success(request, f"Category '{category.name}' was {status_label}.")
    return redirect("custom_admin:category_list")


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
@require_POST
def admin_category_delete_view(request, pk):
    """
    Safely deletes a category ONLY if no products are linked.
    Prevents cascade deletion of catalogue products.
    """
    category = get_object_or_404(Category, pk=pk)
    linked_products_count = category.products.count()

    if linked_products_count > 0:
        messages.error(
            request,
            f"Cannot delete category '{category.name}': {linked_products_count} product(s) are assigned to it. "
            "Please reassign or deactivate the category instead.",
        )
        return redirect("custom_admin:category_list")

    cat_name = category.name
    category.delete()
    messages.success(request, f"Category '{cat_name}' has been safely deleted.")
    return redirect("custom_admin:category_list")


# ==============================================================================
# ORDER MANAGEMENT VIEWS
# ==============================================================================

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, ROLE_SUPPORT_MANAGER])
def admin_order_list_view(request):
    """
    Orders listing with search, status filters, payment filters, and pagination.
    """
    orders = Order.objects.select_related("user").prefetch_related("items").order_by("-created_at")

    # Search filter
    q = request.GET.get("q", "").strip()
    if q:
        orders = orders.filter(
            Q(order_number__icontains=q)
            | Q(user__username__icontains=q)
            | Q(user__email__icontains=q)
            | Q(shipping_full_name__icontains=q)
            | Q(shipping_phone_number__icontains=q)
            | Q(seller_orders__seller__store_name__icontains=q)
        ).distinct()

    date_filter = request.GET.get("date", "").strip()
    if date_filter:
        try:
            target_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            orders = orders.filter(created_at__date=target_date)
        except Exception:
            pass

    # Order fulfillment status filter
    status_filter = request.GET.get("status", "").strip()
    if status_filter:
        orders = orders.filter(status=status_filter)

    # Payment status filter
    payment_status_filter = request.GET.get("payment_status", "").strip()
    if payment_status_filter:
        orders = orders.filter(payment_status=payment_status_filter)

    # Payment method filter
    payment_method_filter = request.GET.get("payment_method", "").strip()
    if payment_method_filter:
        orders = orders.filter(payment_method=payment_method_filter)

    paginator = Paginator(orders, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "q": q,
        "status_filter": status_filter,
        "payment_status_filter": payment_status_filter,
        "payment_method_filter": payment_method_filter,
        "status_choices": Order.STATUS_CHOICES,
        "payment_status_choices": Order.PAYMENT_STATUS_CHOICES,
        "payment_method_choices": Order.PAYMENT_METHOD_CHOICES,
    }
    return render(request, "custom_admin/orders/list.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, ROLE_SUPPORT_MANAGER])
def admin_order_detail_view(request, pk):
    """
    Complete order details view with snapshots, line items, and transition controls.
    """
    order = get_object_or_404(
        Order.objects.select_related("user").prefetch_related("items"),
        pk=pk,
    )
    status_form = AdminOrderStatusForm(order=order)
    payment_form = AdminOrderPaymentStatusForm(order=order)

    # Calculate allowed next transitions for UI display
    allowed_next_statuses = AdminOrderStatusForm.ALLOWED_TRANSITIONS.get(order.status, [])

    context = {
        "order": order,
        "status_form": status_form,
        "payment_form": payment_form,
        "allowed_next_statuses": allowed_next_statuses,
    }
    return render(request, "custom_admin/orders/detail.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, ROLE_SUPPORT_MANAGER])
@require_POST
def admin_order_update_status_view(request, pk):
    """
    Updates order fulfillment status with state machine validation.
    """
    order = get_object_or_404(Order, pk=pk)
    form = AdminOrderStatusForm(request.POST, order=order)

    if form.is_valid():
        new_status = form.cleaned_data["status"]
        if new_status != order.status:
            if new_status == Order.STATUS_CANCELLED:
                success, msg, _ = cancel_order(
                    order=order,
                    user=request.user,
                    cancelled_by="staff",
                    reason="Cancelled by store administrator via management dashboard",
                )
                if success:
                    messages.success(request, f"Order #{order.order_number} status updated to 'Cancelled'. Inventory has been restored.")
                else:
                    messages.error(request, f"Cancellation failed: {msg}")
            else:
                order.status = new_status
                order.save(update_fields=["status", "updated_at"])
                messages.success(request, f"Order status updated to '{order.get_status_display()}'.")
        else:
            messages.info(request, "No status change was requested.")
    else:
        error_msg = form.errors["status"][0] if "status" in form.errors else "Invalid transition."
        messages.error(request, error_msg)

    return redirect("custom_admin:order_detail", pk=order.pk)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, ROLE_SUPPORT_MANAGER])
@require_POST
def admin_order_update_payment_view(request, pk):
    """
    Updates order payment status.
    Guarantees online payments cannot be marked Paid without a verified gateway payment ID.
    """
    order = get_object_or_404(Order, pk=pk)
    form = AdminOrderPaymentStatusForm(request.POST, order=order)

    if form.is_valid():
        new_payment_status = form.cleaned_data["payment_status"]
        if new_payment_status != order.payment_status:
            order.payment_status = new_payment_status
            if new_payment_status == Order.PAYMENT_STATUS_PAID and not order.paid_at:
                order.paid_at = timezone.now()
            order.save(update_fields=["payment_status", "paid_at", "updated_at"])
            messages.success(request, f"Payment status updated to '{order.get_payment_status_display()}'.")
        else:
            messages.info(request, "No payment status change was requested.")
    else:
        error_msg = (
            form.errors["payment_status"][0]
            if "payment_status" in form.errors
            else "Invalid payment status update."
        )
        messages.error(request, error_msg)

    return redirect("custom_admin:order_detail", pk=order.pk)


# ==============================================================================
# PHASE 7: SELLER LEDGER, COD RECONCILIATION & PAYOUT MANAGEMENT VIEWS
# ==============================================================================

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_ledger_list_view(request):
    """
    Platform administrator master financial ledger overview.
    Displays all immutable credit and debit entries across all sellers and platform fulfillment.
    Provides multi-dimensional filtering, searching, and financial aggregates.
    """
    qs = SellerLedgerEntry.objects.select_related(
        "seller", "order", "seller_order", "payout_batch", "refund_record"
    ).order_by("-created_at", "-id")

    # Filters
    seller_param = request.GET.get("seller", "").strip()
    category_param = request.GET.get("category", "").strip()
    status_param = request.GET.get("status", "").strip()
    type_param = request.GET.get("type", "").strip()
    q_param = request.GET.get("q", "").strip()

    if seller_param == "platform":
        qs = qs.filter(is_platform_fulfilled=True)
    elif seller_param.isdigit():
        qs = qs.filter(seller_id=int(seller_param), is_platform_fulfilled=False)

    if category_param:
        qs = qs.filter(category=category_param)
    if status_param:
        qs = qs.filter(status=status_param)
    if type_param in (SellerLedgerEntry.TYPE_CREDIT, SellerLedgerEntry.TYPE_DEBIT):
        qs = qs.filter(entry_type=type_param)
    if q_param:
        qs = qs.filter(
            Q(idempotency_key__icontains=q_param)
            | Q(source_reference__icontains=q_param)
            | Q(description__icontains=q_param)
            | Q(seller__store_name__icontains=q_param)
        )

    # Master aggregates across the filtered set
    posted_qs = qs.filter(status=SellerLedgerEntry.STATUS_POSTED)
    total_credits = (
        posted_qs.filter(entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    total_debits = (
        posted_qs.filter(entry_type=SellerLedgerEntry.TYPE_DEBIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    net_payable = (total_credits - total_debits).quantize(Decimal("0.01"))

    gmv_total = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_ORDER_SALE).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    comm_total = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_COMMISSION).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    refund_total = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_RETURN_REFUND).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    payout_total = (
        posted_qs.filter(category=SellerLedgerEntry.CATEGORY_PAYOUT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )

    pending_cod_count = qs.filter(status=SellerLedgerEntry.STATUS_PENDING_COD).count()
    pending_cod_amt = (
        qs.filter(status=SellerLedgerEntry.STATUS_PENDING_COD, entry_type=SellerLedgerEntry.TYPE_CREDIT).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )

    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    all_sellers = Seller.objects.filter(is_active=True).order_by("store_name")

    context = {
        "page_obj": page_obj,
        "sellers": all_sellers,
        "selected_seller": seller_param,
        "selected_category": category_param,
        "selected_status": status_param,
        "selected_type": type_param,
        "search_query": q_param,
        "category_choices": SellerLedgerEntry.CATEGORY_CHOICES,
        "status_choices": SellerLedgerEntry.STATUS_CHOICES,
        "entry_type_choices": SellerLedgerEntry.ENTRY_TYPE_CHOICES,
        "total_credits": total_credits,
        "total_debits": total_debits,
        "net_payable": net_payable,
        "gmv_total": gmv_total,
        "comm_total": comm_total,
        "refund_total": refund_total,
        "payout_total": payout_total,
        "pending_cod_count": pending_cod_count,
        "pending_cod_amt": pending_cod_amt,
        "total_count": paginator.count,
    }
    return render(request, "custom_admin/ledger/index.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_cod_reconcile_view(request):
    """
    Courier Cash-on-Delivery (COD) reconciliation console.
    Lists delivered COD packages awaiting cash remittance verification.
    Provides reconciliation action to unlock pending ledger entries for seller payout.
    """
    if request.method == "POST":
        package_id = request.POST.get("seller_order_id")
        pkg = get_object_or_404(
            SellerOrder.objects.select_related("order", "seller"),
            pk=package_id,
            order__payment_method=Order.PAYMENT_METHOD_COD,
        )
        form = AdminCODReconcileForm(request.POST)
        if form.is_valid():
            try:
                rec_record = reconcile_cod_remittance(
                    seller_order=pkg,
                    remittance_reference=form.cleaned_data["remittance_reference"],
                    collected_amount=form.cleaned_data["collected_amount"],
                    courier_name=form.cleaned_data.get("courier_name", ""),
                    remitted_date=form.cleaned_data.get("remitted_date"),
                    actor=request.user,
                    notes=form.cleaned_data.get("notes", ""),
                )
                messages.success(
                    request,
                    f"COD remittance successfully reconciled for package #{pkg.sub_order_number} (Ref: {rec_record.remittance_reference}). Funds unlocked for seller payout!"
                )
                return redirect("custom_admin:cod_reconcile")
            except LedgerServiceError as err:
                messages.error(request, f"Reconciliation failed: {err}")
            except Exception as exc:
                messages.error(request, f"Unexpected error during reconciliation: {exc}")
        else:
            messages.error(request, "Please correct the form errors.")
    else:
        form = AdminCODReconcileForm()

    unreconciled_packages = (
        SellerOrder.objects.filter(
            order__payment_method=Order.PAYMENT_METHOD_COD,
            status=SellerOrder.STATUS_DELIVERED,
            cod_reconciliation__isnull=True,
        )
        .select_related("order", "seller")
        .order_by("-delivered_at", "-created_at")
    )

    recent_reconciliations = (
        CODReceiptRecord.objects.select_related("seller_order", "order", "reconciled_by")
        .order_by("-reconciled_at")[:15]
    )

    context = {
        "form": form,
        "unreconciled_packages": unreconciled_packages,
        "recent_reconciliations": recent_reconciliations,
        "unreconciled_count": unreconciled_packages.count(),
        "unreconciled_total": sum(pkg.subtotal for pkg in unreconciled_packages),
    }
    return render(request, "custom_admin/ledger/cod_reconcile.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_payout_batch_list_view(request):
    """
    Platform administrator payout batch dashboard.
    Lists all mock payout disbursement batches and their lifecycle status.
    """
    qs = PayoutBatch.objects.select_related("seller", "processed_by").order_by("-created_at")

    status_filter = request.GET.get("status", "").strip()
    if status_filter:
        qs = qs.filter(status=status_filter)

    draft_count = PayoutBatch.objects.filter(status=PayoutBatch.STATUS_DRAFT).count()
    completed_count = PayoutBatch.objects.filter(status=PayoutBatch.STATUS_COMPLETED).count()
    failed_count = PayoutBatch.objects.filter(status=PayoutBatch.STATUS_FAILED).count()
    reversed_count = PayoutBatch.objects.filter(status=PayoutBatch.STATUS_REVERSED).count()
    total_disbursed = (
        PayoutBatch.objects.filter(status=PayoutBatch.STATUS_COMPLETED).aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )

    paginator = Paginator(qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "selected_status": status_filter,
        "status_choices": PayoutBatch.STATUS_CHOICES,
        "draft_count": draft_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "reversed_count": reversed_count,
        "total_disbursed": total_disbursed,
    }
    return render(request, "custom_admin/ledger/payout_list.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_payout_batch_create_view(request):
    """
    Payout batch preparation workspace.
    Computes real-time payout eligibility for each seller and allows creating a locked DRAFT batch.
    """
    all_sellers = Seller.objects.filter(is_active=True).order_by("store_name")

    seller_eligibility_cards = []
    for s in all_sellers:
        elig = calculate_seller_payout_eligibility(seller=s)
        seller_eligibility_cards.append({
            "seller": s,
            "eligibility": elig,
        })

    # Also compute platform fulfillment store eligibility
    platform_elig = calculate_seller_payout_eligibility(is_platform_fulfilled=True)

    if request.method == "POST":
        target_seller = request.POST.get("seller_id", "").strip()
        notes = request.POST.get("notes", "").strip()

        try:
            if target_seller == "platform":
                batch = create_payout_batch(
                    is_platform_fulfilled=True,
                    actor=request.user,
                    notes=notes,
                )
            elif target_seller.isdigit():
                sel = get_object_or_404(Seller, pk=int(target_seller))
                batch = create_payout_batch(
                    seller=sel,
                    actor=request.user,
                    notes=notes,
                    enforce_bank_verification=True,
                )
            else:
                messages.error(request, "Please select a valid merchant or platform store account.")
                return redirect("custom_admin:payout_create")

            messages.success(
                request,
                f"Payout batch #{batch.batch_reference} prepared in DRAFT status with {batch.eligible_entries_count} locked entries (Amount: ₹{batch.amount})."
            )
            return redirect("custom_admin:payout_detail", batch_reference=batch.batch_reference)

        except LedgerServiceError as err:
            messages.error(request, f"Unable to create payout batch: {err}")
        except Exception as exc:
            messages.error(request, f"Unexpected error: {exc}")

    context = {
        "seller_cards": seller_eligibility_cards,
        "platform_elig": platform_elig,
    }
    return render(request, "custom_admin/ledger/payout_create.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_payout_batch_detail_view(request, batch_reference):
    """
    Detailed audit view of a specific PayoutBatch.
    Shows recipient details, covered ledger entries, timeline, and actions to execute, hold, release, reject, or reverse.
    """
    batch = get_object_or_404(
        PayoutBatch.objects.select_related("seller", "processed_by", "held_by", "released_by", "approved_by", "rejected_by"),
        batch_reference=batch_reference,
    )
    covered_entries = batch.ledger_entries.select_related("order", "seller_order").order_by("-created_at")
    audit_logs = batch.audit_logs.select_related("actor").order_by("-created_at")

    process_form = AdminPayoutProcessForm()
    reverse_form = AdminPayoutReverseForm()
    hold_form = AdminPayoutHoldForm()
    reject_form = AdminPayoutRejectForm()
    payout_system_status = get_payout_system_status()

    context = {
        "batch": batch,
        "covered_entries": covered_entries,
        "audit_logs": audit_logs,
        "process_form": process_form,
        "reverse_form": reverse_form,
        "hold_form": hold_form,
        "reject_form": reject_form,
        "payout_system_status": payout_system_status,
    }
    return render(request, "custom_admin/ledger/payout_detail.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_POST
def admin_payout_batch_process_view(request, batch_reference):
    """
    Executes mock disbursement settlement on a DRAFT or PROCESSING payout batch.
    """
    batch = get_object_or_404(PayoutBatch, batch_reference=batch_reference)
    form = AdminPayoutProcessForm(request.POST)

    simulate_failure = False
    failure_reason = ""
    if form.is_valid():
        simulate_failure = form.cleaned_data.get("simulate_failure", False)
        failure_reason = form.cleaned_data.get("failure_reason", "")

    try:
        success, msg, updated_batch = process_mock_payout_batch(
            payout_batch=batch,
            actor=request.user,
            simulate_failure=simulate_failure,
            failure_reason=failure_reason,
        )
        if success:
            messages.success(request, msg)
        else:
            messages.warning(request, msg)
    except LedgerServiceError as err:
        messages.error(request, f"Payout settlement error: {err}")
    except Exception as exc:
        messages.error(request, f"Unexpected error during settlement: {exc}")

    return redirect("custom_admin:payout_detail", batch_reference=batch.batch_reference)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_POST
def admin_payout_batch_reverse_view(request, batch_reference):
    """
    Reverses an already COMPLETED payout batch with auditable justification.
    """
    batch = get_object_or_404(PayoutBatch, batch_reference=batch_reference)
    form = AdminPayoutReverseForm(request.POST)

    if form.is_valid():
        reason = form.cleaned_data["reason"]
        try:
            success, msg, updated_batch = reverse_payout_batch(
                payout_batch=batch,
                actor=request.user,
                reason=reason,
            )
            messages.success(request, msg)
        except LedgerServiceError as err:
            messages.error(request, f"Reversal failed: {err}")
        except Exception as exc:
            messages.error(request, f"Unexpected error during reversal: {exc}")
    else:
        messages.error(request, "A mandatory explanation reason is required to reverse a payout batch.")

    return redirect("custom_admin:payout_detail", batch_reference=batch.batch_reference)


# ==============================================================================
# SELLER PAYOUT MANAGEMENT DASHBOARD & CONTROLS
# ==============================================================================

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_seller_payout_management_view(request):
    """
    Dedicated Custom Admin Seller Payout Management Dashboard.
    Provides monthly cycle tracking, 6 summary status cards, multi-facet filtering,
    per-seller eligibility breakdown, and server-side authorized hold/approval controls.
    """
    now = timezone.now()
    default_cycle, default_cycle_payout_date, default_prev_start, default_prev_end = get_cycle_bounds(as_of_date=now)

    selected_cycle = request.GET.get("cycle", "").strip() or default_cycle
    canonical_cycle, cycle_payout_date, prev_month_start, prev_month_end = get_cycle_bounds(
        cycle_str=selected_cycle, as_of_date=now
    )

    selected_seller_id = request.GET.get("seller", "").strip()
    store_query = request.GET.get("store", "").strip()
    selected_status = request.GET.get("status", "").strip()
    selected_hold = request.GET.get("hold", "").strip()

    # Base active sellers filter
    sellers_qs = Seller.objects.filter(is_active=True).order_by("store_name")
    if selected_seller_id and selected_seller_id.isdigit():
        sellers_qs = sellers_qs.filter(id=int(selected_seller_id))
    if store_query:
        sellers_qs = sellers_qs.filter(
            Q(store_name__icontains=store_query) | Q(business_name__icontains=store_query)
        )

    # All sellers for filter dropdown
    all_sellers_dropdown = Seller.objects.filter(is_active=True).order_by("store_name").only("id", "store_name")

    # Available recent cycles for the dropdown (current + 5 previous months)
    cycle_options = []
    curr_dt = now
    for _ in range(6):
        c_code, c_payout, p_start, p_end = get_cycle_bounds(as_of_date=curr_dt)
        label = f"{c_payout.strftime('%B %Y')} Cycle (5th {c_payout.strftime('%b %Y')} - Sales for {p_start.strftime('%b %Y')})"
        cycle_options.append({"code": c_code, "label": label})
        # Step back one month
        first_of_curr = curr_dt.replace(day=1)
        curr_dt = first_of_curr - timedelta(days=1)

    # Global summary metrics for this cycle across all active sellers
    total_seller_payable = Decimal("0.00")
    eligible_cycle_count = 0
    on_hold_amount = Decimal("0.00")
    on_hold_count = 0
    pending_approval_amount = Decimal("0.00")
    pending_approval_count = 0
    paid_successfully_amount = Decimal("0.00")
    paid_successfully_count = 0
    failed_payments_amount = Decimal("0.00")
    failed_payments_count = 0

    # Pre-fetch existing batches for this cycle
    cycle_batches = {
        b.seller_id: b
        for b in PayoutBatch.objects.filter(
            payout_cycle=canonical_cycle,
            seller__isnull=False,
        ).select_related("seller", "processed_by", "approved_by", "held_by")
    }

    seller_rows = []

    # Process all active sellers to compute cycle metrics and populate table
    all_active_sellers = Seller.objects.filter(is_active=True).order_by("store_name")
    for s in all_active_sellers:
        elig = calculate_seller_payout_eligibility(
            seller=s, cycle_month=canonical_cycle, as_of_date=cycle_payout_date
        )
        batch = cycle_batches.get(s.id)

        # Batch or computed values
        if batch:
            final_payable = batch.amount
            is_held = (batch.status == PayoutBatch.STATUS_ON_HOLD)
            is_approved = bool(batch.approved_at)
            is_completed = (batch.status == PayoutBatch.STATUS_COMPLETED)
            is_failed = (batch.status == PayoutBatch.STATUS_FAILED)
            is_draft = (batch.status in [PayoutBatch.STATUS_DRAFT, PayoutBatch.STATUS_PROCESSING])
        else:
            final_payable = max(Decimal("0.00"), elig["net_eligible_balance"])
            is_held = False
            is_approved = False
            is_completed = False
            is_failed = False
            is_draft = False

        # Summary aggregates
        if is_completed:
            paid_successfully_amount += final_payable
            paid_successfully_count += 1
        elif is_failed:
            failed_payments_amount += final_payable
            failed_payments_count += 1
        elif is_held:
            on_hold_amount += final_payable
            on_hold_count += 1
            total_seller_payable += final_payable
        elif is_approved or is_draft:
            pending_approval_amount += final_payable
            pending_approval_count += 1
            total_seller_payable += final_payable
        elif elig["is_above_threshold"]:
            total_seller_payable += final_payable

        if elig["is_above_threshold"] and s.is_bank_payout_eligible:
            eligible_cycle_count += 1

        # Also add unbatched held order amount to on_hold_amount if any
        if not batch and elig["held_balance"] > Decimal("0.00"):
            on_hold_amount += elig["held_balance"]

        # Now determine display status label & badge
        if batch:
            if batch.status == PayoutBatch.STATUS_ON_HOLD:
                status_label = "On Hold"
                status_badge = "bg-warning text-dark"
                status_code = "on_hold"
            elif batch.status == PayoutBatch.STATUS_COMPLETED:
                status_label = "Paid (Simulated)"
                status_badge = "bg-success"
                status_code = "completed"
            elif batch.status == PayoutBatch.STATUS_FAILED:
                status_label = "Failed / Rejected"
                status_badge = "bg-danger"
                status_code = "failed"
            elif batch.status == PayoutBatch.STATUS_REVERSED:
                status_label = "Reversed"
                status_badge = "bg-purple text-white"
                status_code = "reversed"
            elif batch.approved_at:
                status_label = "Approved (Ready)"
                status_badge = "bg-info text-dark"
                status_code = "approved"
            else:
                status_label = "Pending Approval"
                status_badge = "bg-secondary"
                status_code = "draft"
        else:
            if elig["net_eligible_balance"] <= Decimal("0.00"):
                status_label = "No Balance"
                status_badge = "bg-light text-muted border"
                status_code = "no_balance"
            elif not s.is_bank_payout_eligible:
                status_label = "Bank Unverified"
                status_badge = "bg-warning-subtle text-warning-emphasis border"
                status_code = "unverified"
            else:
                status_label = "Eligible"
                status_badge = "bg-primary-subtle text-primary border"
                status_code = "eligible"

        row = {
            "seller": s,
            "eligibility": elig,
            "batch": batch,
            "sales_volume": elig["eligible_sales"],
            "commission": elig["eligible_commission"],
            "refunds_and_adjustments": elig["refunds_and_adjustments"],
            "amount_on_hold": batch.amount if (batch and batch.status == PayoutBatch.STATUS_ON_HOLD) else elig["held_balance"],
            "final_payable": final_payable,
            "bank_verification_status": s.bank_verification_status,
            "bank_status_label": s.get_bank_verification_status_display(),
            "masked_account": s.get_masked_account_number(),
            "is_bank_verified": s.is_bank_payout_eligible,
            "status_label": status_label,
            "status_badge": status_badge,
            "status_code": status_code,
            "is_held": is_held,
            "can_generate_batch": (not batch and elig["is_above_threshold"] and s.is_bank_payout_eligible),
            "can_approve": (batch and batch.status in [PayoutBatch.STATUS_DRAFT, PayoutBatch.STATUS_PROCESSING] and not batch.approved_at and s.is_bank_payout_eligible),
            "can_hold": (batch and batch.status != PayoutBatch.STATUS_ON_HOLD and batch.status not in [PayoutBatch.STATUS_COMPLETED, PayoutBatch.STATUS_REVERSED]),
            "can_release": (batch and batch.status == PayoutBatch.STATUS_ON_HOLD),
            "can_reject": (batch and batch.status in [PayoutBatch.STATUS_DRAFT, PayoutBatch.STATUS_PROCESSING, PayoutBatch.STATUS_ON_HOLD]),
            "can_process": (batch and batch.status in [PayoutBatch.STATUS_DRAFT, PayoutBatch.STATUS_PROCESSING] and batch.approved_at and not is_held),
        }

        # Apply filtering for the table display
        if s in sellers_qs:
            # Status filter
            if selected_status:
                if selected_status == "eligible" and status_code != "eligible":
                    continue
                elif selected_status == "draft" and status_code != "draft":
                    continue
                elif selected_status == "approved" and status_code != "approved":
                    continue
                elif selected_status == "on_hold" and status_code != "on_hold":
                    continue
                elif selected_status == "completed" and status_code != "completed":
                    continue
                elif selected_status == "failed" and status_code != "failed":
                    continue
                elif selected_status == "unverified" and status_code != "unverified":
                    continue
                elif selected_status == "no_balance" and status_code != "no_balance":
                    continue

            # Hold filter
            if selected_hold == "held" and not (is_held or row["amount_on_hold"] > Decimal("0.00")):
                continue
            elif selected_hold == "not_held" and (is_held or row["amount_on_hold"] > Decimal("0.00")):
                continue

            seller_rows.append(row)

    hold_form = AdminPayoutHoldForm()
    reject_form = AdminPayoutRejectForm()
    payout_system_status = get_payout_system_status()

    context = {
        "canonical_cycle": canonical_cycle,
        "selected_cycle": selected_cycle,
        "cycle_payout_date": cycle_payout_date,
        "prev_month_start": prev_month_start,
        "prev_month_end": prev_month_end,
        "cycle_options": cycle_options,
        "all_sellers_dropdown": all_sellers_dropdown,
        "selected_seller_id": selected_seller_id,
        "store_query": store_query,
        "selected_status": selected_status,
        "selected_hold": selected_hold,
        # Summary cards
        "total_seller_payable": total_seller_payable,
        "eligible_cycle_count": eligible_cycle_count,
        "on_hold_amount": on_hold_amount,
        "on_hold_count": on_hold_count,
        "pending_approval_amount": pending_approval_amount,
        "pending_approval_count": pending_approval_count,
        "paid_successfully_amount": paid_successfully_amount,
        "paid_successfully_count": paid_successfully_count,
        "failed_payments_amount": failed_payments_amount,
        "failed_payments_count": failed_payments_count,
        # Seller rows
        "seller_rows": seller_rows,
        "hold_form": hold_form,
        "reject_form": reject_form,
        "payout_system_status": payout_system_status,
    }
    return render(request, "custom_admin/ledger/payout_management.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_seller_payout_review_view(request, seller_id):
    """
    Dedicated drill-down review console for an individual seller's monthly payout cycle.
    Provides line-item financial reconciliation, cooling period audits,
    unresolved return/COD status, and masked bank verification overview.
    """
    seller = get_object_or_404(Seller, pk=seller_id)
    now = timezone.now()
    cycle_param = request.GET.get("cycle", "").strip()
    canonical_cycle, cycle_payout_date, prev_month_start, prev_month_end = get_cycle_bounds(
        cycle_str=cycle_param or None,
        as_of_date=now,
    )

    eligibility = calculate_seller_payout_eligibility(
        seller=seller,
        cycle_month=canonical_cycle,
        as_of_date=cycle_payout_date,
    )

    active_batch = PayoutBatch.objects.filter(
        seller=seller,
        payout_cycle=canonical_cycle,
    ).exclude(status__in=[PayoutBatch.STATUS_FAILED, PayoutBatch.STATUS_REVERSED]).first()

    audit_logs = []
    if active_batch:
        audit_logs = active_batch.audit_logs.select_related("actor").order_by("-created_at")

    hold_form = AdminPayoutHoldForm()
    reject_form = AdminPayoutRejectForm()
    payout_system_status = get_payout_system_status()

    context = {
        "seller": seller,
        "canonical_cycle": canonical_cycle,
        "cycle_payout_date": cycle_payout_date,
        "prev_month_start": prev_month_start,
        "prev_month_end": prev_month_end,
        "eligibility": eligibility,
        "active_batch": active_batch,
        "audit_logs": audit_logs,
        "hold_form": hold_form,
        "reject_form": reject_form,
        "payout_system_status": payout_system_status,
    }
    return render(request, "custom_admin/ledger/seller_payout_review.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_POST
def admin_generate_cycle_payout_batch_view(request):
    """
    Idempotently creates a locked PayoutBatch for a specific seller or all eligible sellers
    for the selected monthly cycle. Protected by database transactions and unique constraints.
    """
    target = request.POST.get("seller_id", "").strip()
    cycle = request.POST.get("cycle", "").strip()
    notes = request.POST.get("notes", "").strip()
    next_url = request.POST.get("next") or reverse("custom_admin:seller_payout_management")

    if not cycle:
        messages.error(request, "A valid payout cycle (YYYY-MM) is required.")
        return redirect(next_url)

    if target == "all":
        # Bulk generation for all eligible active sellers with verified bank accounts
        eligible_sellers = Seller.objects.filter(is_active=True, bank_verification_status="verified")
        created_count = 0
        skipped_count = 0
        errors = []

        for s in eligible_sellers:
            try:
                elig = calculate_seller_payout_eligibility(seller=s, cycle_month=cycle)
                if not elig["is_above_threshold"]:
                    continue

                batch = create_payout_batch(
                    seller=s,
                    payout_cycle=cycle,
                    enforce_bank_verification=True,
                    actor=request.user,
                    notes=notes or f"Automated cycle {cycle} batch generation",
                )
                created_count += 1
            except LedgerServiceError as err:
                skipped_count += 1
                errors.append(f"{s.store_name}: {err}")
            except Exception as exc:
                skipped_count += 1
                errors.append(f"{s.store_name}: {exc}")

        if created_count > 0:
            messages.success(request, f"Successfully created {created_count} payout batch(es) for cycle {cycle}.")
        if skipped_count > 0:
            messages.warning(request, f"{skipped_count} seller(s) skipped. Details: {'; '.join(errors[:3])}")
        if created_count == 0 and skipped_count == 0:
            messages.info(request, f"No new eligible sellers found for cycle {cycle}.")

        return redirect(next_url)

    elif target.isdigit():
        seller = get_object_or_404(Seller, pk=int(target))
        try:
            batch = create_payout_batch(
                seller=seller,
                payout_cycle=cycle,
                enforce_bank_verification=True,
                actor=request.user,
                notes=notes,
            )
            messages.success(
                request,
                f"Payout batch #{batch.batch_reference} created for {seller.store_name} (Amount: ₹{batch.amount}) for cycle {cycle}."
            )
        except LedgerServiceError as err:
            messages.error(request, f"Cannot prepare payout batch: {err}")
        except Exception as exc:
            messages.error(request, f"Unexpected error: {exc}")

        return redirect(next_url)
    else:
        messages.error(request, "Invalid seller target.")
        return redirect(next_url)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_POST
def admin_payout_batch_approve_view(request, batch_reference):
    """
    Server-side authorized approval of a DRAFT payout batch.
    Revalidates bank details and hold status before approving.
    """
    batch = get_object_or_404(PayoutBatch, batch_reference=batch_reference)
    next_url = request.POST.get("next") or reverse("custom_admin:seller_payout_management")

    try:
        success, msg, updated_batch = approve_payout_batch(payout_batch=batch, actor=request.user)
        if success:
            messages.success(request, msg)
        else:
            messages.warning(request, msg)
    except LedgerServiceError as err:
        messages.error(request, f"Approval rejected: {err}")
    except Exception as exc:
        messages.error(request, f"Unexpected error: {exc}")

    return redirect(next_url)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_POST
def admin_payout_batch_hold_view(request, batch_reference):
    """
    Places a payout batch on hold with a mandatory auditable reason.
    Ensures held payments are isolated without blocking other eligible merchants.
    """
    batch = get_object_or_404(PayoutBatch, batch_reference=batch_reference)
    form = AdminPayoutHoldForm(request.POST)
    next_url = request.POST.get("next") or reverse("custom_admin:seller_payout_management")

    if form.is_valid():
        reason = form.cleaned_data["reason"]
        try:
            success, msg, updated_batch = hold_payout_batch(payout_batch=batch, actor=request.user, reason=reason)
            messages.warning(request, msg)
        except LedgerServiceError as err:
            messages.error(request, f"Cannot hold payout: {err}")
        except Exception as exc:
            messages.error(request, f"Unexpected error: {exc}")
    else:
        messages.error(request, "A mandatory explanation reason is required to hold a payout batch.")

    return redirect(next_url)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_POST
def admin_payout_batch_release_view(request, batch_reference):
    """
    Releases a held payout batch back to active processing after revalidating bank eligibility.
    """
    batch = get_object_or_404(PayoutBatch, batch_reference=batch_reference)
    notes = request.POST.get("notes", "").strip()
    next_url = request.POST.get("next") or reverse("custom_admin:seller_payout_management")

    try:
        success, msg, updated_batch = release_payout_batch(payout_batch=batch, actor=request.user, notes=notes)
        messages.success(request, msg)
    except LedgerServiceError as err:
        messages.error(request, f"Cannot release hold: {err}")
    except Exception as exc:
        messages.error(request, f"Unexpected error: {exc}")

    return redirect(next_url)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_POST
def admin_payout_batch_reject_view(request, batch_reference):
    """
    Rejects a payout batch with a mandatory auditable explanation.
    Unlinks covered ledger entries so they can be re-evaluated in a future cycle.
    """
    batch = get_object_or_404(PayoutBatch, batch_reference=batch_reference)
    form = AdminPayoutRejectForm(request.POST)
    next_url = request.POST.get("next") or reverse("custom_admin:seller_payout_management")

    if form.is_valid():
        reason = form.cleaned_data["reason"]
        try:
            success, msg, updated_batch = reject_payout_batch(payout_batch=batch, actor=request.user, reason=reason)
            messages.info(request, msg)
        except LedgerServiceError as err:
            messages.error(request, f"Cannot reject payout: {err}")
        except Exception as exc:
            messages.error(request, f"Unexpected error: {exc}")
    else:
        messages.error(request, "A mandatory rejection reason is required to reject a payout batch.")

    return redirect(next_url)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_seller_payout_history_view(request, seller_id):
    """
    Historical payout audit log and batch run ledger for a specific seller.
    """
    seller = get_object_or_404(Seller, pk=seller_id)
    batches = PayoutBatch.objects.filter(seller=seller).select_related(
        "processed_by", "approved_by", "held_by", "released_by", "rejected_by"
    ).order_by("-created_at")

    audit_logs = PayoutAuditLog.objects.filter(
        payout_batch__seller=seller
    ).select_related("payout_batch", "actor").order_by("-created_at")

    paginator = Paginator(batches, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "seller": seller,
        "page_obj": page_obj,
        "audit_logs": audit_logs[:30],
        "masked_account": seller.get_masked_account_number(),
    }
    return render(request, "custom_admin/ledger/seller_payout_history.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_manual_adjustment_view(request):
    """
    Administrative manual financial adjustment entry console.
    Permits authorized staff to record audited manual credits or debits.
    """
    if request.method == "POST":
        form = AdminManualAdjustmentForm(request.POST)
        if form.is_valid():
            seller = form.cleaned_data.get("seller")
            is_platform = seller is None
            entry_type = form.cleaned_data["entry_type"]
            amount = form.cleaned_data["amount"]
            reason = form.cleaned_data["reason"]

            try:
                entry = create_manual_adjustment(
                    entry_type=entry_type,
                    amount=amount,
                    reason=reason,
                    seller=seller,
                    is_platform_fulfilled=is_platform,
                    actor=request.user,
                )
                beneficiary = seller.store_name if seller else "Platform Store"
                sign = "+" if entry_type == SellerLedgerEntry.TYPE_CREDIT else "-"
                messages.success(
                    request,
                    f"Manual {entry_type.upper()} adjustment of {sign}₹{amount} posted successfully for {beneficiary}."
                )
                return redirect("custom_admin:ledger_list")
            except LedgerServiceError as err:
                messages.error(request, f"Adjustment failed: {err}")
            except Exception as exc:
                messages.error(request, f"Unexpected error: {exc}")
    else:
        form = AdminManualAdjustmentForm()

    context = {
        "form": form,
    }
    return render(request, "custom_admin/ledger/manual_adjustment.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_GET
def admin_export_ledger_csv_view(request):
    """
    Generates a master platform financial ledger CSV export.
    Sanitizes values against CSV Formula Injection (CWE-1236).
    """
    qs = SellerLedgerEntry.objects.select_related("seller", "order", "seller_order", "payout_batch").order_by("-created_at")

    date_tag = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"fitmeezo_master_ledger_{date_tag}.csv"

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow([
        "Idempotency Key",
        "Timestamp",
        "Merchant",
        "Fulfillment Source",
        "Entry Type",
        "Category",
        "Amount (INR)",
        "Signed Amount (INR)",
        "Status",
        "Source Type",
        "Source Reference",
        "Order Number",
        "Sub-Order Number",
        "Payout Batch",
        "Description",
    ])

    for entry in qs:
        merchant_name = entry.seller.store_name if entry.seller else "Platform Store"
        source = "Platform Warehouse" if entry.is_platform_fulfilled else "Third-Party Merchant"
        order_num = entry.order.order_number if entry.order else ""
        sub_order_num = entry.seller_order.sub_order_number if entry.seller_order else ""
        batch_ref = entry.payout_batch.batch_reference if entry.payout_batch else "Unbatched"

        writer.writerow([
            sanitize_csv_cell(entry.idempotency_key),
            sanitize_csv_cell(entry.created_at.strftime("%Y-%m-%d %H:%M:%S")),
            sanitize_csv_cell(merchant_name),
            sanitize_csv_cell(source),
            sanitize_csv_cell(entry.entry_type.upper()),
            sanitize_csv_cell(entry.get_category_display()),
            sanitize_csv_cell(str(entry.amount)),
            sanitize_csv_cell(str(entry.signed_amount)),
            sanitize_csv_cell(entry.get_status_display()),
            sanitize_csv_cell(entry.source_type),
            sanitize_csv_cell(entry.source_reference),
            sanitize_csv_cell(order_num),
            sanitize_csv_cell(sub_order_num),
            sanitize_csv_cell(batch_ref),
            sanitize_csv_cell(entry.description),
        ])

    return response


# ==============================================================================
# PHASE 8: CUSTOM ADMIN INVOICE MANAGEMENT VIEWS
# ==============================================================================

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_GET
def admin_invoice_list_view(request):
    """
    Staff dashboard displaying issued customer and seller invoice records,
    filter criteria, and audit tracking.
    """
    qs = Invoice.objects.select_related("order", "seller_order", "seller").all().order_by("-issued_at")

    invoice_type = request.GET.get("type", "").strip()
    if invoice_type:
        qs = qs.filter(invoice_type=invoice_type)

    status = request.GET.get("status", "").strip()
    if status:
        qs = qs.filter(status=status)

    search_query = request.GET.get("q", "").strip()
    if search_query:
        qs = qs.filter(
            Q(invoice_number__icontains=search_query)
            | Q(order__order_number__icontains=search_query)
            | Q(recipient_name__icontains=search_query)
            | Q(issuer_name__icontains=search_query)
        )

    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Summary metrics
    total_invoices_count = Invoice.objects.count()
    customer_inv_count = Invoice.objects.filter(invoice_type=Invoice.TYPE_CUSTOMER_PARENT).count()
    seller_inv_count = Invoice.objects.filter(invoice_type=Invoice.TYPE_SELLER_PACKAGE).count()

    context = {
        "page_obj": page_obj,
        "selected_type": invoice_type,
        "selected_status": status,
        "search_query": search_query,
        "total_invoices_count": total_invoices_count,
        "customer_inv_count": customer_inv_count,
        "seller_inv_count": seller_inv_count,
        "invoice_types": Invoice.TYPE_CHOICES,
        "invoice_statuses": Invoice.STATUS_CHOICES,
    }
    return render(request, "custom_admin/invoices/index.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
@require_GET
def admin_invoice_pdf_view(request, pk):
    """
    Staff action to preview or download any issued invoice PDF.
    """
    invoice = get_object_or_404(
        Invoice.objects.select_related("order", "seller_order", "seller"),
        pk=pk,
    )

    from core.invoice_services import (
        generate_customer_invoice_pdf,
        generate_seller_package_invoice_pdf,
    )

    if invoice.invoice_type == Invoice.TYPE_SELLER_PACKAGE:
        pdf_bytes = generate_seller_package_invoice_pdf(invoice)
        filename = f"seller_invoice_{invoice.invoice_number}.pdf"
    else:
        pdf_bytes = generate_customer_invoice_pdf(invoice)
        filename = f"customer_invoice_{invoice.invoice_number}.pdf"

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return response


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SELLER_MANAGER, ROLE_FINANCE_MANAGER])
def admin_seller_bank_verification_view(request, seller_id):
    """
    Platform administrator review and verification of a seller's submitted bank details.
    GET: Renders dedicated inspection page showing seller credentials, bank info, and audit actions.
    POST: Processes manual approval or rejection (with required rejection reason).
    """
    seller = get_object_or_404(Seller, pk=seller_id)

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        rejection_reason = request.POST.get("rejection_reason", "").strip()

        if action == "approve":
            seller.bank_verification_status = "verified"
            seller.bank_rejection_reason = ""
            seller.bank_verified_at = timezone.now()
            seller.bank_verified_by = request.user
            seller.save(
                update_fields=[
                    "bank_verification_status",
                    "bank_rejection_reason",
                    "bank_verified_at",
                    "bank_verified_by",
                    "updated_at",
                ]
            )
            messages.success(
                request,
                f"Bank details for seller '{seller.store_name}' ({seller.get_masked_account_number()}) "
                f"successfully verified and approved by {request.user.username}."
            )
        elif action == "reject":
            if not rejection_reason:
                messages.error(request, "A mandatory rejection reason is required when rejecting bank details.")
                form = AdminBankVerificationForm(initial={"action": "reject"})
                context = {
                    "seller": seller,
                    "form": form,
                    "masked_account": seller.get_masked_account_number(),
                    "has_bank_details": bool(seller.bank_account_number and seller.bank_ifsc_code),
                    "is_verified": seller.bank_verification_status == "verified",
                    "is_pending": seller.bank_verification_status == "pending",
                    "is_rejected": seller.bank_verification_status == "rejected",
                    "rejection_error": "Please provide a specific rejection reason explaining what needs correction.",
                }
                return render(request, "custom_admin/sellers/bank_verify.html", context)
            else:
                seller.bank_verification_status = "rejected"
                seller.bank_rejection_reason = rejection_reason
                seller.bank_verified_at = None
                seller.bank_verified_by = request.user
                seller.save(
                    update_fields=[
                        "bank_verification_status",
                        "bank_rejection_reason",
                        "bank_verified_at",
                        "bank_verified_by",
                        "updated_at",
                    ]
                )
                messages.warning(
                    request,
                    f"Bank details for seller '{seller.store_name}' marked as REJECTED: {rejection_reason}"
                )
        else:
            messages.error(request, "Invalid bank verification action.")

        next_url = request.POST.get("next") or reverse("custom_admin:seller_bank_verify", kwargs={"seller_id": seller.id})
        return redirect(next_url)

    # GET request: render the dedicated bank verification review template
    form = AdminBankVerificationForm(
        initial={"action": "approve" if seller.bank_verification_status != "verified" else "reject"}
    )
    context = {
        "seller": seller,
        "form": form,
        "masked_account": seller.get_masked_account_number(),
        "has_bank_details": bool(seller.bank_account_number and seller.bank_ifsc_code),
        "is_verified": seller.bank_verification_status == "verified",
        "is_pending": seller.bank_verification_status == "pending",
        "is_rejected": seller.bank_verification_status == "rejected",
    }
    return render(request, "custom_admin/sellers/bank_verify.html", context)


# ==============================================================================
# RETURN & EXCHANGE MANAGEMENT VIEWS (PHASE 6 & CUSTOM ADMIN)
# ==============================================================================

@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
def admin_return_list_view(request):
    """
    Returns and exchange requests list in Custom Admin with filtering,
    KPI summary metrics, search, and pagination.
    """
    returns_qs = (
        ReturnRequest.objects.select_related("order", "seller_order", "seller", "customer")
        .prefetch_related("items__order_item")
        .order_by("-created_at")
    )

    # Search filter
    q = request.GET.get("q", "").strip()
    if q:
        returns_qs = returns_qs.filter(
            Q(request_number__icontains=q)
            | Q(order__order_number__icontains=q)
            | Q(seller_order__sub_order_number__icontains=q)
            | Q(customer__username__icontains=q)
            | Q(customer__email__icontains=q)
            | Q(customer__phone_number__icontains=q)
        )

    # Status filter
    status_filter = request.GET.get("status", "").strip()
    if status_filter:
        returns_qs = returns_qs.filter(status=status_filter)

    # Request type filter (return vs exchange)
    type_filter = request.GET.get("type", "").strip()
    if type_filter:
        returns_qs = returns_qs.filter(request_type=type_filter)

    # Seller filter
    seller_filter = request.GET.get("seller", "").strip()
    if seller_filter:
        returns_qs = returns_qs.filter(seller_id=seller_filter)

    # Summary KPI counts
    all_returns = ReturnRequest.objects.all()
    kpis = {
        "total": all_returns.count(),
        "pending": all_returns.filter(status=ReturnRequest.STATUS_REQUESTED).count(),
        "approved": all_returns.filter(status=ReturnRequest.STATUS_APPROVED).count(),
        "in_progress": all_returns.filter(
            status__in=[
                ReturnRequest.STATUS_PICKUP_PENDING,
                ReturnRequest.STATUS_ITEM_RECEIVED,
                ReturnRequest.STATUS_INSPECTION,
                ReturnRequest.STATUS_PROCESSING,
            ]
        ).count(),
        "completed": all_returns.filter(status=ReturnRequest.STATUS_COMPLETED).count(),
        "rejected": all_returns.filter(
            status__in=[ReturnRequest.STATUS_REJECTED, ReturnRequest.STATUS_CANCELLED]
        ).count(),
    }

    sellers = Seller.objects.filter(is_active=True).order_by("store_name")

    paginator = Paginator(returns_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "returns": page_obj,
        "page_obj": page_obj,
        "kpis": kpis,
        "status_choices": ReturnRequest.STATUS_CHOICES,
        "type_choices": ReturnRequest.TYPE_CHOICES,
        "selected_status": status_filter,
        "selected_type": type_filter,
        "selected_seller": seller_filter,
        "query": q,
        "sellers": sellers,
    }
    return render(request, "custom_admin/returns/return_list.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
def admin_return_detail_view(request, request_number):
    """
    Comprehensive administrative detail view for a return/exchange request:
    - Customer information, delivery date, package ID
    - Line-item breakdown with refund amount calculation
    - Uploaded photo evidence inspection
    - Quality inspection form
    - State transition controls and rejection modal
    - Safe refund processing trigger (no fake gateway success)
    - Full audit timeline trail
    """
    return_req = get_object_or_404(
        ReturnRequest.objects.select_related("order", "seller_order", "seller", "customer"),
        request_number=request_number,
    )
    items = return_req.items.select_related("order_item__product", "order_item__variant", "exchange_variant")
    audit_logs = return_req.audit_logs.select_related("actor").order_by("-created_at")
    refunds = return_req.refund_records.select_related("processed_by").order_by("-created_at")

    allowed_next_states = VALID_TRANSITIONS.get(return_req.status, [])

    context = {
        "return_request": return_req,
        "items": items,
        "audit_logs": audit_logs,
        "refunds": refunds,
        "allowed_next_states": allowed_next_states,
        "inspection_choices": ReturnRequestItem.INSPECTION_CHOICES,
    }
    return render(request, "custom_admin/returns/return_detail.html", context)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
@require_POST
def admin_return_transition_view(request, request_number):
    """
    Executes a validated lifecycle transition on the ReturnRequest:
    e.g. requested -> approved, approved -> pickup_pending, pickup_pending -> item_received,
    item_received -> inspection, inspection -> processing.
    Also handles item inspection results (passed/failed, restock true/false, inspection notes).
    """
    return_req = get_object_or_404(ReturnRequest, request_number=request_number)
    target_status = request.POST.get("target_status", "").strip()
    notes = request.POST.get("notes", "").strip()

    if not target_status:
        messages.error(request, "Target status is required.")
        return redirect("custom_admin:return_detail", request_number=return_req.request_number)

    # Collect inspection decisions if present
    inspection_decisions = {}
    for item in return_req.items.all():
        prefix = f"item_{item.id}_"
        status_val = request.POST.get(f"{prefix}status")
        restock_val = request.POST.get(f"{prefix}restock")
        notes_val = request.POST.get(f"{prefix}notes")
        if status_val or restock_val is not None or notes_val is not None:
            inspection_decisions[item.id] = {
                "status": status_val or item.inspection_status,
                "restock": bool(restock_val == "1" or restock_val == "on" or restock_val is True),
                "notes": notes_val or "",
            }

    try:
        updated_req = transition_return_status(
            return_request=return_req,
            new_status=target_status,
            actor=request.user,
            actor_role="platform_admin",
            notes=notes,
            inspection_decisions=inspection_decisions if inspection_decisions else None,
        )
        messages.success(
            request,
            f"Return Request #{updated_req.request_number} transitioned to '{updated_req.get_status_display()}'.",
        )
    except (ReturnServiceError, ReturnValidationError) as err:
        messages.error(request, f"Transition failed: {err}")
    except Exception as err:
        messages.error(request, f"Unexpected error during transition: {err}")

    return redirect("custom_admin:return_detail", request_number=return_req.request_number)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
@require_POST
def admin_return_reject_view(request, request_number):
    """
    Rejects a ReturnRequest. Rejection reason is mandatory under Indian E-commerce guidelines.
    """
    return_req = get_object_or_404(ReturnRequest, request_number=request_number)
    rejection_reason = request.POST.get("rejection_reason", "").strip()
    notes = request.POST.get("notes", "").strip()

    if not rejection_reason:
        messages.error(request, "A clear rejection reason is mandatory to reject a customer return.")
        return redirect("custom_admin:return_detail", request_number=return_req.request_number)

    try:
        updated_req = transition_return_status(
            return_request=return_req,
            new_status=ReturnRequest.STATUS_REJECTED,
            actor=request.user,
            actor_role="platform_admin",
            notes=notes,
            rejection_reason=rejection_reason,
        )
        messages.warning(
            request,
            f"Return Request #{updated_req.request_number} has been REJECTED. Reason logged and customer notified.",
        )
    except (ReturnServiceError, ReturnValidationError) as err:
        messages.error(request, f"Rejection failed: {err}")
    except Exception as err:
        messages.error(request, f"Unexpected error during rejection: {err}")

    return redirect("custom_admin:return_detail", request_number=return_req.request_number)


@admin_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
@require_POST
def admin_return_refund_view(request, request_number):
    """
    Executes a safe simulated refund voucher for the approved return request.
    Does NOT make live external payment gateway calls (FITMEEZO_MOCK_REFUNDS=True).
    Reverses commission, deducts net seller payable in ledger, restores inventory (if restock checked),
    and updates request status to COMPLETED.
    """
    return_req = get_object_or_404(ReturnRequest, request_number=request_number)
    amount_str = request.POST.get("approved_amount", "").strip()
    reason = request.POST.get("reason", "").strip()
    payment_method = request.POST.get("payment_method", "").strip()

    approved_amount = None
    if amount_str:
        try:
            approved_amount = Decimal(amount_str)
        except Exception:
            messages.error(request, "Invalid refund amount specified.")
            return redirect("custom_admin:return_detail", request_number=return_req.request_number)

    # If the return request is in 'inspection', advance to 'processing' first
    if return_req.status == ReturnRequest.STATUS_INSPECTION:
        try:
            transition_return_status(
                return_request=return_req,
                new_status=ReturnRequest.STATUS_PROCESSING,
                actor=request.user,
                actor_role="platform_admin",
                notes="Advanced to refund processing by administrator.",
            )
        except Exception as trans_err:
            messages.error(request, f"Could not advance status to processing: {trans_err}")
            return redirect("custom_admin:return_detail", request_number=return_req.request_number)

    try:
        refund_record = process_simulated_refund(
            return_request=return_req,
            actor=request.user,
            approved_amount=approved_amount,
            reason=reason or f"Approved return refund for #{return_req.request_number}",
            payment_method=payment_method,
        )
        messages.success(
            request,
            f"Refund Voucher #{refund_record.refund_reference} generated successfully for ₹{refund_record.approved_amount}. "
            f"Seller ledger and inventory updated. (Mock simulated mode active - zero live gateway debit).",
        )
    except (RefundError, ReturnServiceError) as ref_err:
        messages.error(request, f"Refund execution failed: {ref_err}")
    except Exception as exc:
        messages.error(request, f"Unexpected error during refund processing: {exc}")

    return redirect("custom_admin:return_detail", request_number=return_req.request_number)




