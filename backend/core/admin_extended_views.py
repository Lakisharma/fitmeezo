"""
Fitmeezo Custom Admin - Extended Management Controllers.
Implements:
- Sellers directory, approvals queue, detail & actions (approve, reject, suspend, reactivate, commission)
- Product approval queue & safe deletion safeguards
- Inventory stock dashboard & safe updates
- Customer directory, customer profile & status toggle
- Platform commissions analytics
- Reports dashboard & safe CSV exports
- Admin staff accounts & role-based access management
- Audit log viewer & System configuration overview
- Global admin search
"""

import csv
from datetime import datetime, timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .admin_forms import (
    AdminInventoryUpdateForm,
    AdminProductForm,
    AdminReportFilterForm,
    AdminSellerActionForm,
    AdminSellerCommissionForm,
    AdminStaffUserForm,
    AdminUserRoleForm,
)
from .admin_permissions import (
    ALL_ROLES,
    ROLE_FINANCE_MANAGER,
    ROLE_PRODUCT_MANAGER,
    ROLE_SELLER_MANAGER,
    ROLE_SUPER_ADMIN,
    ROLE_SUPPORT_MANAGER,
    admin_role_required,
    get_user_role,
    log_admin_action,
    set_user_role,
)
from .models import (
    Address,
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    PayoutAuditLog,
    PayoutBatch,
    Product,
    ProductVariant,
    ReturnAuditLog,
    ReturnRequest,
    Seller,
    SellerLedgerEntry,
    SellerOrder,
)
from .seller_services import sanitize_csv_cell

User = get_user_model()


# ==============================================================================
# 1. SELLER MANAGEMENT
# ==============================================================================

@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SELLER_MANAGER])
def admin_seller_list_view(request):
    """
    List, search, filter and paginate marketplace sellers.
    """
    status_filter = request.GET.get("status", "").strip().lower()
    bank_filter = request.GET.get("bank", "").strip().lower()
    query = request.GET.get("q", "").strip()

    sellers_qs = (
        Seller.objects.select_related("user")
        .annotate(
            total_products=Count("products", distinct=True),
            total_orders=Count("seller_orders", distinct=True),
        )
        .order_by("-created_at")
    )

    if status_filter in dict(Seller.STATUS_CHOICES):
        sellers_qs = sellers_qs.filter(status=status_filter)

    if bank_filter in dict(Seller.BANK_STATUS_CHOICES):
        sellers_qs = sellers_qs.filter(bank_verification_status=bank_filter)

    if query:
        sellers_qs = sellers_qs.filter(
            Q(store_name__icontains=query)
            | Q(legal_business_name__icontains=query)
            | Q(support_email__icontains=query)
            | Q(support_phone__icontains=query)
            | Q(city__icontains=query)
            | Q(gstin__icontains=query)
            | Q(pan_number__icontains=query)
            | Q(user__username__icontains=query)
            | Q(user__email__icontains=query)
        )

    # Metrics cards
    total_sellers_count = Seller.objects.count()
    approved_sellers_count = Seller.objects.filter(status=Seller.STATUS_APPROVED).count()
    pending_sellers_count = Seller.objects.filter(status=Seller.STATUS_PENDING).count()
    suspended_sellers_count = Seller.objects.filter(status=Seller.STATUS_SUSPENDED).count()

    paginator = Paginator(sellers_qs, 15)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "sellers": page_obj,
        "page_obj": page_obj,
        "total_sellers_count": total_sellers_count,
        "approved_sellers_count": approved_sellers_count,
        "pending_sellers_count": pending_sellers_count,
        "suspended_sellers_count": suspended_sellers_count,
        "status_filter": status_filter,
        "bank_filter": bank_filter,
        "query": query,
    }
    return render(request, "custom_admin/sellers/list.html", context)


@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SELLER_MANAGER])
def admin_seller_approvals_view(request):
    """
    Dedicated queue for reviewing pending seller registration applications.
    """
    pending_sellers = (
        Seller.objects.filter(status=Seller.STATUS_PENDING)
        .select_related("user")
        .order_by("-created_at")
    )

    context = {
        "pending_sellers": pending_sellers,
        "pending_count": pending_sellers.count(),
    }
    return render(request, "custom_admin/sellers/approvals.html", context)


@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SELLER_MANAGER])
def admin_seller_detail_view(request, seller_id):
    """
    Comprehensive seller view with profile, business details, products, orders,
    commissions, payout history, bank details, and administrative controls.
    """
    seller = get_object_or_404(
        Seller.objects.select_related("user", "bank_verified_by"), pk=seller_id
    )

    # Financial & performance metrics
    seller_products = Product.objects.filter(seller=seller).select_related("category").order_by("-created_at")
    seller_orders = SellerOrder.objects.filter(seller=seller).select_related("order").order_by("-created_at")

    total_sales_revenue = (
        seller_orders.exclude(status="cancelled").aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00")
    )
    total_commission_earned = (
        seller_orders.exclude(status="cancelled").aggregate(total=Sum("commission_amount"))["total"]
        or Decimal("0.00")
    )

    # Current ledger balance
    ledger_entries = SellerLedgerEntry.objects.filter(seller=seller).order_by("-created_at")
    posted_balance = (
        ledger_entries.filter(status="posted").aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )

    # Payout batches
    payout_batches = PayoutBatch.objects.filter(seller=seller).order_by("-created_at")

    # Audit trail for this seller
    action_form = AdminSellerActionForm()
    commission_form = AdminSellerCommissionForm(initial={"commission_rate": seller.commission_rate})

    context = {
        "seller": seller,
        "seller_products": seller_products[:10],
        "seller_products_count": seller_products.count(),
        "seller_orders": seller_orders[:10],
        "seller_orders_count": seller_orders.count(),
        "total_sales_revenue": total_sales_revenue,
        "total_commission_earned": total_commission_earned,
        "posted_balance": posted_balance,
        "ledger_entries": ledger_entries[:15],
        "payout_batches": payout_batches[:10],
        "action_form": action_form,
        "commission_form": commission_form,
    }
    return render(request, "custom_admin/sellers/detail.html", context)


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SELLER_MANAGER])
def admin_seller_action_view(request, seller_id):
    """
    Execute seller state transitions: approve, reject, suspend, or reactivate.
    Enforces server-side validation and records audit logs.
    """
    seller = get_object_or_404(Seller, pk=seller_id)
    form = AdminSellerActionForm(request.POST)

    if not form.is_valid():
        error_msg = "; ".join([" ".join(errs) for errs in form.errors.values()])
        messages.error(request, f"Action failed: {error_msg}")
        return redirect("custom_admin:seller_detail", seller_id=seller.pk)

    action = form.cleaned_data["action"]
    reason = form.cleaned_data.get("reason", "").strip()
    actor_str = f"Admin {request.user.username}"
    now = timezone.now()

    if action == AdminSellerActionForm.ACTION_APPROVE:
        seller.status = Seller.STATUS_APPROVED
        seller.is_active = True
        seller.approved_at = now
        seller.admin_notes = f"[{now.strftime('%Y-%m-%d %H:%M')}] Approved by {actor_str}.\n" + (seller.admin_notes or "")
        seller.save(update_fields=["status", "is_active", "approved_at", "admin_notes", "updated_at"])
        log_admin_action(request.user, seller, CHANGE, f"Approved seller account. Notes: {reason or 'Standard approval'}")
        messages.success(request, f"Seller '{seller.store_name}' has been successfully approved.")

    elif action == AdminSellerActionForm.ACTION_REJECT:
        seller.status = Seller.STATUS_REJECTED
        seller.is_active = False
        seller.admin_notes = f"[{now.strftime('%Y-%m-%d %H:%M')}] Rejected by {actor_str}. Reason: {reason}\n" + (seller.admin_notes or "")
        seller.save(update_fields=["status", "is_active", "admin_notes", "updated_at"])
        log_admin_action(request.user, seller, CHANGE, f"Rejected seller account. Reason: {reason}")
        messages.warning(request, f"Seller application '{seller.store_name}' has been rejected.")

    elif action == AdminSellerActionForm.ACTION_SUSPEND:
        seller.status = Seller.STATUS_SUSPENDED
        seller.is_active = False
        seller.admin_notes = f"[{now.strftime('%Y-%m-%d %H:%M')}] Suspended by {actor_str}. Reason: {reason}\n" + (seller.admin_notes or "")
        seller.save(update_fields=["status", "is_active", "admin_notes", "updated_at"])
        # Deactivate seller's products to protect buyers
        Product.objects.filter(seller=seller).update(is_active=False)
        log_admin_action(request.user, seller, CHANGE, f"Suspended seller account and unlisted products. Reason: {reason}")
        messages.warning(request, f"Seller '{seller.store_name}' has been suspended and products set to inactive.")

    elif action == AdminSellerActionForm.ACTION_REACTIVATE:
        seller.status = Seller.STATUS_APPROVED
        seller.is_active = True
        seller.admin_notes = f"[{now.strftime('%Y-%m-%d %H:%M')}] Reactivated by {actor_str}.\n" + (seller.admin_notes or "")
        seller.save(update_fields=["status", "is_active", "admin_notes", "updated_at"])
        log_admin_action(request.user, seller, CHANGE, f"Reactivated seller account.")
        messages.success(request, f"Seller '{seller.store_name}' has been reactivated.")

    return redirect("custom_admin:seller_detail", seller_id=seller.pk)


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SELLER_MANAGER, ROLE_FINANCE_MANAGER])
def admin_seller_commission_update_view(request, seller_id):
    """
    Updates a seller's standard marketplace commission rate.
    """
    seller = get_object_or_404(Seller, pk=seller_id)
    form = AdminSellerCommissionForm(request.POST)

    if form.is_valid():
        old_rate = seller.commission_rate
        new_rate = form.cleaned_data["commission_rate"]
        seller.commission_rate = new_rate
        seller.save(update_fields=["commission_rate", "updated_at"])
        log_admin_action(
            request.user, seller, CHANGE, f"Updated commission rate from {old_rate}% to {new_rate}%"
        )
        messages.success(request, f"Commission rate for '{seller.store_name}' updated to {new_rate}%.")
    else:
        messages.error(request, "Invalid commission rate. Must be between 0% and 100%.")

    return redirect("custom_admin:seller_detail", seller_id=seller.pk)


# ==============================================================================
# 2. PRODUCT MANAGEMENT SAFEGUARDS & APPROVAL
# ==============================================================================

@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_product_approval_view(request, pk):
    """
    Approve or reject a submitted product.
    """
    product = get_object_or_404(Product, pk=pk)
    action = request.POST.get("action", "").strip().lower()
    reason = request.POST.get("reason", "").strip()

    if action == "approve":
        product.approval_status = "approved"
        product.is_approved = True
        product.rejection_reason = ""
        product.save(update_fields=["approval_status", "is_approved", "rejection_reason", "updated_at"])
        log_admin_action(request.user, product, CHANGE, "Approved product for publication")
        messages.success(request, f"Product '{product.name}' has been approved.")
    elif action == "reject":
        if not reason:
            messages.error(request, "A reason is required to reject a product.")
            return redirect("custom_admin:product_detail", pk=product.pk)
        product.approval_status = "rejected"
        product.is_approved = False
        product.rejection_reason = reason
        product.is_active = False
        product.save(update_fields=["approval_status", "is_approved", "rejection_reason", "is_active", "updated_at"])
        log_admin_action(request.user, product, CHANGE, f"Rejected product. Reason: {reason}")
        messages.warning(request, f"Product '{product.name}' rejected. Reason: {reason}")
    else:
        messages.error(request, "Invalid action requested.")

    return redirect("custom_admin:product_detail", pk=product.pk)


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_product_delete_view(request, pk):
    """
    Safeguarded product deletion:
    Prevents hard deletion if the product is referenced in existing customer orders,
    offering safe deactivation instead to preserve historical transactions.
    """
    product = get_object_or_404(Product, pk=pk)
    order_items_count = OrderItem.objects.filter(product=product).count()

    if order_items_count > 0:
        # Prevent database corruption & broken order histories
        product.is_active = False
        product.save(update_fields=["is_active", "updated_at"])
        log_admin_action(
            request.user, product, CHANGE,
            f"Blocked hard delete due to {order_items_count} order references; unlisted product instead"
        )
        messages.warning(
            request,
            f"Cannot permanently delete '{product.name}' because it is referenced in {order_items_count} historical order item(s). "
            f"The product has been safely unlisted (set to Inactive) instead."
        )
        return redirect("custom_admin:product_list")

    # Safe to delete permanently
    product_name = product.name
    log_admin_action(request.user, product, DELETION, f"Permanently deleted product '{product_name}'")
    product.delete()
    messages.success(request, f"Product '{product_name}' was permanently deleted.")
    return redirect("custom_admin:product_list")


# ==============================================================================
# 3. INVENTORY DASHBOARD & SAFE STOCK ADJUSTMENTS
# ==============================================================================

@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_inventory_view(request):
    """
    Dedicated inventory overview showing real-time stock levels, low-stock warnings,
    out-of-stock items, and search/filter controls.
    """
    filter_type = request.GET.get("filter", "all").strip().lower()
    query = request.GET.get("q", "").strip()
    category_id = request.GET.get("category", "").strip()

    products_qs = (
        Product.objects.select_related("category", "seller")
        .prefetch_related("variants")
        .order_by("stock_quantity")
    )

    if query:
        products_qs = products_qs.filter(
            Q(name__icontains=query)
            | Q(sku__icontains=query)
            | Q(seller__store_name__icontains=query)
        )

    if category_id.isdigit():
        products_qs = products_qs.filter(category_id=int(category_id))

    if filter_type == "low":
        products_qs = products_qs.filter(stock_quantity__gt=0, stock_quantity__lt=10)
    elif filter_type == "out":
        products_qs = products_qs.filter(stock_quantity=0)
    elif filter_type == "in":
        products_qs = products_qs.filter(stock_quantity__gte=10)

    # Inventory aggregate summary
    total_units = Product.objects.aggregate(total=Sum("stock_quantity"))["total"] or 0
    variant_units = ProductVariant.objects.aggregate(total=Sum("stock_quantity"))["total"] or 0
    all_units = total_units + variant_units

    out_of_stock_count = Product.objects.filter(stock_quantity=0).count()
    low_stock_count = Product.objects.filter(stock_quantity__gt=0, stock_quantity__lt=10).count()
    categories = Category.objects.filter(is_active=True).order_by("name")

    paginator = Paginator(products_qs, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "products": page_obj,
        "page_obj": page_obj,
        "all_units": all_units,
        "out_of_stock_count": out_of_stock_count,
        "low_stock_count": low_stock_count,
        "filter_type": filter_type,
        "query": query,
        "category_id": category_id,
        "categories": categories,
    }
    return render(request, "custom_admin/inventory/index.html", context)


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_PRODUCT_MANAGER])
def admin_inventory_update_view(request):
    """
    Safely update stock quantity for a product or variant.
    Prevents negative stock and logs audit trail.
    """
    product_id = request.POST.get("product_id")
    variant_id = request.POST.get("variant_id")
    form = AdminInventoryUpdateForm(request.POST)

    if not form.is_valid():
        error_msg = "; ".join([" ".join(errs) for errs in form.errors.values()])
        messages.error(request, f"Inventory adjustment failed: {error_msg}")
        return redirect("custom_admin:inventory")

    new_stock = form.cleaned_data["stock_quantity"]
    reason = form.cleaned_data.get("reason", "").strip()

    if variant_id:
        variant = get_object_or_404(ProductVariant, pk=variant_id)
        old_stock = variant.stock_quantity
        variant.stock_quantity = new_stock
        variant.save(update_fields=["stock_quantity"])
        log_admin_action(
            request.user, variant, CHANGE,
            f"Adjusted variant '{variant.sku}' stock from {old_stock} to {new_stock}. Reason: {reason}"
        )
        messages.success(request, f"Variant '{variant.sku}' stock updated to {new_stock}.")
    elif product_id:
        product = get_object_or_404(Product, pk=product_id)
        old_stock = product.stock_quantity
        product.stock_quantity = new_stock
        product.save(update_fields=["stock_quantity", "updated_at"])
        log_admin_action(
            request.user, product, CHANGE,
            f"Adjusted product '{product.name}' stock from {old_stock} to {new_stock}. Reason: {reason}"
        )
        messages.success(request, f"Product '{product.name}' stock updated to {new_stock}.")
    else:
        messages.error(request, "No target product or variant specified.")

    return redirect("custom_admin:inventory")


# ==============================================================================
# 4. CUSTOMER MANAGEMENT
# ==============================================================================

@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
def admin_customer_list_view(request):
    """
    List, search, filter and paginate registered customer accounts.
    Never exposes passwords, tokens, or private secrets.
    """
    query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip().lower()

    customers_qs = (
        User.objects.filter(is_staff=False, is_superuser=False)
        .select_related("customerprofile")
        .annotate(
            orders_count=Count("orders", distinct=True),
            total_spent=Sum("orders__total_amount", filter=~Q(orders__status="cancelled")),
        )
        .order_by("-date_joined")
    )

    if status_filter == "active":
        customers_qs = customers_qs.filter(is_active=True)
    elif status_filter == "inactive":
        customers_qs = customers_qs.filter(is_active=False)

    if query:
        customers_qs = customers_qs.filter(
            Q(username__icontains=query)
            | Q(email__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(customerprofile__phone_number__icontains=query)
        )

    total_customers_count = User.objects.filter(is_staff=False, is_superuser=False).count()
    active_customers_count = User.objects.filter(is_staff=False, is_superuser=False, is_active=True).count()

    paginator = Paginator(customers_qs, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "customers": page_obj,
        "page_obj": page_obj,
        "total_customers_count": total_customers_count,
        "active_customers_count": active_customers_count,
        "query": query,
        "status_filter": status_filter,
    }
    return render(request, "custom_admin/customers/list.html", context)


@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
def admin_customer_detail_view(request, user_id):
    """
    Customer profile view with order history, shipping addresses, and support details.
    """
    customer = get_object_or_404(
        User.objects.filter(is_staff=False, is_superuser=False).select_related("customerprofile"),
        pk=user_id,
    )

    customer_orders = Order.objects.filter(user=customer).prefetch_related("items").order_by("-created_at")
    customer_addresses = Address.objects.filter(user=customer).order_by("-is_default", "-created_at")
    total_spent = (
        customer_orders.exclude(status="cancelled").aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00")
    )

    context = {
        "customer": customer,
        "orders": customer_orders,
        "orders_count": customer_orders.count(),
        "addresses": customer_addresses,
        "total_spent": total_spent,
    }
    return render(request, "custom_admin/customers/detail.html", context)


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_SUPPORT_MANAGER])
def admin_customer_toggle_status_view(request, user_id):
    """
    Toggle customer account active status (activate/deactivate).
    """
    customer = get_object_or_404(
        User.objects.filter(is_staff=False, is_superuser=False), pk=user_id
    )
    customer.is_active = not customer.is_active
    customer.save(update_fields=["is_active"])

    status_str = "activated" if customer.is_active else "deactivated"
    log_admin_action(request.user, customer, CHANGE, f"Customer account {status_str}")
    messages.success(request, f"Customer account '{customer.username}' was {status_str}.")
    return redirect("custom_admin:customer_detail", user_id=customer.pk)


# ==============================================================================
# 5. COMMISSIONS ANALYTICS
# ==============================================================================

@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER])
def admin_commissions_view(request):
    """
    Marketplace commission dashboard displaying platform earnings,
    breakdown by seller, order commissions, and commission rates.
    """
    seller_id = request.GET.get("seller", "").strip()

    commission_orders_qs = (
        SellerOrder.objects.exclude(status="cancelled")
        .select_related("seller", "order")
        .order_by("-created_at")
    )

    if seller_id.isdigit():
        commission_orders_qs = commission_orders_qs.filter(seller_id=int(seller_id))

    total_commission_all_time = (
        SellerOrder.objects.exclude(status="cancelled").aggregate(total=Sum("commission_amount"))["total"]
        or Decimal("0.00")
    )

    # Current month commissions
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_commission = (
        SellerOrder.objects.exclude(status="cancelled")
        .filter(created_at__gte=month_start)
        .aggregate(total=Sum("commission_amount"))["total"]
        or Decimal("0.00")
    )

    # Top sellers by commission generated
    top_sellers = (
        Seller.objects.annotate(
            total_comm=Sum("seller_orders__commission_amount", filter=~Q(seller_orders__status="cancelled")),
            total_gmv=Sum("seller_orders__total_amount", filter=~Q(seller_orders__status="cancelled")),
        )
        .filter(total_comm__gt=0)
        .order_by("-total_comm")[:10]
    )

    all_sellers = Seller.objects.filter(status=Seller.STATUS_APPROVED).order_by("store_name")

    paginator = Paginator(commission_orders_qs, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "commission_orders": page_obj,
        "page_obj": page_obj,
        "total_commission_all_time": total_commission_all_time,
        "month_commission": month_commission,
        "top_sellers": top_sellers,
        "all_sellers": all_sellers,
        "selected_seller_id": seller_id,
    }
    return render(request, "custom_admin/ledger/commissions.html", context)


# ==============================================================================
# 6. REPORTS & CSV EXPORT
# ==============================================================================

@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, ROLE_PRODUCT_MANAGER])
def admin_reports_view(request):
    """
    Comprehensive analytics and reports dashboard with date filtering for:
    - Sales & Revenue
    - Orders & Status distribution
    - Seller Performance
    - Product Performance
    - Commissions & Payouts
    - Low-stock Inventory
    """
    form = AdminReportFilterForm(request.GET)
    preset = request.GET.get("preset", "all")
    start_date = None
    end_date = None

    now = timezone.now()
    if preset == "today":
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = now
    elif preset == "7d":
        start_date = now - timedelta(days=7)
        end_date = now
    elif preset == "30d":
        start_date = now - timedelta(days=30)
        end_date = now
    elif preset == "this_month":
        start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = now
    elif preset == "custom":
        sd_str = request.GET.get("start_date")
        ed_str = request.GET.get("end_date")
        if sd_str:
            try:
                start_date = timezone.make_aware(datetime.strptime(sd_str, "%Y-%m-%d"))
            except Exception:
                pass
        if ed_str:
            try:
                end_date = timezone.make_aware(datetime.strptime(ed_str, "%Y-%m-%d") + timedelta(days=1))
            except Exception:
                pass

    # Sales & Orders metrics in period
    orders_qs = Order.objects.all()
    if start_date:
        orders_qs = orders_qs.filter(created_at__gte=start_date)
    if end_date:
        orders_qs = orders_qs.filter(created_at__lte=end_date)

    total_orders_count = orders_qs.count()
    gross_sales = (
        orders_qs.exclude(status="cancelled").aggregate(total=Sum("total_amount"))["total"]
        or Decimal("0.00")
    )
    discounts_given = (
        orders_qs.exclude(status="cancelled").aggregate(total=Sum("discount_amount"))["total"]
        or Decimal("0.00")
    )
    shipping_collected = (
        orders_qs.exclude(status="cancelled").aggregate(total=Sum("shipping_fee"))["total"]
        or Decimal("0.00")
    )
    net_sales = gross_sales - shipping_collected

    # Orders breakdown by status
    orders_by_status = orders_qs.values("status").annotate(count=Count("id")).order_by("-count")

    # Seller performance
    seller_orders_qs = SellerOrder.objects.exclude(status="cancelled")
    if start_date:
        seller_orders_qs = seller_orders_qs.filter(created_at__gte=start_date)
    if end_date:
        seller_orders_qs = seller_orders_qs.filter(created_at__lte=end_date)

    seller_performance = (
        seller_orders_qs.values("seller__id", "seller__store_name")
        .annotate(
            orders_count=Count("id"),
            total_revenue=Sum("total_amount"),
            total_commission=Sum("commission_amount"),
        )
        .order_by("-total_revenue")[:10]
    )

    # Product performance (top sellers)
    order_items_qs = OrderItem.objects.exclude(order__status="cancelled")
    if start_date:
        order_items_qs = order_items_qs.filter(order__created_at__gte=start_date)
    if end_date:
        order_items_qs = order_items_qs.filter(order__created_at__lte=end_date)

    top_products = (
        order_items_qs.values("product__id", "product_name", "product_sku")
        .annotate(
            units_sold=Sum("quantity"),
            total_revenue=Sum("total_price"),
        )
        .order_by("-units_sold")[:10]
    )

    # Low-stock items
    low_stock_items = Product.objects.filter(is_active=True, stock_quantity__lt=10).order_by("stock_quantity")[:10]

    context = {
        "form": form,
        "preset": preset,
        "start_date": start_date,
        "end_date": end_date,
        "total_orders_count": total_orders_count,
        "gross_sales": gross_sales,
        "discounts_given": discounts_given,
        "shipping_collected": shipping_collected,
        "net_sales": net_sales,
        "orders_by_status": orders_by_status,
        "seller_performance": seller_performance,
        "top_products": top_products,
        "low_stock_items": low_stock_items,
    }
    return render(request, "custom_admin/reports/index.html", context)


@require_GET
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN, ROLE_FINANCE_MANAGER, ROLE_PRODUCT_MANAGER])
def admin_export_report_csv_view(request, report_type):
    """
    Safely exports analytical reports as CSV files with sanitized fields to prevent CSV formula injection.
    """
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"fitmeezo_{report_type}_report_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)

    if report_type == "sales":
        writer.writerow(["Order Number", "Date", "Customer", "Status", "Payment Method", "Payment Status", "Subtotal", "Shipping", "Discount", "Total Amount"])
        orders = Order.objects.select_related("user").order_by("-created_at")[:1000]
        for o in orders:
            writer.writerow([
                sanitize_csv_cell(o.order_number),
                sanitize_csv_cell(o.created_at.strftime("%Y-%m-%d %H:%M")),
                sanitize_csv_cell(o.user.email if o.user else o.shipping_full_name),
                sanitize_csv_cell(o.status),
                sanitize_csv_cell(o.payment_method),
                sanitize_csv_cell(o.payment_status),
                sanitize_csv_cell(str(o.subtotal)),
                sanitize_csv_cell(str(o.shipping_fee)),
                sanitize_csv_cell(str(o.discount_amount)),
                sanitize_csv_cell(str(o.total_amount)),
            ])

    elif report_type == "sellers":
        writer.writerow(["Store Name", "Legal Name", "Status", "Contact Email", "Phone", "City", "State", "GSTIN", "Commission Rate (%)", "Created Date"])
        sellers = Seller.objects.order_by("-created_at")[:1000]
        for s in sellers:
            writer.writerow([
                sanitize_csv_cell(s.store_name),
                sanitize_csv_cell(s.legal_business_name),
                sanitize_csv_cell(s.status),
                sanitize_csv_cell(s.support_email),
                sanitize_csv_cell(s.support_phone),
                sanitize_csv_cell(s.city),
                sanitize_csv_cell(s.state),
                sanitize_csv_cell(s.gstin),
                sanitize_csv_cell(str(s.commission_rate)),
                sanitize_csv_cell(s.created_at.strftime("%Y-%m-%d")),
            ])

    elif report_type == "products":
        writer.writerow(["SKU", "Product Name", "Category", "Seller", "Price", "Compare At Price", "Stock", "Active", "Approval Status"])
        products = Product.objects.select_related("category", "seller").order_by("name")[:1000]
        for p in products:
            writer.writerow([
                sanitize_csv_cell(p.sku),
                sanitize_csv_cell(p.name),
                sanitize_csv_cell(p.category.name if p.category else ""),
                sanitize_csv_cell(p.seller.store_name if p.seller else "Platform"),
                sanitize_csv_cell(str(p.price)),
                sanitize_csv_cell(str(p.compare_at_price or "")),
                sanitize_csv_cell(str(p.stock_quantity)),
                sanitize_csv_cell("Yes" if p.is_active else "No"),
                sanitize_csv_cell(p.approval_status),
            ])

    elif report_type == "inventory":
        writer.writerow(["SKU", "Product Name", "Category", "Current Stock", "Status"])
        products = Product.objects.select_related("category").order_by("stock_quantity")[:1000]
        for p in products:
            status = "Out of Stock" if p.stock_quantity == 0 else ("Low Stock" if p.stock_quantity < 10 else "In Stock")
            writer.writerow([
                sanitize_csv_cell(p.sku),
                sanitize_csv_cell(p.name),
                sanitize_csv_cell(p.category.name if p.category else ""),
                sanitize_csv_cell(str(p.stock_quantity)),
                sanitize_csv_cell(status),
            ])

    else:
        return HttpResponseForbidden("Invalid report type requested.")

    return response


# ==============================================================================
# 7. ADMIN USERS & ROLES MANAGEMENT
# ==============================================================================

@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN])
def admin_users_view(request):
    """
    Admin user management: view all staff accounts, their assigned roles, and activity status.
    Strictly restricted to Super Administrators.
    """
    staff_users = (
        User.objects.filter(Q(is_staff=True) | Q(is_superuser=True))
        .prefetch_related("groups")
        .order_by("-is_superuser", "username")
    )

    user_data = []
    for u in staff_users:
        user_data.append({
            "user": u,
            "role": get_user_role(u),
            "is_self": u.pk == request.user.pk,
        })

    create_form = AdminStaffUserForm()

    context = {
        "staff_users": user_data,
        "create_form": create_form,
        "all_roles": ALL_ROLES,
    }
    return render(request, "custom_admin/users/list.html", context)


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN])
def admin_user_create_view(request):
    """
    Create a new staff member and assign their initial administrative role.
    """
    form = AdminStaffUserForm(request.POST)
    if form.is_valid():
        username = form.cleaned_data["username"]
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        first_name = form.cleaned_data.get("first_name", "")
        last_name = form.cleaned_data.get("last_name", "")
        role = form.cleaned_data["role"]
        is_active = form.cleaned_data["is_active"]

        new_user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_staff=True,
            is_active=is_active,
        )

        set_user_role(new_user, role)
        log_admin_action(request.user, new_user, ADDITION, f"Created staff user with role '{role}'")
        messages.success(request, f"Staff user '{username}' created with role '{role}'.")
    else:
        error_msg = "; ".join([" ".join(errs) for errs in form.errors.values()])
        messages.error(request, f"Failed to create user: {error_msg}")

    return redirect("custom_admin:users_list")


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN])
def admin_user_role_update_view(request, user_id):
    """
    Update a staff user's administrative role.
    Prevents superusers from demoting themselves to prevent lockout.
    """
    target_user = get_object_or_404(User, pk=user_id)
    new_role = request.POST.get("role", "").strip()

    if target_user.pk == request.user.pk and new_role != ROLE_SUPER_ADMIN:
        messages.error(request, "You cannot demote or remove Super Admin privileges from your own active account.")
        return redirect("custom_admin:users_list")

    if new_role not in ALL_ROLES:
        messages.error(request, f"Invalid role specified. Must be one of: {', '.join(ALL_ROLES)}")
        return redirect("custom_admin:users_list")

    old_role = get_user_role(target_user)
    set_user_role(target_user, new_role)

    # If made Super Admin, grant is_superuser
    if new_role == ROLE_SUPER_ADMIN:
        target_user.is_superuser = True
        target_user.save(update_fields=["is_superuser"])
    elif target_user.is_superuser and target_user.pk != request.user.pk:
        target_user.is_superuser = False
        target_user.save(update_fields=["is_superuser"])

    log_admin_action(
        request.user, target_user, CHANGE, f"Updated role from '{old_role}' to '{new_role}'"
    )
    messages.success(request, f"Role for '{target_user.username}' updated to '{new_role}'.")
    return redirect("custom_admin:users_list")


@require_POST
@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN])
def admin_user_toggle_status_view(request, user_id):
    """
    Toggle a staff account active status. Prevents deactivating own account.
    """
    target_user = get_object_or_404(User, pk=user_id)

    if target_user.pk == request.user.pk:
        messages.error(request, "You cannot deactivate your own administrative account.")
        return redirect("custom_admin:users_list")

    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=["is_active"])

    status_str = "activated" if target_user.is_active else "deactivated"
    log_admin_action(request.user, target_user, CHANGE, f"Staff account {status_str}")
    messages.success(request, f"Staff account '{target_user.username}' was {status_str}.")
    return redirect("custom_admin:users_list")


# ==============================================================================
# 8. AUDIT LOGS & SETTINGS VIEWS
# ==============================================================================

@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN])
def admin_audit_logs_view(request):
    """
    Consolidated audit log viewer showing administrative actions,
    payout status changes, and return workflow transitions.
    """
    query = request.GET.get("q", "").strip()

    logs_qs = LogEntry.objects.select_related("user", "content_type").order_by("-action_time")

    if query:
        logs_qs = logs_qs.filter(
            Q(object_repr__icontains=query)
            | Q(change_message__icontains=query)
            | Q(user__username__icontains=query)
        )

    paginator = Paginator(logs_qs, 30)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Payout audit logs
    recent_payout_logs = (
        PayoutAuditLog.objects.select_related("actor", "payout_batch")
        .order_by("-created_at")[:15]
    )

    context = {
        "logs": page_obj,
        "page_obj": page_obj,
        "recent_payout_logs": recent_payout_logs,
        "query": query,
    }
    return render(request, "custom_admin/audit_logs/index.html", context)


@admin_role_required(allowed_roles=[ROLE_SUPER_ADMIN])
def admin_settings_view(request):
    """
    System configuration overview: store settings, payment gateways,
    shipping integrations, database engines, and environment status.
    """
    from django.conf import settings

    context = {
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        "brand_tagline": getattr(settings, "BRAND_TAGLINE", "SHOP SMART • LIVE BETTER"),
        "support_email": getattr(settings, "SUPPORT_EMAIL", "support@fitmeezo.com"),
        "currency": getattr(settings, "CURRENCY", "INR"),
        "currency_symbol": getattr(settings, "CURRENCY_SYMBOL", "₹"),
        "debug_mode": settings.DEBUG,
        "db_engine": settings.DATABASES["default"]["ENGINE"],
        "db_name": settings.DATABASES["default"]["NAME"],
        "default_commission_rate": "10.00%",
        "razorpay_configured": bool(getattr(settings, "RAZORPAY_KEY_ID", "")),
        "allowed_hosts": settings.ALLOWED_HOSTS,
        "session_cookie_age": getattr(settings, "SESSION_COOKIE_AGE", 1209600),
    }
    return render(request, "custom_admin/settings/index.html", context)


# ==============================================================================
# 9. GLOBAL QUICK SEARCH
# ==============================================================================

@admin_role_required()
def admin_global_search_view(request):
    """
    Omni-search searching across Orders, Products, Sellers, and Customers.
    """
    q = request.GET.get("q", "").strip()
    if not q:
        return redirect("custom_admin:dashboard")

    # Match orders
    matching_orders = Order.objects.filter(
        Q(order_number__icontains=q)
        | Q(shipping_full_name__icontains=q)
        | Q(shipping_phone_number__icontains=q)
    )[:10]

    # Match products
    matching_products = Product.objects.filter(
        Q(name__icontains=q) | Q(sku__icontains=q)
    ).select_related("category")[:10]

    # Match sellers
    matching_sellers = Seller.objects.filter(
        Q(store_name__icontains=q)
        | Q(support_email__icontains=q)
        | Q(support_phone__icontains=q)
    )[:10]

    # Match customers
    matching_customers = User.objects.filter(
        is_staff=False,
        is_superuser=False,
    ).filter(
        Q(username__icontains=q)
        | Q(email__icontains=q)
        | Q(first_name__icontains=q)
        | Q(last_name__icontains=q)
    )[:10]

    context = {
        "query": q,
        "orders": matching_orders,
        "products": matching_products,
        "sellers": matching_sellers,
        "customers": matching_customers,
        "total_results": (
            len(matching_orders)
            + len(matching_products)
            + len(matching_sellers)
            + len(matching_customers)
        ),
    }
    return render(request, "custom_admin/search_results.html", context)
