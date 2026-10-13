"""
Fitmeezo Seller Operations & Metrics Service.
Centralizes scoped data aggregations, financial snapshot computations,
and inventory monitoring for third-party marketplace sellers.
"""

from datetime import datetime, time
from decimal import Decimal
import logging
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import OrderItem, Product, ProductVariant, Seller, SellerOrder

logger = logging.getLogger(__name__)

# Centralized, clearly documented threshold for low-stock inventory alerts
DEFAULT_LOW_STOCK_THRESHOLD = 10


def parse_date_range(start_date_str: str, end_date_str: str):
    """
    Parses and validates user-supplied YYYY-MM-DD date bounds into timezone-aware datetimes.

    Returns:
        tuple: (start_dt, end_dt, error_message)
        If no dates supplied, returns (None, None, None).
        If validation fails, returns (None, None, error_message).
    """
    if not start_date_str and not end_date_str:
        return None, None, None

    start_date = None
    end_date = None
    tz = timezone.get_current_timezone()

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None, None, "Invalid start date format. Please use YYYY-MM-DD."

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None, None, "Invalid end date format. Please use YYYY-MM-DD."

    if start_date and end_date and start_date > end_date:
        return None, None, "Start date cannot be after end date."

    start_dt = None
    if start_date:
        start_dt = timezone.make_aware(datetime.combine(start_date, time.min), tz)

    end_dt = None
    if end_date:
        end_dt = timezone.make_aware(datetime.combine(end_date, time.max), tz)

    return start_dt, end_dt, None


def get_seller_dashboard_metrics(
    seller: Seller,
    start_dt=None,
    end_dt=None,
    low_stock_threshold: int = DEFAULT_LOW_STOCK_THRESHOLD,
) -> dict:
    """
    Computes all dashboard metrics strictly scoped to the specified Seller.

    Financial Principles:
    - Calculations use exact Decimal arithmetic.
    - Commission and payable values rely STRICTLY on stored snapshots from SellerOrder records.
      Historical commissions are NEVER recalculated using current commission rates.
    - Platform-owned merchandise (seller=None) and other sellers' records are excluded by design.
    - Figures represent order financial snapshot summaries, not processed payouts or settlements.
    - Estimated logistics expenses are not deducted as actual expenses.
    """
    if not seller:
        raise ValueError("A valid Seller instance is required to compute metrics.")

    # 1. Base SellerOrders queryset strictly scoped to this seller
    base_orders_qs = SellerOrder.objects.filter(seller=seller)

    # All-time package counts
    total_assigned_orders = base_orders_qs.count()

    # 2. Date filtering
    filtered_orders_qs = base_orders_qs
    if start_dt:
        filtered_orders_qs = filtered_orders_qs.filter(created_at__gte=start_dt)
    if end_dt:
        filtered_orders_qs = filtered_orders_qs.filter(created_at__lte=end_dt)

    filtered_orders_count = filtered_orders_qs.count()

    # 3. Fulfillment status counts (within the filtered date window)
    pending_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_PENDING).count()
    processing_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_PROCESSING).count()
    ready_to_ship_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_READY_TO_SHIP).count()
    shipped_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_SHIPPED).count()
    delivered_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_DELIVERED).count()
    cancelled_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_CANCELLED).count()

    # 4. Financial totals from stored snapshots (excluding cancelled orders from active sales)
    active_packages_qs = filtered_orders_qs.exclude(status=SellerOrder.STATUS_CANCELLED)
    financials = active_packages_qs.aggregate(
        gross_sales=Coalesce(Sum("subtotal"), Decimal("0.00")),
        total_commission=Coalesce(Sum("total_commission"), Decimal("0.00")),
        net_payable=Coalesce(Sum("net_seller_payable"), Decimal("0.00")),
    )

    gross_sales = financials["gross_sales"]
    total_commission = financials["total_commission"]
    net_seller_payable = financials["net_payable"]

    # 5. Seller's own catalog & inventory monitoring
    seller_products_qs = Product.objects.filter(seller=seller)
    total_products = seller_products_qs.count()
    active_products = seller_products_qs.filter(is_active=True).count()

    # Identify low-stock products & variants (threshold <= low_stock_threshold)
    low_stock_alerts = []
    for product in seller_products_qs.prefetch_related("variants"):
        if product.variants.exists():
            # Check individual active variants
            for variant in product.variants.filter(is_active=True):
                if variant.stock_quantity <= low_stock_threshold:
                    low_stock_alerts.append({
                        "product": product,
                        "product_id": product.id,
                        "product_name": product.name,
                        "variant_id": variant.id,
                        "item_name": f"{product.name} ({variant.option_type}: {variant.option_value})",
                        "sku": variant.sku,
                        "stock_quantity": variant.stock_quantity,
                        "total_stock": variant.stock_quantity,
                        "is_variant": True,
                        "is_out_of_stock": variant.stock_quantity == 0,
                    })
        else:
            if product.stock_quantity <= low_stock_threshold:
                low_stock_alerts.append({
                    "product": product,
                    "product_id": product.id,
                    "product_name": product.name,
                    "variant_id": None,
                    "item_name": product.name,
                    "sku": product.sku,
                    "stock_quantity": product.stock_quantity,
                    "total_stock": product.stock_quantity,
                    "is_variant": False,
                    "is_out_of_stock": product.stock_quantity == 0,
                })

    low_stock_count = len(low_stock_alerts)

    # 6. Recent packages list (latest 10)
    recent_packages = (
        filtered_orders_qs.select_related("order")
        .prefetch_related("items")
        .order_by("-created_at")[:10]
    )

    return {
        "total_assigned_orders": total_assigned_orders,
        "filtered_orders_count": filtered_orders_count,
        "pending_count": pending_count,
        "processing_count": processing_count,
        "ready_to_ship_count": ready_to_ship_count,
        "shipped_count": shipped_count,
        "delivered_count": delivered_count,
        "cancelled_count": cancelled_count,
        "gross_sales": gross_sales,
        "total_commission": total_commission,
        "net_seller_payable": net_seller_payable,
        "total_products": total_products,
        "active_products": active_products,
        "low_stock_threshold": low_stock_threshold,
        "low_stock_count": low_stock_count,
        "low_stock_alerts": low_stock_alerts[:20],  # Top 20 alerts
        "recent_packages": recent_packages,
        "is_empty": (total_assigned_orders == 0 and total_products == 0),
        # Structured nested dictionaries for clean template access and API consumption
        "financial_summary": {
            "gross_sales": gross_sales,
            "total_commission": total_commission,
            "net_seller_payable": net_seller_payable,
        },
        "order_counts": {
            "total": filtered_orders_count,
            "pending": pending_count,
            "processing": processing_count,
            "ready_to_ship": ready_to_ship_count,
            "shipped": shipped_count,
            "delivered": delivered_count,
            "cancelled": cancelled_count,
        },
        "catalog_summary": {
            "total_products": total_products,
            "active_products": active_products,
            "low_stock_products": low_stock_count,
        },
    }


def sanitize_csv_cell(value):
    """
    Sanitizes values written to CSV cells to prevent CSV Formula Injection (CWE-1236).
    If a string begins with '=', '+', '-', '@', '\t', or '\r', prefixes it with a single quote (').
    """
    if value is None:
        return ""
    val_str = str(value)
    if val_str and val_str[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{val_str}"
    return val_str


def get_seller_reports_data(
    seller: Seller,
    start_dt=None,
    end_dt=None,
) -> dict:
    """
    Computes comprehensive, seller-scoped performance reports, financial snapshot totals,
    status breakdowns, and product sales quantities.

    Safety & Integrity Principles:
    - Scoped strictly to SellerOrder.objects.filter(seller=seller) and OrderItem.objects.filter(seller=seller).
    - Platform packages and other sellers' records are never accessed or aggregated.
    - Financial totals use stored SellerOrder snapshots (subtotal, total_commission, net_seller_payable).
      Historical commission is NEVER recomputed from the seller's current commission rate.
    - Active package totals explicitly EXCLUDE cancelled packages.
    - Cancelled packages and their financial snapshots are segregated and clearly reported separately.
    - Commission is never double-counted (summed strictly from SellerOrder, not adding OrderItem commission).
    - Figures represent recorded order snapshots, not processed or settled payouts.
    """
    if not seller:
        raise ValueError("A valid Seller instance is required to compute reports data.")

    # 1. Base SellerOrders queryset strictly scoped to this seller
    base_orders_qs = SellerOrder.objects.filter(seller=seller)

    # 2. Date filtering
    filtered_orders_qs = base_orders_qs
    if start_dt:
        filtered_orders_qs = filtered_orders_qs.filter(created_at__gte=start_dt)
    if end_dt:
        filtered_orders_qs = filtered_orders_qs.filter(created_at__lte=end_dt)

    total_packages_count = filtered_orders_qs.count()

    # 3. Fulfillment status breakdown
    pending_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_PENDING).count()
    processing_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_PROCESSING).count()
    ready_to_ship_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_READY_TO_SHIP).count()
    shipped_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_SHIPPED).count()
    delivered_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_DELIVERED).count()
    cancelled_count = filtered_orders_qs.filter(status=SellerOrder.STATUS_CANCELLED).count()
    active_count = total_packages_count - cancelled_count

    # 4. Financial totals from stored snapshots for active (non-cancelled) packages
    active_packages_qs = filtered_orders_qs.exclude(status=SellerOrder.STATUS_CANCELLED)
    active_financials = active_packages_qs.aggregate(
        gross_sales=Coalesce(Sum("subtotal"), Decimal("0.00")),
        total_commission=Coalesce(Sum("total_commission"), Decimal("0.00")),
        net_payable=Coalesce(Sum("net_seller_payable"), Decimal("0.00")),
    )
    gross_sales = Decimal(str(active_financials["gross_sales"])).quantize(Decimal("0.01"))
    total_commission = Decimal(str(active_financials["total_commission"])).quantize(Decimal("0.01"))
    net_seller_payable = Decimal(str(active_financials["net_payable"])).quantize(Decimal("0.01"))

    # Cancelled packages financial totals (tracked separately)
    cancelled_packages_qs = filtered_orders_qs.filter(status=SellerOrder.STATUS_CANCELLED)
    cancelled_financials = cancelled_packages_qs.aggregate(
        cancelled_subtotal=Coalesce(Sum("subtotal"), Decimal("0.00")),
        cancelled_commission=Coalesce(Sum("total_commission"), Decimal("0.00")),
        cancelled_payable=Coalesce(Sum("net_seller_payable"), Decimal("0.00")),
    )
    cancelled_subtotal = Decimal(str(cancelled_financials["cancelled_subtotal"])).quantize(Decimal("0.01"))
    cancelled_commission = Decimal(str(cancelled_financials["cancelled_commission"])).quantize(Decimal("0.01"))
    cancelled_payable = Decimal(str(cancelled_financials["cancelled_payable"])).quantize(Decimal("0.01"))

    # 5. Product sales quantities for the seller's own items (active orders only)
    # Grouped by product name, variant label, and SKU
    product_sales_qs = (
        OrderItem.objects.filter(
            seller=seller,
            seller_order__in=active_packages_qs,
        )
        .values("product_name", "variant_label", "sku")
        .annotate(
            units_sold=Coalesce(Sum("quantity"), 0),
            gross_sales=Coalesce(Sum("line_total"), Decimal("0.00")),
            orders_count=Count("id"),
        )
        .order_by("-units_sold", "-gross_sales")
    )
    product_sales = list(product_sales_qs)

    total_units_sold = sum(item["units_sold"] for item in product_sales) if product_sales else 0
    total_product_revenue = sum(item["gross_sales"] for item in product_sales) if product_sales else Decimal("0.00")

    # 6. Recent packages list (up to 25 within the filtered window)
    recent_packages = (
        filtered_orders_qs.select_related("order")
        .prefetch_related("items")
        .order_by("-created_at")[:25]
    )

    return {
        "total_packages": total_packages_count,
        "active_packages_count": active_count,
        "pending_count": pending_count,
        "processing_count": processing_count,
        "ready_to_ship_count": ready_to_ship_count,
        "shipped_count": shipped_count,
        "delivered_count": delivered_count,
        "cancelled_count": cancelled_count,
        "gross_sales": gross_sales,
        "total_commission": total_commission,
        "net_seller_payable": net_seller_payable,
        "cancelled_subtotal": cancelled_subtotal,
        "cancelled_commission": cancelled_commission,
        "cancelled_payable": cancelled_payable,
        "product_sales": product_sales,
        "total_units_sold": total_units_sold,
        "total_product_revenue": total_product_revenue,
        "recent_packages": recent_packages,
        "order_counts": {
            "total": total_packages_count,
            "active": active_count,
            "pending": pending_count,
            "processing": processing_count,
            "ready_to_ship": ready_to_ship_count,
            "shipped": shipped_count,
            "delivered": delivered_count,
            "cancelled": cancelled_count,
        },
        "financial_summary": {
            "gross_sales": gross_sales,
            "total_commission": total_commission,
            "net_seller_payable": net_seller_payable,
            "cancelled_subtotal": cancelled_subtotal,
            "cancelled_commission": cancelled_commission,
            "cancelled_payable": cancelled_payable,
        },
    }

