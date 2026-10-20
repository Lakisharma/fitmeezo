from django import forms
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import slugify

User = get_user_model()

from .models import (
    Address,
    Cart,
    CartItem,
    Category,
    CustomerProfile,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    Seller,
    SellerLoginOTP,
    SellerOrder,
    WishlistItem,
    Order,
    OrderItem,
    OrderStatusHistory,
    RefundRecord,
    ReturnAuditLog,
    ReturnRequest,
    ReturnRequestItem,
    ShipmentBooking,
    CODReceiptRecord,
    PayoutBatch,
    PayoutAuditLog,
    SellerLedgerEntry,
    Invoice,
    InvoiceSequence,
    ContactInquiry,
)
from .ledger_services import (
    process_mock_payout_batch,
    reverse_payout_batch,
    reconcile_cod_remittance,
    LedgerServiceError,
)
from .return_services import (
    transition_return_status,
    process_simulated_refund,
    ReturnServiceError,
    RefundError,
)


class StockStatusFilter(admin.SimpleListFilter):
    """
    Custom admin filter for inventory levels: In Stock, Low Stock, Out of Stock.
    """
    title = "Inventory Level"
    parameter_name = "inventory_level"

    def lookups(self, request, model_admin):
        return [
            ("in_stock", "In Stock (> 9 units)"),
            ("low_stock", "Low Stock (1-9 units)"),
            ("out_of_stock", "Out of Stock (0 units)"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "in_stock":
            return queryset.filter(stock_quantity__gte=10)
        if self.value() == "low_stock":
            return queryset.filter(stock_quantity__gt=0, stock_quantity__lt=10)
        if self.value() == "out_of_stock":
            return queryset.filter(stock_quantity=0)
        return queryset


class ProductImageInline(admin.TabularInline):
    """
    Inline gallery management for additional product images.
    """
    model = ProductImage
    extra = 1
    fields = ("image", "alt_text", "display_order")


class ProductVariantInline(admin.TabularInline):
    """
    Inline management for product variants directly under parent Product.
    """
    model = ProductVariant
    extra = 1
    fields = (
        "option_type",
        "option_value",
        "sku",
        "price",
        "compare_at_price",
        "stock_quantity",
        "weight_grams",
        "length_cm",
        "breadth_cm",
        "height_cm",
        "is_dimensions_verified",
        "is_active",
        "stock_indicator",
    )
    readonly_fields = ("stock_indicator",)

    @admin.display(description="Stock Indicator")
    def stock_indicator(self, obj):
        if not obj or not obj.pk:
            return "—"
        if not obj.is_active:
            return "⚪ Inactive"
        if obj.stock_quantity == 0:
            return "🔴 Out of Stock (0)"
        elif obj.stock_quantity < 10:
            return f"🟡 Low Stock ({obj.stock_quantity})"
        return f"🟢 In Stock ({obj.stock_quantity})"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    """
    Django Admin interface for Fitmeezo Category model.
    Enables inline reordering via list_editable, fast status toggling,
    instant search, and automated slug generation during creation.
    """

    list_display = (
        "name",
        "slug",
        "is_active",
        "display_order",
        "created_at",
        "updated_at",
    )
    list_editable = (
        "display_order",
        "is_active",
    )
    list_filter = (
        "is_active",
        "created_at",
    )
    search_fields = (
        "name",
        "slug",
        "description",
    )
    prepopulated_fields = {
        "slug": ("name",),
    }
    ordering = (
        "display_order",
        "name",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    fieldsets = (
        ("Basic Information", {
            "fields": ("name", "slug", "description")
        }),
        ("Display & Visibility", {
            "fields": ("is_active", "display_order", "image")
        }),
        ("System Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """
    Django Admin interface for Fitmeezo Product catalogue.
    Features SKU search, category/status filters, stock tracking,
    inline gallery image uploads, variant management, and calculated discount displays.
    """

    list_display = (
        "name",
        "sku",
        "category",
        "seller",
        "formatted_price",
        "dimensions_summary",
        "stock_status",
        "is_active",
        "is_approved",
        "approval_status",
        "is_featured",
        "created_at",
    )
    list_editable = (
        "is_active",
        "is_featured",
    )
    list_filter = (
        "category",
        "seller",
        "is_dimensions_verified",
        "is_approved",
        "approval_status",
        "is_active",
        "is_featured",
        "created_at",
    )
    search_fields = (
        "name",
        "sku",
        "short_description",
        "seller__store_name",
        "seller__legal_business_name",
    )
    prepopulated_fields = {
        "slug": ("name",),
    }
    raw_id_fields = ("seller",)
    ordering = ("-created_at",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "calculated_discount",
        "volumetric_weight_display",
    )
    actions = ["approve_products", "reject_products"]
    inlines = [ProductImageInline, ProductVariantInline]

    fieldsets = (
        ("Product Identity", {
            "fields": (
                "name",
                "slug",
                "sku",
                "category",
                "seller",
                "short_description",
                "description",
            )
        }),
        ("Pricing & Standalone Inventory", {
            "description": "For products with variants, stock and prices are governed by the variants below.",
            "fields": (
                ("price", "compare_at_price", "calculated_discount"),
                "stock_quantity",
            )
        }),
        ("Physical Dimensions & Logistics", {
            "fields": (
                ("weight_grams", "length_cm", "breadth_cm", "height_cm"),
                ("is_dimensions_verified", "volumetric_weight_display"),
            ),
        }),
        ("Primary Showcase Media", {
            "fields": ("image",)
        }),
        ("Catalogue Status & Moderation", {
            "fields": (
                ("is_active", "is_featured"),
                ("is_approved", "approval_status"),
                "rejection_reason",
            )
        }),
        ("Audit Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description="Dimensions (L×B×H cm, Wt)")
    def dimensions_summary(self, obj):
        return obj.dimensions_display

    @admin.display(description="Volumetric Wt (kg)")
    def volumetric_weight_display(self, obj):
        vw = obj.volumetric_weight_kg
        return f"{vw:.3f} kg" if vw else "Not calculated"


    @admin.action(description="Approve selected seller products for catalog display")
    def approve_products(self, request, queryset):
        updated = queryset.update(
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
            rejection_reason="",
        )
        self.message_user(request, f"{updated} product(s) marked as Approved.")

    @admin.action(description="Reject selected seller products")
    def reject_products(self, request, queryset):
        updated = queryset.update(
            is_approved=False,
            approval_status=Product.APPROVAL_REJECTED,
        )
        self.message_user(request, f"{updated} product(s) marked as Rejected.")

    def get_queryset(self, request):
        """Optimizes queries by eager loading category and seller in single SQL JOIN."""
        return super().get_queryset(request).select_related("category", "seller").prefetch_related("variants")

    @admin.display(description="Price (₹)")
    def formatted_price(self, obj):
        if obj.has_variants and obj.has_variable_pricing:
            return f"₹{obj.min_price:,.2f} – ₹{obj.max_price:,.2f}"
        elif obj.has_variants:
            return f"₹{obj.min_price:,.2f}"
        return f"₹{obj.price:,.2f}"

    @admin.display(description="Stock Status")
    def stock_status(self, obj):
        if obj.variants.exists():
            total = obj.available_inventory
            count = obj.variants.count()
            if total == 0:
                return f"Out of Stock (0 in {count} variants)"
            elif total < 10:
                return f"Low Stock ({total} in {count} variants)"
            return f"In Stock ({total} in {count} variants)"
        if obj.stock_quantity == 0:
            return "Out of Stock (0)"
        elif obj.stock_quantity < 10:
            return f"Low Stock ({obj.stock_quantity})"
        return f"In Stock ({obj.stock_quantity})"

    @admin.display(description="Calculated Discount")
    def calculated_discount(self, obj):
        if obj.discount_percentage > 0:
            return f"{obj.discount_percentage}% OFF"
        return "None"


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    """
    Dedicated Django Admin interface for all Product Variants.
    Provides SKU search, inventory filtering, price inspection, and batch status toggles.
    """

    list_display = (
        "sku",
        "product",
        "option_type",
        "option_value",
        "effective_price_display",
        "stock_quantity",
        "stock_status_indicator",
        "is_active",
        "updated_at",
    )
    list_editable = (
        "stock_quantity",
        "is_active",
    )
    list_filter = (
        "is_active",
        "option_type",
        StockStatusFilter,
        "product__category",
        "created_at",
    )
    search_fields = (
        "sku",
        "option_value",
        "option_type",
        "product__name",
        "product__sku",
    )
    ordering = ("product", "option_type", "option_value")
    readonly_fields = (
        "created_at",
        "updated_at",
        "calculated_discount_display",
        "effective_price_display",
    )
    fieldsets = (
        ("Variant Identity", {
            "fields": (
                "product",
                ("option_type", "option_value"),
                "sku",
            )
        }),
        ("Pricing Overrides", {
            "description": "Leave blank to inherit the parent product's price and compare-at price.",
            "fields": (
                ("price", "compare_at_price"),
                ("effective_price_display", "calculated_discount_display"),
            )
        }),
        ("Inventory & Availability", {
            "fields": (
                "stock_quantity",
                "is_active",
            )
        }),
        ("Audit Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("product", "product__category")

    @admin.display(description="Effective Price (₹)")
    def effective_price_display(self, obj):
        eff = obj.effective_price
        if obj.price is not None:
            return f"₹{eff:,.2f} (Override)"
        return f"₹{eff:,.2f} (Inherited)"

    @admin.display(description="Stock Status")
    def stock_status_indicator(self, obj):
        if not obj.is_active:
            return "⚪ Inactive"
        if obj.stock_quantity == 0:
            return "🔴 Out of Stock (0)"
        elif obj.stock_quantity < 10:
            return f"🟡 Low Stock ({obj.stock_quantity})"
        return f"🟢 In Stock ({obj.stock_quantity})"

    @admin.display(description="Calculated Discount")
    def calculated_discount_display(self, obj):
        pct = obj.discount_percentage
        if pct > 0:
            return f"{pct}% OFF"
        return "None"


class CartItemInline(admin.TabularInline):
    """
    Inline management of items inside a Shopping Cart.
    """
    model = CartItem
    extra = 0
    fields = ("product", "variant", "quantity", "unit_price_display", "line_total_display", "created_at")
    readonly_fields = ("unit_price_display", "line_total_display", "created_at")

    @admin.display(description="Unit Price (₹)")
    def unit_price_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        price = obj.variant.effective_price if obj.variant else obj.product.price
        return f"₹{price:,.2f}"

    @admin.display(description="Line Total (₹)")
    def line_total_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        price = obj.variant.effective_price if obj.variant else obj.product.price
        return f"₹{price * obj.quantity:,.2f}"


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    """
    Admin configuration for customer Shopping Carts.
    """
    list_display = ("id", "user", "items_count", "total_units", "cart_total_display", "updated_at", "created_at")
    list_filter = ("created_at", "updated_at")
    search_fields = ("user__username", "user__email", "id")
    readonly_fields = ("created_at", "updated_at", "cart_total_display", "total_units")
    inlines = [CartItemInline]

    @admin.display(description="Line Items")
    def items_count(self, obj):
        return obj.items.count()

    @admin.display(description="Total Units")
    def total_units(self, obj):
        return sum(item.quantity for item in obj.items.all())

    @admin.display(description="Cart Subtotal (₹)")
    def cart_total_display(self, obj):
        total = sum(
            (item.variant.effective_price if item.variant else item.product.price) * item.quantity
            for item in obj.items.select_related("product", "variant")
        )
        return f"₹{total:,.2f}"


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    """Admin configuration for customer profiles."""
    list_display = ("id", "user", "phone_number", "mobile_number", "created_at", "updated_at")
    search_fields = ("user__username", "user__email", "user__first_name", "user__last_name", "phone_number", "mobile_number")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    """Admin configuration for saved delivery addresses."""
    list_display = ("id", "user", "full_name", "city", "state", "postal_code", "address_type", "is_default", "updated_at")
    list_filter = ("address_type", "is_default", "state", "created_at")
    search_fields = ("user__username", "user__email", "full_name", "phone_number", "city", "postal_code")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    """
    Django Admin management for Customer Product Reviews.
    Features moderation workflows (approve, reject), rating filters,
    and safe customer display.
    """
    list_display = (
        "id",
        "product",
        "user_display",
        "rating_stars",
        "title",
        "status_badge",
        "created_at",
    )
    list_filter = (
        "status",
        "rating",
        "created_at",
    )
    search_fields = (
        "product__name",
        "product__sku",
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
        "title",
        "comment",
    )
    list_select_related = ("product", "user")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")
    actions = ["approve_reviews", "reject_reviews", "mark_pending"]

    @admin.display(description="Customer")
    def user_display(self, obj):
        return f"{obj.user.email} ({obj.customer_display_name})"

    @admin.display(description="Rating")
    def rating_stars(self, obj):
        return "★" * obj.rating + "☆" * (5 - obj.rating)

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            ProductReview.STATUS_APPROVED: "#198754",
            ProductReview.STATUS_PENDING: "#fd7e14",
            ProductReview.STATUS_REJECTED: "#dc3545",
        }
        color = colors.get(obj.status, "#6c757d")
        return format_html(
            '<span style="font-weight: bold; color: {};">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.action(description="Approve selected reviews")
    def approve_reviews(self, request, queryset):
        updated = queryset.update(status=ProductReview.STATUS_APPROVED)
        self.message_user(request, f"{updated} review(s) successfully marked as Approved.")

    @admin.action(description="Reject selected reviews")
    def reject_reviews(self, request, queryset):
        updated = queryset.update(status=ProductReview.STATUS_REJECTED)
        self.message_user(request, f"{updated} review(s) successfully marked as Rejected.")

    @admin.action(description="Mark selected reviews as Pending Moderation")
    def mark_pending(self, request, queryset):
        updated = queryset.update(status=ProductReview.STATUS_PENDING)
        self.message_user(request, f"{updated} review(s) set to Pending Moderation.")


@admin.register(WishlistItem)
class WishlistItemAdmin(admin.ModelAdmin):
    """
    Django Admin interface for Customer Wishlist items.
    """
    list_display = ("id", "user", "product", "product_sku", "product_stock", "created_at")
    list_filter = ("created_at",)
    search_fields = (
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
        "product__name",
        "product__sku",
    )
    list_select_related = ("user", "product")
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)

    @admin.display(description="SKU")
    def product_sku(self, obj):
        return obj.product.sku

    @admin.display(description="Stock")
    def product_stock(self, obj):
        if not obj.product.is_active:
            return "Inactive"
        return f"{obj.product.available_inventory} units"


class SellerOrderInline(admin.TabularInline):
    """
    Inline seller sub-orders / packages inside parent Order admin details.
    """
    model = SellerOrder
    extra = 0
    fields = (
        "sub_order_number",
        "seller",
        "is_platform_fulfilled",
        "status",
        "subtotal",
        "customer_shipping_charge",
        "total_commission",
        "net_seller_payable",
    )
    readonly_fields = (
        "sub_order_number",
        "seller",
        "is_platform_fulfilled",
        "subtotal",
        "customer_shipping_charge",
        "total_commission",
        "net_seller_payable",
    )


class OrderItemInline(admin.TabularInline):
    """
    Inline order items table inside Order admin details.
    Displays purchase snapshot data with read-only integrity.
    """
    model = OrderItem
    extra = 0
    fields = (
        "product_name",
        "variant_label",
        "sku",
        "unit_price_display",
        "quantity",
        "line_total_display",
        "seller",
        "seller_order",
        "commission_rate",
        "commission_amount",
        "product",
        "variant",
    )
    readonly_fields = (
        "product_name",
        "variant_label",
        "sku",
        "unit_price_display",
        "quantity",
        "line_total_display",
        "seller",
        "seller_order",
        "commission_rate",
        "commission_amount",
    )

    @admin.display(description="Unit Price (₹)")
    def unit_price_display(self, obj):
        return f"₹{obj.unit_price:,.2f}" if obj and obj.pk else "—"

    @admin.display(description="Line Total (₹)")
    def line_total_display(self, obj):
        return f"₹{obj.line_total:,.2f}" if obj and obj.pk else "—"


@admin.register(SellerOrder)
class SellerOrderAdmin(admin.ModelAdmin):
    """
    Django Admin management for Seller Sub-Orders / Packages.
    Platform administrators can inspect and track every vendor parcel.
    """
    list_display = (
        "sub_order_number",
        "order_link",
        "seller_display",
        "is_platform_fulfilled",
        "status",
        "dimension_status_display",
        "chargeable_weight_display",
        "subtotal",
        "customer_shipping_charge",
        "total_commission",
        "net_seller_payable",
        "created_at",
    )
    list_filter = (
        "status",
        "package_dimension_status",
        "is_platform_fulfilled",
        "seller",
        "created_at",
    )
    search_fields = (
        "sub_order_number",
        "order__order_number",
        "seller__store_name",
        "courier_name",
        "tracking_number",
        "shiprocket_order_id",
        "shiprocket_shipment_id",
        "shiprocket_awb_code",
    )
    readonly_fields = (
        "order",
        "seller",
        "is_platform_fulfilled",
        "sub_order_number",
        "subtotal",
        "total_commission",
        "net_seller_payable",
        "customer_shipping_charge",
        "actual_shipping_cost",
        "shipping_allocated_to_seller",
        "shipping_margin",
        "package_dimension_status",
        "estimated_dimensions_display",
        "actual_dimensions_display",
        "chargeable_weight_display",
        "pickup_location_display",
        "courier_name",
        "tracking_number",
        "tracking_url",
        "shiprocket_order_id",
        "shiprocket_shipment_id",
        "shiprocket_awb_code",
        "dimensions_verified_at",
        "dimensions_verified_by",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        ("Package Identity & Seller", {
            "fields": (
                "sub_order_number",
                "order",
                "seller",
                "is_platform_fulfilled",
                "status",
            )
        }),
        ("Merchandise Financials", {
            "fields": (
                "subtotal",
                "total_commission",
                "net_seller_payable",
            )
        }),
        ("Shipping & Logistics Financials", {
            "fields": (
                "customer_shipping_charge",
                "actual_shipping_cost",
                "shipping_allocated_to_seller",
                "shipping_margin",
            )
        }),
        ("Package Physical Dimensions & Measurements", {
            "fields": (
                "package_dimension_status",
                "estimated_dimensions_display",
                ("estimated_weight_grams", "estimated_length_cm", "estimated_breadth_cm", "estimated_height_cm"),
                "actual_dimensions_display",
                ("actual_weight_grams", "actual_length_cm", "actual_breadth_cm", "actual_height_cm"),
                ("dimensions_verified_at", "dimensions_verified_by"),
                "chargeable_weight_display",
            )
        }),
        ("Pickup Location & Facility Verification", {
            "fields": (
                "pickup_location_display",
            )
        }),
        ("Courier Dispatch & Provider Tracking", {
            "fields": (
                "courier_name",
                "tracking_number",
                "tracking_url",
                "shiprocket_order_id",
                "shiprocket_shipment_id",
                "shiprocket_awb_code",
            )
        }),
        ("Audit Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    ordering = ("-created_at",)

    @admin.display(description="Dimension Tier")
    def dimension_status_display(self, obj):
        return obj.get_package_dimension_status_display()

    @admin.display(description="Estimated Dimensions")
    def estimated_dimensions_display(self, obj):
        if obj.estimated_length_cm and obj.estimated_breadth_cm and obj.estimated_height_cm:
            return f"{obj.estimated_length_cm} × {obj.estimated_breadth_cm} × {obj.estimated_height_cm} cm (Est. Wt: {obj.estimated_weight_grams} g)"
        elif obj.estimated_weight_grams:
            return f"Est. Wt: {obj.estimated_weight_grams} g (Bounding dimensions missing)"
        return "Unknown"

    @admin.display(description="Actual Packed Carton")
    def actual_dimensions_display(self, obj):
        if obj.actual_length_cm and obj.actual_breadth_cm and obj.actual_height_cm:
            return f"{obj.actual_length_cm} × {obj.actual_breadth_cm} × {obj.actual_height_cm} cm (Actual Wt: {obj.actual_weight_grams} g)"
        return "Not verified / No packed measurements recorded"

    @admin.display(description="Chargeable Wt (kg)")
    def chargeable_weight_display(self, obj):
        cw = obj.get_chargeable_weight_kg()
        if cw is not None:
            dw = obj.get_dead_weight_kg() or Decimal("0.000")
            vw = obj.get_volumetric_weight_kg() or Decimal("0.000")
            return f"{cw:.3f} kg (Dead: {dw:.3f}kg, Vol: {vw:.3f}kg)"
        return "Unknown"

    @admin.display(description="Pickup Facility Verification")
    def pickup_location_display(self, obj):
        details = obj.get_pickup_location_details()
        status = details.get("status", "unknown").upper()
        source = details.get("source", "unknown")
        city = details.get("city") or details.get("pickup_city") or ""
        pin = details.get("postal_code") or details.get("pickup_postal_code") or ""
        errs = details.get("errors", [])
        err_text = f" | Issues: {', '.join(errs)}" if errs else ""
        return f"[{status}] Source: {source} ({city} - {pin}){err_text}"

    @admin.display(description="Parent Order")
    def order_link(self, obj):
        return obj.order.order_number

    @admin.display(description="Seller")
    def seller_display(self, obj):
        if obj.is_platform_fulfilled or not obj.seller:
            return "Fitmeezo Direct"
        return obj.seller.store_name

    actions = ["check_shiprocket_rates_action"]

    @admin.action(description="Check Shiprocket serviceability & non-binding rate estimates")
    def check_shiprocket_rates_action(self, request, queryset):
        """
        Staff action to query courier serviceability and non-binding rate estimates
        for selected seller packages.
        Strictly read-only; never touches financial columns or creates Shiprocket orders.
        """
        from core.shiprocket import estimate_package_shipping_rate, ShiprocketError

        success_count = 0
        error_count = 0
        for pkg in queryset:
            try:
                res = estimate_package_shipping_rate(pkg)
                if res.is_serviceable and res.cheapest_courier:
                    self.message_user(
                        request,
                        f"[Package #{pkg.sub_order_number}] Serviceable! Cheapest: {res.cheapest_courier.courier_name} "
                        f"at ₹{res.cheapest_courier.rate} ({len(res.available_couriers)} couriers available). "
                        f"NOTE: Estimate only, actual courier expense is unchanged.",
                        messages.INFO,
                    )
                    success_count += 1
                else:
                    self.message_user(
                        request,
                        f"[Package #{pkg.sub_order_number}] Not serviceable or no rates: {res.message}",
                        messages.WARNING,
                    )
                    error_count += 1
            except ShiprocketError as err:
                self.message_user(
                    request,
                    f"[Package #{pkg.sub_order_number}] Rate check failed: {err.user_message}",
                    messages.ERROR,
                )
                error_count += 1
            except Exception:
                self.message_user(
                    request,
                    f"[Package #{pkg.sub_order_number}] Unexpected error during rate check.",
                    messages.ERROR,
                )
                error_count += 1




class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    can_delete = False
    readonly_fields = ("from_status", "to_status", "changed_by", "notes", "created_at")


@admin.register(OrderStatusHistory)
class OrderStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ("order", "from_status", "to_status", "changed_by", "created_at")
    list_filter = ("to_status", "created_at")
    search_fields = ("order__order_number", "notes", "changed_by__username")
    readonly_fields = ("order", "from_status", "to_status", "changed_by", "notes", "created_at")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """
    Django Admin management for Customer Orders.
    Includes status workflow management, COD payment tracking,
    immutable address/item snapshots, and comprehensive filters.
    """
    list_display = (
        "order_number",
        "user_display",
        "status",
        "payment_method_display",
        "payment_status",
        "total_amount_display",
        "items_count",
        "created_at",
    )
    list_editable = (
        "status",
        "payment_status",
    )
    list_filter = (
        "status",
        "payment_status",
        "payment_method",
        "created_at",
    )
    search_fields = (
        "order_number",
        "razorpay_order_id",
        "razorpay_payment_id",
        "user__username",
        "user__email",
        "shipping_full_name",
        "shipping_phone_number",
        "shipping_city",
        "shipping_postal_code",
    )
    list_select_related = ("user",)
    ordering = ("-created_at",)
    readonly_fields = (
        "order_number",
        "created_at",
        "updated_at",
        "subtotal",
        "shipping_fee",
        "discount_amount",
        "total_amount",
        "idempotency_key",
        "formatted_shipping_address",
        "razorpay_order_id",
        "razorpay_payment_id",
        "razorpay_signature",
        "paid_at",
        "confirmed_at",
        "packed_at",
        "shipped_at",
        "delivered_at",
    )
    inlines = [SellerOrderInline, OrderItemInline, OrderStatusHistoryInline]
    actions = [
        "mark_processing",
        "mark_shipped",
        "mark_delivered",
        "mark_cancelled",
        "mark_payment_paid",
        "mark_payment_unpaid",
    ]

    fieldsets = (
        ("Order Identification", {
            "fields": (
                ("order_number", "user"),
                ("status", "payment_status"),
                ("created_at", "updated_at"),
                "idempotency_key",
            )
        }),
        ("Financial Summary", {
            "fields": (
                ("subtotal", "shipping_fee"),
                ("discount_amount", "total_amount"),
                ("payment_method", "currency"),
            )
        }),
        ("Online Payment Gateway (Razorpay)", {
            "fields": (
                ("razorpay_order_id", "razorpay_payment_id"),
                "razorpay_signature",
                "paid_at",
            ),
            "classes": ("collapse",),
        }),
        ("Delivery Destination Snapshot", {
            "fields": (
                ("shipping_full_name", "shipping_phone_number"),
                "shipping_address_line1",
                "shipping_address_line2",
                ("shipping_city", "shipping_state", "shipping_postal_code"),
                ("shipping_landmark", "shipping_address_type"),
                "formatted_shipping_address",
            )
        }),
        ("Customer Instructions & Notes", {
            "fields": ("customer_notes",),
            "classes": ("collapse",),
        }),
    )

    def save_model(self, request, obj, form, change):
        if obj.payment_status == Order.PAYMENT_STATUS_PAID:
            if obj.payment_method == Order.PAYMENT_METHOD_RAZORPAY and not obj.razorpay_payment_id:
                from django.core.exceptions import ValidationError
                raise ValidationError(
                    "Online (Razorpay) orders cannot be marked as Paid without a verified Razorpay Payment ID."
                )
            if not obj.paid_at:
                obj.paid_at = timezone.now()
        super().save_model(request, obj, form, change)

    @admin.display(description="Customer")
    def user_display(self, obj):
        return f"{obj.user.username} ({obj.shipping_full_name})"

    @admin.display(description="Order Status")
    def status_badge(self, obj):
        color_map = {
            Order.STATUS_PENDING: "#d97706",
            Order.STATUS_PROCESSING: "#2563eb",
            Order.STATUS_SHIPPED: "#7c3aed",
            Order.STATUS_DELIVERED: "#16a34a",
            Order.STATUS_CANCELLED: "#dc2626",
        }
        color = color_map.get(obj.status, "#4b5563")
        return format_html(
            '<span style="font-weight: 600; color: {};">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Payment Method")
    def payment_method_display(self, obj):
        return obj.get_payment_method_display()

    @admin.display(description="Payment")
    def payment_status_badge(self, obj):
        color_map = {
            Order.PAYMENT_STATUS_UNPAID: "#dc2626",
            Order.PAYMENT_STATUS_PAID: "#16a34a",
            Order.PAYMENT_STATUS_FAILED: "#991b1b",
            Order.PAYMENT_STATUS_REFUNDED: "#6b7280",
        }
        color = color_map.get(obj.payment_status, "#4b5563")
        return format_html(
            '<span style="font-weight: 600; color: {};">{}</span>',
            color,
            obj.get_payment_status_display(),
        )

    @admin.display(description="Total (₹)")
    def total_amount_display(self, obj):
        return f"₹{obj.total_amount:,.2f}"

    @admin.display(description="Items")
    def items_count(self, obj):
        return obj.items.count()

    @admin.display(description="Full Delivery Address")
    def formatted_shipping_address(self, obj):
        return obj.shipping_address_formatted

    @admin.action(description="Mark selected orders as Processing")
    def mark_processing(self, request, queryset):
        updated = queryset.update(status=Order.STATUS_PROCESSING)
        self.message_user(request, f"{updated} order(s) updated to Processing.")

    @admin.action(description="Mark selected orders as Shipped")
    def mark_shipped(self, request, queryset):
        updated = queryset.update(status=Order.STATUS_SHIPPED)
        self.message_user(request, f"{updated} order(s) updated to Shipped.")

    @admin.action(description="Mark selected orders as Delivered")
    def mark_delivered(self, request, queryset):
        updated = queryset.update(status=Order.STATUS_DELIVERED)
        self.message_user(request, f"{updated} order(s) updated to Delivered.")

    @admin.action(description="Mark selected orders as Cancelled")
    def mark_cancelled(self, request, queryset):
        updated = queryset.update(status=Order.STATUS_CANCELLED)
        self.message_user(request, f"{updated} order(s) marked as Cancelled.")

    @admin.action(description="Mark payment as Paid (Documented COD Collection / Verified Gateway)")
    def mark_payment_paid(self, request, queryset):
        success_count = 0
        skipped_count = 0
        for order in queryset:
            # Online orders cannot be marked as Paid without a verified Razorpay payment ID
            if order.payment_method == Order.PAYMENT_METHOD_RAZORPAY and not order.razorpay_payment_id:
                skipped_count += 1
                continue
            order.payment_status = Order.PAYMENT_STATUS_PAID
            if not order.paid_at:
                order.paid_at = timezone.now()
            order.save(update_fields=["payment_status", "paid_at", "updated_at"])
            success_count += 1

        if success_count > 0:
            self.message_user(request, f"{success_count} order(s) marked as Paid.")
        if skipped_count > 0:
            self.message_user(
                request,
                f"{skipped_count} online order(s) skipped: Online payments cannot be marked Paid without a verified Razorpay Payment ID.",
                level=messages.WARNING,
            )

    @admin.action(description="Mark payment as Unpaid")
    def mark_payment_unpaid(self, request, queryset):
        updated = queryset.update(payment_status=Order.PAYMENT_STATUS_UNPAID)
        self.message_user(request, f"{updated} order(s) marked as Unpaid.")


class SellerUserChoiceField(forms.ModelChoiceField):
    """
    Robust User selector field for Django Admin Seller management.
    Resolves users flexibly by primary key (ID), username, or email,
    while enforcing clear error messaging instead of ambiguous choice rejections.
    """

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if isinstance(value, self.queryset.model):
            return value

        val_str = str(value).strip()
        user = None

        if val_str.isdigit():
            user = User.objects.filter(pk=int(val_str)).first()

        if not user:
            user = User.objects.filter(
                Q(username__iexact=val_str) | Q(email__iexact=val_str)
            ).first()

        if not user:
            raise ValidationError(
                f"User '{val_str}' does not exist. Please select or enter a valid registered user.",
                code="invalid_choice",
            )

        return user


class SellerAdminForm(forms.ModelForm):
    """
    Admin ModelForm for Seller management.
    Provides consistent User queryset scoping across GET and POST:
    - Filters to eligible active non-staff accounts without an existing Seller profile.
    - Preserves existing seller user linkage during update/edit workflows.
    - Emits clear, actionable validation messages when an ineligible account is chosen.
    - Gracefully auto-populates courier dispatch facility from registered business address if omitted.
    - Auto-generates unique store slug if omitted.
    - Guides administrator on GSTIN requirement when setting status to Approved.
    """

    user = SellerUserChoiceField(
        queryset=User.objects.all(),
        help_text="Select an eligible non-staff customer account for this seller profile.",
    )
    pickup_address = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text="Warehouse/pickup address where couriers collect shipments. If left blank, defaults to registered business address.",
    )
    pickup_city = forms.CharField(
        max_length=100,
        required=False,
        help_text="Pickup city. If left blank, defaults to registered city.",
    )
    pickup_state = forms.CharField(
        max_length=100,
        required=False,
        help_text="Pickup state. If left blank, defaults to registered state.",
    )
    pickup_postal_code = forms.CharField(
        max_length=10,
        required=False,
        help_text="Pickup PIN/postal code. If left blank, defaults to registered postal code.",
    )

    class Meta:
        model = Seller
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "user" in self.fields:
            current_user_id = self.instance.user_id if self.instance and self.instance.pk else None
            eligible_filter = Q(
                is_staff=False,
                is_superuser=False,
                is_active=True,
                seller_profile__isnull=True,
            )
            if current_user_id:
                eligible_filter |= Q(pk=current_user_id)

            self.fields["user"].queryset = (
                User.objects.filter(eligible_filter).exclude(username="").order_by("username")
            )
            self.fields["user"].label_from_instance = (
                lambda u: f"{u.username} ({u.email})" if u.email else u.username
            )

    def clean_user(self):
        user = self.cleaned_data.get("user")
        if not user:
            raise ValidationError("Please select an eligible customer account for this seller profile.")

        if not user.is_active:
            raise ValidationError("Inactive users cannot be assigned as sellers.")

        if user.is_staff or user.is_superuser:
            raise ValidationError(
                "Staff members and superusers cannot be assigned as sellers. "
                "Seller accounts must be standard customer accounts."
            )

        dup_qs = Seller.objects.filter(user=user)
        if self.instance and self.instance.pk:
            dup_qs = dup_qs.exclude(pk=self.instance.pk)
        if dup_qs.exists():
            raise ValidationError(
                f"User '{user.username}' is already linked to another seller store. "
                "Each user can only have one seller profile."
            )

        return user

    def clean_slug(self):
        slug = (self.cleaned_data.get("slug") or "").strip().lower()
        store_name = (self.cleaned_data.get("store_name") or "").strip()
        if not slug and store_name:
            base_slug = slugify(store_name)
            slug = base_slug
            counter = 1
            qs = Seller.objects.filter(slug=slug)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            while qs.exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
                qs = Seller.objects.filter(slug=slug)
                if self.instance and self.instance.pk:
                    qs = qs.exclude(pk=self.instance.pk)
        return slug

    def clean_status(self):
        status = self.cleaned_data.get("status")
        gstin = (self.cleaned_data.get("gstin") or "").strip()
        if status == Seller.STATUS_APPROVED and not gstin:
            raise ValidationError(
                "A seller cannot be approved without a valid 15-character GSTIN. "
                "Please enter a valid GSTIN or set status to 'Pending Approval'."
            )
        return status

    def clean(self):
        cleaned_data = super().clean()
        reg_addr = (cleaned_data.get("registered_address") or "").strip()
        reg_city = (cleaned_data.get("city") or "").strip()
        reg_state = (cleaned_data.get("state") or "").strip()
        reg_postal = (cleaned_data.get("postal_code") or "").strip()

        # Seamless auto-fallback: if courier pickup facility is omitted, inherit registered address
        if not (cleaned_data.get("pickup_address") or "").strip() and reg_addr:
            cleaned_data["pickup_address"] = reg_addr
        if not (cleaned_data.get("pickup_city") or "").strip() and reg_city:
            cleaned_data["pickup_city"] = reg_city
        if not (cleaned_data.get("pickup_state") or "").strip() and reg_state:
            cleaned_data["pickup_state"] = reg_state
        if not (cleaned_data.get("pickup_postal_code") or "").strip() and reg_postal:
            cleaned_data["pickup_postal_code"] = reg_postal

        # Enforce requirement if neither pickup nor registered address was provided
        if not (cleaned_data.get("pickup_address") or "").strip():
            self.add_error("pickup_address", "Pickup address is required (or provide a registered business address).")
        if not (cleaned_data.get("pickup_city") or "").strip():
            self.add_error("pickup_city", "Pickup city is required (or provide a registered city).")
        if not (cleaned_data.get("pickup_state") or "").strip():
            self.add_error("pickup_state", "Pickup state is required (or provide a registered state).")
        if not (cleaned_data.get("pickup_postal_code") or "").strip():
            self.add_error("pickup_postal_code", "Pickup postal code is required (or provide a registered postal code).")

        return cleaned_data


@admin.register(Seller)
class SellerAdmin(admin.ModelAdmin):
    """
    Django Admin interface for Fitmeezo Third-Party Sellers.
    Empowers platform administrators to review onboarding applications,
    validate Indian GSTIN/PAN credentials, inspect pickup facilities,
    and approve, reject, or suspend seller accounts.
    """

    form = SellerAdminForm

    list_display = (
        "store_name",
        "legal_business_name",
        "user_email",
        "registered_mobile",
        "business_type",
        "status_badge",
        "pickup_status_badge",
        "is_active",
        "commission_rate",
        "created_at",
    )
    list_filter = (
        "status",
        "pickup_location_verified",
        "business_type",
        "is_active",
        "created_at",
    )
    search_fields = (
        "store_name",
        "legal_business_name",
        "user__email",
        "user__username",
        "registered_mobile",
        "gstin",
        "pan_number",
        "support_phone",
        "support_email",
        "city",
        "state",
        "pickup_city",
        "pickup_state",
        "pickup_postal_code",
    )
    prepopulated_fields = {
        "slug": ("store_name",),
    }
    readonly_fields = (
        "created_at",
        "updated_at",
        "approved_at",
    )
    ordering = ("-created_at",)
    actions = ["approve_sellers", "reject_sellers", "suspend_sellers"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            object_id = (
                request.resolver_match.kwargs.get("object_id")
                if request.resolver_match
                else None
            )
            eligible_filter = Q(
                is_staff=False,
                is_superuser=False,
                is_active=True,
                seller_profile__isnull=True,
            )
            if object_id:
                seller = Seller.objects.filter(pk=object_id).first()
                if seller and seller.user_id:
                    eligible_filter |= Q(pk=seller.user_id)
            kwargs["queryset"] = (
                User.objects.filter(eligible_filter).exclude(username="").order_by("username")
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    fieldsets = (
        ("Store Identity & Ownership", {
            "fields": (
                "user",
                "store_name",
                "slug",
                "store_description",
            )
        }),
        ("Legal Entity & Business Details", {
            "fields": (
                "legal_business_name",
                "business_type",
                ("gstin", "pan_number"),
            )
        }),
        ("Contact Details", {
            "fields": (
                ("support_email", "support_phone"),
                "registered_mobile",
            )
        }),
        ("Registered Business Address", {
            "fields": (
                "registered_address",
                ("city", "state", "postal_code"),
            )
        }),
        ("Courier Dispatch & Pickup Facility", {
            "fields": (
                "pickup_address",
                ("pickup_city", "pickup_state", "pickup_postal_code"),
                "pickup_location_nickname",
                ("pickup_location_verified", "pickup_verification_notes"),
            )
        }),
        ("Platform Administration & Governance", {
            "fields": (
                ("status", "is_active"),
                "commission_rate",
                "approved_at",
                "admin_notes",
            )
        }),
        ("Audit Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
            ),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user")

    @admin.display(description="User Email")
    def user_email(self, obj):
        return obj.user.email or obj.user.username

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            Seller.STATUS_APPROVED: "#198754",   # Green
            Seller.STATUS_PENDING: "#fd7e14",    # Orange
            Seller.STATUS_REJECTED: "#dc3545",   # Red
            Seller.STATUS_SUSPENDED: "#6c757d",  # Grey
        }
        color = colors.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background-color: {}; color: #fff; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Pickup Facility Status")
    def pickup_status_badge(self, obj):
        res = obj.validate_pickup_location()
        status = res["status"]
        if status == "verified":
            color = "#198754"
            label = "Verified"
        elif status == "unverified":
            color = "#fd7e14"
            label = "Complete (Unverified)"
        else:
            color = "#dc3545"
            label = "Incomplete"
        return format_html(
            '<span style="background-color: {}; color: #fff; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 0.8rem;">{}</span>',
            color,
            label,
        )


    @admin.action(description="Approve selected sellers")
    def approve_sellers(self, request, queryset):
        count = 0
        now = timezone.now()
        for seller in queryset:
            if not seller.has_valid_gstin:
                self.message_user(
                    request,
                    f"Cannot approve '{seller.store_name}': A valid 15-character GSTIN is required before approval.",
                    messages.ERROR,
                )
                continue
            seller.status = Seller.STATUS_APPROVED
            seller.approved_at = now
            seller.is_active = True
            seller.save(update_fields=["status", "approved_at", "is_active", "updated_at"])
            count += 1
        self.message_user(
            request,
            f"Successfully approved {count} seller(s).",
            messages.SUCCESS,
        )

    @admin.action(description="Reject selected sellers")
    def reject_sellers(self, request, queryset):
        count = queryset.update(status=Seller.STATUS_REJECTED, is_active=False, updated_at=timezone.now())
        self.message_user(
            request,
            f"Marked {count} seller(s) as rejected.",
            messages.WARNING,
        )

    @admin.action(description="Suspend selected sellers")
    def suspend_sellers(self, request, queryset):
        count = queryset.update(status=Seller.STATUS_SUSPENDED, is_active=False, updated_at=timezone.now())
        self.message_user(
            request,
            f"Suspended {count} seller(s). Their products are hidden from customers.",
            messages.WARNING,
        )


@admin.register(ShipmentBooking)
class ShipmentBookingAdmin(admin.ModelAdmin):
    """
    Staff admin view for shipment bookings (Phase 5 Prompt 5B).
    Provides durable, read-only audit logs for mock and live shipment bookings.
    """
    list_display = (
        "shipment_id",
        "awb_code",
        "package_number",
        "courier_name",
        "mock_badge",
        "status",
        "created_at",
    )
    list_filter = ("is_mock", "status", "courier_name", "created_at")
    search_fields = (
        "shipment_id",
        "awb_code",
        "idempotency_key",
        "package__sub_order_number",
        "pickup_nickname",
    )
    readonly_fields = (
        "package",
        "idempotency_key",
        "is_mock",
        "status",
        "shipment_id",
        "awb_code",
        "courier_name",
        "courier_company_id",
        "chargeable_weight_kg",
        "estimated_rate",
        "pickup_nickname",
        "metadata",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Package")
    def package_number(self, obj):
        return obj.package.sub_order_number

    @admin.display(description="Mode")
    def mock_badge(self, obj):
        if obj.is_mock:
            return format_html(
                '<span style="background-color: #6c757d; color: #fff; padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">SIMULATED MOCK</span>'
            )
        return format_html(
            '<span style="background-color: #198754; color: #fff; padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">LIVE</span>'
        )


# ==============================================================================
# PHASE 6: RETURNS, EXCHANGES & REFUND MANAGEMENT ADMIN
# ==============================================================================

class ReturnRequestItemInline(admin.TabularInline):
    model = ReturnRequestItem
    extra = 0
    fields = (
        "order_item",
        "quantity",
        "item_unit_price",
        "refund_eligible_amount",
        "exchange_variant",
        "inspection_status",
        "restock_inventory",
        "inspection_notes",
    )
    readonly_fields = ("order_item", "item_unit_price", "refund_eligible_amount")


class ReturnAuditLogInline(admin.TabularInline):
    model = ReturnAuditLog
    extra = 0
    fields = ("created_at", "actor", "actor_role", "from_status", "to_status", "action", "notes")
    readonly_fields = ("created_at", "actor", "actor_role", "from_status", "to_status", "action", "notes")

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class RefundRecordInline(admin.TabularInline):
    model = RefundRecord
    extra = 0
    fields = (
        "refund_reference",
        "external_reference",
        "approved_amount",
        "commission_reversal",
        "seller_payable_deduction",
        "status",
        "is_mock",
        "processed_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = (
        "request_number",
        "customer",
        "package_number",
        "fulfillment_source",
        "request_type",
        "status_badge",
        "total_units",
        "total_refund_display",
        "inventory_restored",
        "created_at",
    )
    list_filter = (
        "status",
        "request_type",
        "is_platform_fulfilled",
        "inventory_restored",
        "created_at",
    )
    search_fields = (
        "request_number",
        "order__order_number",
        "seller_order__sub_order_number",
        "customer__username",
        "customer__email",
        "seller__store_name",
    )
    readonly_fields = (
        "request_number",
        "order",
        "seller_order",
        "seller",
        "is_platform_fulfilled",
        "customer",
        "created_at",
        "updated_at",
        "resolved_at",
    )
    inlines = [ReturnRequestItemInline, RefundRecordInline, ReturnAuditLogInline]
    actions = [
        "approve_selected_returns",
        "mark_pickup_pending",
        "mark_items_received",
        "complete_returns_and_restock",
        "issue_simulated_mock_refund",
    ]

    @admin.display(description="Package")
    def package_number(self, obj):
        return obj.seller_order.sub_order_number

    @admin.display(description="Fulfillment")
    def fulfillment_source(self, obj):
        if obj.is_platform_fulfilled:
            return format_html('<span style="color: #0d6efd; font-weight: 600;">Platform Direct</span>')
        return obj.seller.store_name if obj.seller else "Third-Party"

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "requested": "#ffc107",
            "approved": "#0dcaf0",
            "pickup_pending": "#0d6efd",
            "item_received": "#6c757d",
            "inspection": "#ffc107",
            "processing": "#6f42c1",
            "completed": "#198754",
            "rejected": "#dc3545",
            "cancelled": "#adb5bd",
        }
        c = colors.get(obj.status, "#6c757d")
        return format_html(
            f'<span style="background-color: {c}; color: #000; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">{obj.get_status_display()}</span>'
        )

    @admin.display(description="Units")
    def total_units(self, obj):
        return obj.total_quantity

    @admin.display(description="Refund Total")
    def total_refund_display(self, obj):
        return f"₹{obj.total_refund_amount}"

    @admin.action(description="Approve selected return requests")
    def approve_selected_returns(self, request, queryset):
        success_count = 0
        for r in queryset.filter(status=ReturnRequest.STATUS_REQUESTED):
            try:
                transition_return_status(
                    return_request=r,
                    new_status=ReturnRequest.STATUS_APPROVED,
                    actor=request.user,
                    actor_role="platform_admin",
                    notes="Approved by platform administrator.",
                )
                success_count += 1
            except ReturnServiceError as e:
                self.message_user(request, f"Error approving #{r.request_number}: {e}", level=messages.ERROR)
        if success_count:
            self.message_user(request, f"Successfully approved {success_count} return requests.", level=messages.SUCCESS)

    @admin.action(description="Mark return pickup scheduled (Pickup Pending)")
    def mark_pickup_pending(self, request, queryset):
        success_count = 0
        for r in queryset.filter(status=ReturnRequest.STATUS_APPROVED):
            try:
                transition_return_status(
                    return_request=r,
                    new_status=ReturnRequest.STATUS_PICKUP_PENDING,
                    actor=request.user,
                    actor_role="platform_admin",
                    notes="Scheduled pickup partner.",
                )
                success_count += 1
            except ReturnServiceError as e:
                self.message_user(request, f"Error on #{r.request_number}: {e}", level=messages.ERROR)
        if success_count:
            self.message_user(request, f"Updated {success_count} requests to Pickup Pending.", level=messages.SUCCESS)

    @admin.action(description="Mark returned items received (Item Received)")
    def mark_items_received(self, request, queryset):
        success_count = 0
        for r in queryset.filter(status=ReturnRequest.STATUS_PICKUP_PENDING):
            try:
                transition_return_status(
                    return_request=r,
                    new_status=ReturnRequest.STATUS_ITEM_RECEIVED,
                    actor=request.user,
                    actor_role="platform_admin",
                    notes="Merchandise physically received at facility.",
                )
                success_count += 1
            except ReturnServiceError as e:
                self.message_user(request, f"Error on #{r.request_number}: {e}", level=messages.ERROR)
        if success_count:
            self.message_user(request, f"Updated {success_count} requests to Item Received.", level=messages.SUCCESS)

    @admin.action(description="Complete return requests & restock eligible items")
    def complete_returns_and_restock(self, request, queryset):
        success_count = 0
        for r in queryset.filter(status__in=[ReturnRequest.STATUS_INSPECTION, ReturnRequest.STATUS_PROCESSING]):
            try:
                transition_return_status(
                    return_request=r,
                    new_status=ReturnRequest.STATUS_COMPLETED,
                    actor=request.user,
                    actor_role="platform_admin",
                    notes="Completed by platform administrator.",
                )
                success_count += 1
            except ReturnServiceError as e:
                self.message_user(request, f"Error completing #{r.request_number}: {e}", level=messages.ERROR)
        if success_count:
            self.message_user(request, f"Completed {success_count} return requests.", level=messages.SUCCESS)

    @admin.action(description="Issue simulated mock refund voucher")
    def issue_simulated_mock_refund(self, request, queryset):
        success_count = 0
        for r in queryset.filter(status__in=[ReturnRequest.STATUS_INSPECTION, ReturnRequest.STATUS_PROCESSING]):
            try:
                # Ensure in processing
                if r.status != ReturnRequest.STATUS_PROCESSING:
                    transition_return_status(
                        return_request=r,
                        new_status=ReturnRequest.STATUS_PROCESSING,
                        actor=request.user,
                        actor_role="platform_admin",
                        notes="Moving to processing for refund.",
                    )
                process_simulated_refund(
                    return_request=r,
                    actor=request.user,
                    reason=f"Admin issued simulated refund for Return #{r.request_number}",
                )
                success_count += 1
            except (ReturnServiceError, RefundError) as e:
                self.message_user(request, f"Error issuing refund for #{r.request_number}: {e}", level=messages.ERROR)
        if success_count:
            self.message_user(request, f"Issued {success_count} mock refund vouchers.", level=messages.SUCCESS)


@admin.register(RefundRecord)
class RefundRecordAdmin(admin.ModelAdmin):
    list_display = (
        "refund_reference",
        "external_reference",
        "order_number",
        "return_request_number",
        "approved_amount",
        "commission_reversal",
        "seller_payable_deduction",
        "payment_method",
        "status_badge",
        "mock_badge",
        "created_at",
    )
    list_filter = ("status", "is_mock", "payment_method", "created_at")
    search_fields = (
        "refund_reference",
        "external_reference",
        "order__order_number",
        "return_request__request_number",
    )
    readonly_fields = (
        "refund_reference",
        "external_reference",
        "order",
        "return_request",
        "seller_order",
        "requested_amount",
        "approved_amount",
        "commission_reversal",
        "seller_payable_deduction",
        "currency",
        "payment_method",
        "status",
        "is_mock",
        "processed_by",
        "processed_at",
        "reason",
        "audit_note",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Order")
    def order_number(self, obj):
        return obj.order.order_number

    @admin.display(description="Return Request")
    def return_request_number(self, obj):
        return obj.return_request.request_number if obj.return_request else "—"

    @admin.display(description="Status")
    def status_badge(self, obj):
        color = "#198754" if obj.status == "processed" else "#6c757d"
        return format_html(
            f'<span style="background-color: {color}; color: #fff; padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">{obj.get_status_display()}</span>'
        )

    @admin.display(description="Mode")
    def mock_badge(self, obj):
        if obj.is_mock:
            return format_html(
                '<span style="background-color: #6c757d; color: #fff; padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">SIMULATED MOCK</span>'
            )
        return format_html(
            '<span style="background-color: #198754; color: #fff; padding: 2px 6px; border-radius: 4px; font-weight: 600; font-size: 0.75rem;">LIVE</span>'
        )


@admin.register(ReturnAuditLog)
class ReturnAuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "return_request_display",
        "actor_display",
        "actor_role",
        "from_status",
        "to_status",
        "action",
        "notes_preview",
    )
    list_filter = ("actor_role", "action", "created_at")
    search_fields = (
        "return_request__request_number",
        "actor__username",
        "action",
        "notes",
    )
    readonly_fields = (
        "return_request",
        "actor",
        "actor_role",
        "from_status",
        "to_status",
        "action",
        "notes",
        "metadata",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Return Request")
    def return_request_display(self, obj):
        return obj.return_request.request_number

    @admin.display(description="Actor")
    def actor_display(self, obj):
        return obj.actor.username if obj.actor else "System"

    @admin.display(description="Notes")
    def notes_preview(self, obj):
        return (obj.notes[:50] + "...") if len(obj.notes) > 50 else obj.notes


@admin.register(SellerLedgerEntry)
class SellerLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "idempotency_key",
        "seller_display",
        "entry_type_badge",
        "category_badge",
        "amount_display",
        "status_badge",
        "source_reference",
        "created_at",
    )
    list_filter = (
        "entry_type",
        "category",
        "status",
        "is_platform_fulfilled",
        "created_at",
    )
    search_fields = (
        "idempotency_key",
        "source_reference",
        "description",
        "seller__store_name",
        "order__order_number",
        "seller_order__sub_order_number",
    )
    readonly_fields = (
        "seller",
        "is_platform_fulfilled",
        "entry_type",
        "category",
        "amount",
        "currency",
        "status",
        "source_type",
        "source_reference",
        "order",
        "seller_order",
        "refund_record",
        "payout_batch",
        "idempotency_key",
        "description",
        "created_at",
        "posted_at",
        "created_by",
        "audit_metadata",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Merchant")
    def seller_display(self, obj):
        if obj.is_platform_fulfilled or not obj.seller:
            return format_html('<span style="color:#2563eb;font-weight:600;">Platform Store</span>')
        return obj.seller.store_name

    @admin.display(description="Type")
    def entry_type_badge(self, obj):
        if obj.entry_type == SellerLedgerEntry.TYPE_CREDIT:
            return format_html('<span style="color:#16a34a;font-weight:700;">+ CREDIT</span>')
        return format_html('<span style="color:#dc2626;font-weight:700;">- DEBIT</span>')

    @admin.display(description="Category")
    def category_badge(self, obj):
        return format_html(
            '<span style="background:#f1f5f9;color:#334155;padding:2px 8px;border-radius:4px;font-size:12px;">{}</span>',
            obj.get_category_display(),
        )

    @admin.display(description="Amount")
    def amount_display(self, obj):
        sign = "+" if obj.entry_type == SellerLedgerEntry.TYPE_CREDIT else "-"
        color = "#16a34a" if obj.entry_type == SellerLedgerEntry.TYPE_CREDIT else "#dc2626"
        return format_html(
            '<span style="color:{};font-weight:600;">{}₹{}</span>',
            color,
            sign,
            obj.amount,
        )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            SellerLedgerEntry.STATUS_POSTED: ("#dcfce7", "#166534"),
            SellerLedgerEntry.STATUS_PENDING_COD: ("#fef9c3", "#854d0e"),
            SellerLedgerEntry.STATUS_REVERSED: ("#fee2e2", "#991b1b"),
        }
        bg, fg = colors.get(obj.status, ("#f1f5f9", "#334155"))
        return format_html(
            '<span style="background:{};color:{};padding:2px 8px;border-radius:4px;font-weight:600;font-size:11px;">{}</span>',
            bg,
            fg,
            obj.get_status_display(),
        )


class PayoutAuditLogInline(admin.TabularInline):
    model = PayoutAuditLog
    extra = 0
    readonly_fields = ("action", "actor", "from_status", "to_status", "reason", "created_at")
    can_delete = False


@admin.register(PayoutBatch)
class PayoutBatchAdmin(admin.ModelAdmin):
    list_display = (
        "batch_reference",
        "seller_display",
        "amount_display",
        "status_badge",
        "payout_cycle",
        "external_reference",
        "eligible_entries_count",
        "is_mock_badge",
        "created_at",
    )
    list_filter = (
        "status",
        "payout_cycle",
        "is_platform_fulfilled",
        "payout_method",
        "created_at",
    )
    search_fields = (
        "batch_reference",
        "external_reference",
        "seller__store_name",
        "notes",
        "hold_reason",
        "rejection_reason",
    )
    readonly_fields = (
        "batch_reference",
        "seller",
        "is_platform_fulfilled",
        "amount",
        "currency",
        "payout_cycle",
        "eligible_entries_count",
        "period_start",
        "period_end",
        "external_reference",
        "processed_at",
        "processed_by",
        "held_at",
        "held_by",
        "hold_reason",
        "released_at",
        "released_by",
        "approved_at",
        "approved_by",
        "rejected_at",
        "rejected_by",
        "rejection_reason",
        "provider_status",
        "retry_count",
        "is_mock",
        "created_at",
        "updated_at",
        "audit_metadata",
    )
    inlines = [PayoutAuditLogInline]

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Merchant")
    def seller_display(self, obj):
        if obj.is_platform_fulfilled or not obj.seller:
            return format_html('<span style="color:#2563eb;font-weight:600;">Platform Store</span>')
        return obj.seller.store_name

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return format_html('<span style="font-weight:700;">₹{}</span>', obj.amount)

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            PayoutBatch.STATUS_DRAFT: ("#f1f5f9", "#334155"),
            PayoutBatch.STATUS_PROCESSING: ("#dbeafe", "#1e40af"),
            PayoutBatch.STATUS_COMPLETED: ("#dcfce7", "#166534"),
            PayoutBatch.STATUS_FAILED: ("#fee2e2", "#991b1b"),
            PayoutBatch.STATUS_REVERSED: ("#ede9fe", "#5b21b6"),
            PayoutBatch.STATUS_ON_HOLD: ("#fef3c7", "#92400e"),
        }
        bg, fg = colors.get(obj.status, ("#f1f5f9", "#334155"))
        return format_html(
            '<span style="background:{};color:{};padding:2px 8px;border-radius:4px;font-weight:600;font-size:11px;">{}</span>',
            bg,
            fg,
            obj.get_status_display(),
        )

    @admin.display(description="Simulation")
    def is_mock_badge(self, obj):
        return format_html('<span style="background:#e0f2fe;color:#0369a1;padding:2px 6px;border-radius:4px;font-size:11px;">SIMULATED</span>')


@admin.register(PayoutAuditLog)
class PayoutAuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "payout_batch",
        "action",
        "actor",
        "from_status",
        "to_status",
        "created_at",
    )
    list_filter = ("action", "created_at")
    search_fields = ("payout_batch__batch_reference", "reason", "actor__username")
    readonly_fields = (
        "payout_batch",
        "action",
        "actor",
        "from_status",
        "to_status",
        "reason",
        "metadata",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CODReceiptRecord)
class CODReceiptRecordAdmin(admin.ModelAdmin):
    list_display = (
        "remittance_reference",
        "seller_order_display",
        "collected_amount_display",
        "courier_name",
        "remitted_date",
        "reconciled_by_display",
        "reconciled_at",
    )
    search_fields = (
        "remittance_reference",
        "seller_order__sub_order_number",
        "order__order_number",
        "courier_name",
        "notes",
    )
    readonly_fields = (
        "remittance_reference",
        "seller_order",
        "order",
        "collected_amount",
        "courier_name",
        "remitted_date",
        "reconciled_by",
        "reconciled_at",
        "audit_metadata",
    )

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Package Number")
    def seller_order_display(self, obj):
        return obj.seller_order.sub_order_number

    @admin.display(description="Remitted Cash")
    def collected_amount_display(self, obj):
        return format_html('<span style="font-weight:700;color:#16a34a;">₹{}</span>', obj.collected_amount)

    @admin.display(description="Verified By")
    def reconciled_by_display(self, obj):
        return obj.reconciled_by.username if obj.reconciled_by else "System"


@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(admin.ModelAdmin):
    list_display = ("prefix", "last_number", "updated_at")
    readonly_fields = ("prefix", "last_number", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "invoice_type",
        "order_display",
        "recipient_name",
        "total_amount_display",
        "status",
        "issued_at",
    )
    list_filter = ("invoice_type", "status", "issued_at")
    search_fields = (
        "invoice_number",
        "order__order_number",
        "seller_order__sub_order_number",
        "recipient_name",
        "issuer_name",
    )
    readonly_fields = [f.name for f in Invoice._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Order Reference")
    def order_display(self, obj):
        return f"#{obj.order.order_number}"

    @admin.display(description="Invoice Total")
    def total_amount_display(self, obj):
        return format_html('<span style="font-weight:700;color:#2563eb;">₹{}</span>', obj.total_amount)


@admin.register(SellerLoginOTP)
class SellerLoginOTPAdmin(admin.ModelAdmin):
    """
    Audit-only admin view for Seller Mobile Login OTPs.
    Plaintext OTPs are never stored; only cryptographic hashes are held.
    Manual creation and editing are disabled to preserve security.
    """
    list_display = (
        "seller_store",
        "masked_mobile",
        "created_at",
        "expires_at",
        "is_used",
        "attempts_count",
        "ip_address",
    )
    list_filter = ("is_used", "created_at")
    search_fields = ("mobile", "seller__store_name", "ip_address")
    readonly_fields = [f.name for f in SellerLoginOTP._meta.fields]
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Seller Store")
    def seller_store(self, obj):
        return obj.seller.store_name

    @admin.display(description="Mobile Number")
    def masked_mobile(self, obj):
        from core.sms_services import mask_phone_number
        return mask_phone_number(obj.mobile)


@admin.register(ContactInquiry)
class ContactInquiryAdmin(admin.ModelAdmin):
    """
    Administrative management for customer and vendor contact inquiries.
    """
    list_display = (
        "id",
        "full_name",
        "email",
        "phone_number",
        "subject",
        "order_number",
        "status",
        "created_at",
    )
    list_filter = ("status", "subject", "created_at")
    search_fields = ("full_name", "email", "phone_number", "order_number", "message", "admin_notes")
    list_editable = ("status",)
    readonly_fields = ("created_at", "updated_at", "ip_address")
    fieldsets = (
        ("Inquirer Details", {
            "fields": ("full_name", "email", "phone_number", "ip_address"),
        }),
        ("Inquiry Details", {
            "fields": ("subject", "order_number", "message", "created_at", "updated_at"),
        }),
        ("Support Management", {
            "fields": ("status", "admin_notes"),
        }),
    )









