"""
Fitmeezo REST API Serializers.
Supports the website and future mobile application with secure, validated,
and optimized serialization for categories, products, customer accounts,
shopping cart, saved addresses, checkout, and order fulfillment.
"""

from decimal import Decimal
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import (
    Address,
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    Seller,
    SellerOrder,
    WishlistItem,
)

User = get_user_model()


# ==============================================================================
# CATEGORY & PRODUCT SERIALIZERS
# ==============================================================================

class ProductImageSerializer(serializers.ModelSerializer):
    """Gallery showcase image serializer."""
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ["id", "image_url", "alt_text", "display_order"]
        read_only_fields = fields

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None


class ProductVariantSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for public ProductVariant API.
    Exposes effective pricing, discount, and stock availability for active variants.
    """
    effective_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    effective_compare_at_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, allow_null=True
    )
    discount_percentage = serializers.IntegerField(read_only=True)
    is_in_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "sku",
            "option_type",
            "option_value",
            "price",
            "compare_at_price",
            "effective_price",
            "effective_compare_at_price",
            "discount_percentage",
            "stock_quantity",
            "is_in_stock",
        ]
        read_only_fields = fields


class CategoryListSerializer(serializers.ModelSerializer):
    """List serializer for active categories."""
    product_count = serializers.IntegerField(read_only=True, default=0)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "image_url",
            "icon_class",
            "badge",
            "display_order",
            "product_count",
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None


class ProductListSerializer(serializers.ModelSerializer):
    """Summary product serializer for catalog grids and search."""
    category_name = serializers.CharField(source="category.name", read_only=True)
    category_slug = serializers.CharField(source="category.slug", read_only=True)
    image_url = serializers.SerializerMethodField()
    is_in_stock = serializers.BooleanField(read_only=True)
    available_inventory = serializers.IntegerField(read_only=True)
    has_variants = serializers.BooleanField(read_only=True)
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)
    seller_name = serializers.CharField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "sku",
            "category_name",
            "category_slug",
            "seller_name",
            "short_description",
            "price",
            "compare_at_price",
            "discount_percentage",
            "image_url",
            "is_in_stock",
            "available_inventory",
            "has_variants",
            "is_featured",
            "average_rating",
            "review_count",
            "created_at",
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None


class ProductDetailSerializer(serializers.ModelSerializer):
    """Comprehensive product detail serializer with variants, gallery, and ratings."""
    category = CategoryListSerializer(read_only=True)
    image_url = serializers.SerializerMethodField()
    gallery_images = ProductImageSerializer(source="images", many=True, read_only=True)
    variants = serializers.SerializerMethodField()
    is_in_stock = serializers.BooleanField(read_only=True)
    available_inventory = serializers.IntegerField(read_only=True)
    has_variants = serializers.BooleanField(read_only=True)
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)
    rating_breakdown = serializers.SerializerMethodField()
    seller_name = serializers.CharField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "sku",
            "category",
            "seller_name",
            "short_description",
            "description",
            "price",
            "compare_at_price",
            "discount_percentage",
            "image_url",
            "gallery_images",
            "variants",
            "is_in_stock",
            "available_inventory",
            "has_variants",
            "is_featured",
            "average_rating",
            "review_count",
            "rating_breakdown",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None

    def get_variants(self, obj):
        active_vars = obj.variants.filter(is_active=True).order_by("option_type", "option_value", "id")
        return ProductVariantSerializer(active_vars, many=True, context=self.context).data

    def get_rating_breakdown(self, obj):
        return obj.get_rating_breakdown()


class CategoryDetailSerializer(serializers.ModelSerializer):
    """Detailed category serializer with its active products."""
    image_url = serializers.SerializerMethodField()
    products = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "image_url",
            "icon_class",
            "badge",
            "display_order",
            "products",
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        request = self.context.get("request")
        if obj.image:
            return request.build_absolute_uri(obj.image.url) if request else obj.image.url
        return None

    def get_products(self, obj):
        active_prods = obj.products.filter(is_active=True).order_by("-created_at")[:24]
        return ProductListSerializer(active_prods, many=True, context=self.context).data


class ProductReviewSerializer(serializers.ModelSerializer):
    """
    Public and customer serializer for ProductReview API.
    Exposes customer display name safely without revealing private emails or user IDs.
    """
    customer_name = serializers.CharField(source="customer_display_name", read_only=True)
    product_slug = serializers.CharField(source="product.slug", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = ProductReview
        fields = [
            "id",
            "rating",
            "title",
            "comment",
            "customer_name",
            "product_slug",
            "product_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "customer_name",
            "product_slug",
            "product_name",
            "created_at",
            "updated_at",
        ]


# ==============================================================================
# AUTHENTICATION & CUSTOMER PROFILE SERIALIZERS
# ==============================================================================

class CustomerRegistrationSerializer(serializers.Serializer):
    """Validates registration data and provisions new customer account."""
    full_name = serializers.CharField(max_length=100, trim_whitespace=True)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        val = value.strip().lower()
        if User.objects.filter(email__iexact=val).exists():
            raise serializers.ValidationError("An account with this email address already exists. Please log in.")
        return val

    def validate(self, attrs):
        pw = attrs.get("password")
        confirm = attrs.get("confirm_password")
        email = attrs.get("email")

        if pw and confirm and pw != confirm:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})

        # Run Django core password validators
        if pw and email:
            temp_user = User(username=email, email=email)
            try:
                validate_password(pw, user=temp_user)
            except DjangoValidationError as err:
                raise serializers.ValidationError({"password": list(err.messages)})

        return attrs

    def create(self, validated_data):
        email = validated_data["email"]
        full_name = validated_data["full_name"]
        password = validated_data["password"]

        parts = full_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        username = email
        if User.objects.filter(username__iexact=username).exists():
            base_user = email.split("@")[0]
            counter = 1
            while User.objects.filter(username__iexact=f"{base_user}_{counter}").exists():
                counter += 1
            username = f"{base_user}_{counter}"

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )
        CustomerProfile.objects.get_or_create(user=user)
        return user


class CustomerLoginSerializer(serializers.Serializer):
    """Customer credentials serializer."""
    username_or_email = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        identifier = attrs.get("username_or_email", "").strip()
        password = attrs.get("password")

        user = authenticate(username=identifier, password=password)
        if not user:
            raise serializers.ValidationError("Invalid credentials. Please verify your email/username and password.")

        if not user.is_active:
            raise serializers.ValidationError("This account is currently deactivated. Please contact customer support.")

        attrs["user"] = user
        return attrs


class CustomerProfileSerializer(serializers.ModelSerializer):
    """Customer profile inspection and modification serializer."""
    phone_number = serializers.CharField(source="profile.phone_number", allow_blank=True, required=False)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone_number",
            "date_joined",
        ]
        read_only_fields = ["id", "username", "email", "date_joined"]

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        first_name = validated_data.get("first_name", instance.first_name)
        last_name = validated_data.get("last_name", instance.last_name)

        # Support 'full_name' input if passed
        req = self.context.get("request")
        if req and "full_name" in req.data:
            fn = req.data["full_name"].strip()
            parts = fn.split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""

        instance.first_name = first_name
        instance.last_name = last_name
        instance.save(update_fields=["first_name", "last_name"])

        if "phone_number" in profile_data:
            profile, _ = CustomerProfile.objects.get_or_create(user=instance)
            profile.phone_number = profile_data["phone_number"].strip()
            profile.save(update_fields=["phone_number", "updated_at"])

        return instance


# ==============================================================================
# ADDRESS SERIALIZERS
# ==============================================================================

class AddressSerializer(serializers.ModelSerializer):
    """Customer shipping destination address serializer."""

    class Meta:
        model = Address
        fields = [
            "id",
            "full_name",
            "phone_number",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "landmark",
            "address_type",
            "is_default",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_postal_code(self, value):
        code = value.strip()
        if code.isdigit() and len(code) == 6 and code.startswith("0"):
            raise serializers.ValidationError("Indian PIN codes cannot start with 0.")
        return code

    def create(self, validated_data):
        request = self.context.get("request")
        validated_data["user"] = request.user
        return super().create(validated_data)


# ==============================================================================
# WISHLIST & CART SERIALIZERS
# ==============================================================================

class WishlistItemSerializer(serializers.ModelSerializer):
    """
    Serializer for authenticated customer Wishlist API.
    Exposes product details safely without allowing client tampering.
    """
    product_id = serializers.IntegerField(source="product.id", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_slug = serializers.CharField(source="product.slug", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True)
    product_price = serializers.DecimalField(source="product.price", max_digits=10, decimal_places=2, read_only=True)
    compare_at_price = serializers.DecimalField(source="product.compare_at_price", max_digits=10, decimal_places=2, read_only=True, allow_null=True)
    discount_percentage = serializers.IntegerField(source="product.discount_percentage", read_only=True)
    product_image = serializers.SerializerMethodField()
    is_in_stock = serializers.BooleanField(source="product.is_in_stock", read_only=True)

    class Meta:
        model = WishlistItem
        fields = [
            "id",
            "product_id",
            "product_name",
            "product_slug",
            "product_sku",
            "product_price",
            "compare_at_price",
            "discount_percentage",
            "product_image",
            "is_in_stock",
            "created_at",
        ]
        read_only_fields = fields

    def get_product_image(self, obj):
        request = self.context.get("request")
        if obj.product.image:
            return request.build_absolute_uri(obj.product.image.url) if request else obj.product.image.url
        return None


class CartLineAPISerializer(serializers.Serializer):
    """DTO Serializer for live verified line item in cart."""
    key = serializers.CharField()
    product_id = serializers.IntegerField(source="product.id")
    product_name = serializers.CharField(source="product.name")
    product_slug = serializers.CharField(source="product.slug")
    product_image = serializers.SerializerMethodField()
    variant_id = serializers.SerializerMethodField()
    variant_label = serializers.CharField()
    sku = serializers.CharField()
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    compare_at_price = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)
    quantity = serializers.IntegerField()
    line_subtotal = serializers.DecimalField(max_digits=10, decimal_places=2)
    line_discount = serializers.DecimalField(max_digits=10, decimal_places=2)
    discount_percent = serializers.IntegerField()
    available_stock = serializers.IntegerField()
    is_available = serializers.BooleanField()
    has_stock_warning = serializers.BooleanField()
    warning_message = serializers.CharField(allow_null=True)

    def get_product_image(self, obj):
        request = self.context.get("request")
        if obj.product and obj.product.image:
            return request.build_absolute_uri(obj.product.image.url) if request else obj.product.image.url
        return None

    def get_variant_id(self, obj):
        return obj.variant.id if obj.variant else None


# ==============================================================================
# ORDER & CHECKOUT SERIALIZERS
# ==============================================================================

class OrderItemSerializer(serializers.ModelSerializer):
    """Snapshot line item in finalized customer order."""
    seller_order_id = serializers.IntegerField(source="seller_order.id", read_only=True, allow_null=True)

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "seller_order_id",
            "product_id",
            "product_name",
            "variant_id",
            "variant_label",
            "sku",
            "unit_price",
            "compare_at_price",
            "quantity",
            "line_total",
        ]
        read_only_fields = fields


class SellerOrderSerializer(serializers.ModelSerializer):
    """Serializer for seller packages / sub-orders."""
    items = OrderItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    seller_name = serializers.SerializerMethodField()

    class Meta:
        model = SellerOrder
        fields = [
            "id",
            "sub_order_number",
            "seller_id",
            "seller_name",
            "is_platform_fulfilled",
            "status",
            "status_display",
            "subtotal",
            "total_commission",
            "net_seller_payable",
            "customer_shipping_charge",
            "actual_shipping_cost",
            "shipping_allocated_to_seller",
            "shipping_margin",
            "package_dimension_status",
            "estimated_weight_grams",
            "estimated_length_cm",
            "estimated_breadth_cm",
            "estimated_height_cm",
            "actual_weight_grams",
            "actual_length_cm",
            "actual_breadth_cm",
            "actual_height_cm",
            "dimensions_verified_at",
            "courier_name",
            "tracking_number",
            "tracking_url",
            "shiprocket_order_id",
            "shiprocket_shipment_id",
            "shiprocket_awb_code",
            "shipped_at",
            "delivered_at",
            "cancelled_at",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


    def get_seller_name(self, obj):
        return obj.seller.store_name if obj.seller else "Fitmeezo Direct"


class CustomerSellerOrderSerializer(serializers.ModelSerializer):
    """
    Customer-facing serializer for order fulfillment tracking.
    Hides internal marketplace financial metrics (commission rate, total commission, net seller payable).
    """
    items = OrderItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    seller_name = serializers.SerializerMethodField()

    class Meta:
        model = SellerOrder
        fields = [
            "id",
            "sub_order_number",
            "seller_id",
            "seller_name",
            "is_platform_fulfilled",
            "status",
            "status_display",
            "subtotal",
            "courier_name",
            "tracking_number",
            "tracking_url",
            "shipped_at",
            "delivered_at",
            "cancelled_at",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_seller_name(self, obj):
        return obj.seller.store_name if obj.seller else "Fitmeezo Direct"


class OrderListSerializer(serializers.ModelSerializer):
    """Compact summary serializer for customer order history."""
    items_count = serializers.IntegerField(source="items.count", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    payment_status_display = serializers.CharField(source="get_payment_status_display", read_only=True)
    payment_method_display = serializers.CharField(source="get_payment_method_display", read_only=True)
    refund_status_display = serializers.CharField(source="get_refund_status_display", read_only=True)
    is_cancellable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "created_at",
            "status",
            "status_display",
            "is_cancellable",
            "payment_method",
            "payment_method_display",
            "payment_status",
            "payment_status_display",
            "refund_status",
            "refund_status_display",
            "total_amount",
            "items_count",
        ]
        read_only_fields = fields


class OrderDetailSerializer(serializers.ModelSerializer):
    """Complete detail serializer for individual customer order."""
    items = OrderItemSerializer(many=True, read_only=True)
    seller_orders = CustomerSellerOrderSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    payment_status_display = serializers.CharField(source="get_payment_status_display", read_only=True)
    payment_method_display = serializers.CharField(source="get_payment_method_display", read_only=True)
    refund_status_display = serializers.CharField(source="get_refund_status_display", read_only=True)
    is_cancellable = serializers.BooleanField(read_only=True)
    shipping_address = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "created_at",
            "updated_at",
            "status",
            "status_display",
            "is_cancellable",
            "payment_method",
            "payment_method_display",
            "payment_status",
            "payment_status_display",
            "refund_status",
            "refund_status_display",
            "cancelled_at",
            "cancelled_by",
            "cancellation_reason",
            "inventory_restored",
            "paid_at",
            "subtotal",
            "shipping_fee",
            "discount_amount",
            "total_amount",
            "currency",
            "shipping_address",
            "customer_notes",
            "seller_orders",
            "items",
        ]
        read_only_fields = fields

    def get_shipping_address(self, obj):
        return {
            "full_name": obj.shipping_full_name,
            "phone_number": obj.shipping_phone_number,
            "address_line1": obj.shipping_address_line1,
            "address_line2": obj.shipping_address_line2,
            "city": obj.shipping_city,
            "state": obj.shipping_state,
            "postal_code": obj.shipping_postal_code,
            "landmark": obj.shipping_landmark,
            "address_type": obj.shipping_address_type,
            "formatted": obj.shipping_address_formatted,
        }


class SellerProfileSerializer(serializers.ModelSerializer):
    """
    Public/authorized serializer for seller profile details.
    Protects internal admin notes and commission from public leak.
    """
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Seller
        fields = [
            "id",
            "store_name",
            "slug",
            "legal_business_name",
            "business_type",
            "store_description",
            "support_email",
            "support_phone",
            "city",
            "state",
            "postal_code",
            "status",
            "is_active",
            "user_email",
            "created_at",
        ]
        read_only_fields = ["id", "status", "is_active", "user_email", "created_at"]

