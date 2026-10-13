"""
Fitmeezo Backend REST API Views.
Powers the storefront and mobile application with secure endpoints for
catalogue browsing, authentication, profile, addresses, cart, checkout, and orders.
"""

from decimal import Decimal
import uuid
from django.conf import settings
from django.contrib.auth import login, logout, get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .cart import Cart
from .models import (
    Address,
    Category,
    CustomerProfile,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
    SellerOrder,
)
from .serializers import (
    AddressSerializer,
    CartLineAPISerializer,
    CategoryDetailSerializer,
    CategoryListSerializer,
    CustomerLoginSerializer,
    CustomerProfileSerializer,
    CustomerRegistrationSerializer,
    OrderDetailSerializer,
    OrderListSerializer,
    ProductDetailSerializer,
    ProductListSerializer,
)
import logging

from .services import (
    cancel_order,
    create_order_from_cart,
    CheckoutError,
    CheckoutValidationError,
)

logger = logging.getLogger(__name__)
User = get_user_model()

FREE_SHIPPING_THRESHOLD = Decimal("499.00")
STANDARD_SHIPPING_FEE = Decimal("49.00")


class StandardResultsPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = "page_size"
    max_page_size = 48


# ==============================================================================
# CATALOGUE APIS (PUBLIC)
# ==============================================================================

@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def api_category_list(request):
    """Lists all active categories with product counts."""
    categories = (
        Category.objects.filter(is_active=True)
        .annotate(product_count=Count("products", filter=Q(products__is_active=True)))
        .order_by("display_order", "name")
    )
    serializer = CategoryListSerializer(categories, many=True, context={"request": request})
    return Response({"status": "success", "count": len(serializer.data), "categories": serializer.data})


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def api_category_detail(request, slug):
    """Retrieves details of a category and its active products."""
    category = get_object_or_404(Category, slug=slug, is_active=True)
    serializer = CategoryDetailSerializer(category, context={"request": request})
    return Response({"status": "success", "category": serializer.data})


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def api_product_list(request):
    """
    Lists active products with search, category filtering, ordering, and pagination.
    Query parameters:
    - q: Search string for title, SKU, or description
    - category: Category slug
    - ordering: 'price_asc', 'price_desc', 'newest', 'name'
    - in_stock: 'true' (filter to in-stock items only)
    """
    products = (
        Product.objects.filter(is_active=True, is_approved=True)
        .select_related("category")
        .prefetch_related("variants")
        .order_by("-created_at")
    )

    q = request.GET.get("q", "").strip()
    if q:
        products = products.filter(
            Q(name__icontains=q)
            | Q(sku__icontains=q)
            | Q(short_description__icontains=q)
            | Q(category__name__icontains=q)
        )

    cat_slug = request.GET.get("category", "").strip()
    if cat_slug:
        products = products.filter(category__slug=cat_slug)

    ordering = request.GET.get("ordering", "").strip().lower()
    if ordering == "price_asc":
        products = products.order_by("price")
    elif ordering == "price_desc":
        products = products.order_by("-price")
    elif ordering == "name":
        products = products.order_by("name")
    elif ordering == "newest":
        products = products.order_by("-created_at")

    in_stock_only = request.GET.get("in_stock", "").strip().lower() == "true"
    if in_stock_only:
        products = [p for p in products if p.is_in_stock]

    paginator = StandardResultsPagination()
    page = paginator.paginate_queryset(products, request)
    serializer = ProductListSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def api_product_detail(request, slug):
    """Full product specifications, active variants, gallery images, and ratings."""
    product = get_object_or_404(
        Product.objects.select_related("category").prefetch_related("images", "variants"),
        slug=slug,
        is_active=True,
        is_approved=True,
    )
    serializer = ProductDetailSerializer(product, context={"request": request})
    return Response({"status": "success", "product": serializer.data})


# ==============================================================================
# AUTHENTICATION & PROFILE APIS
# ==============================================================================

@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def api_register(request):
    """Registers a new customer account and generates an authentication token."""
    serializer = CustomerRegistrationSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
        token, _ = Token.objects.get_or_create(user=user)
        profile_data = CustomerProfileSerializer(user, context={"request": request}).data
        return Response(
            {
                "status": "success",
                "message": "Registration successful. Welcome to Fitmeezo!",
                "token": token.key,
                "user": profile_data,
            },
            status=status.HTTP_201_CREATED,
        )
    return Response({"status": "error", "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def api_login(request):
    """Authenticates customer credentials and issues an authentication token."""
    serializer = CustomerLoginSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.validated_data["user"]
        login(request, user)  # Also establish session for hybrid consumers
        token, _ = Token.objects.get_or_create(user=user)
        profile_data = CustomerProfileSerializer(user, context={"request": request}).data
        return Response(
            {
                "status": "success",
                "message": f"Welcome back, {user.first_name or user.username}!",
                "token": token.key,
                "user": profile_data,
            }
        )
    return Response({"status": "error", "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_logout(request):
    """Invalidates the customer's authentication token and session."""
    Token.objects.filter(user=request.user).delete()
    logout(request)
    return Response({"status": "success", "message": "Successfully logged out."})


@api_view(["GET", "PUT", "PATCH"])
@permission_classes([permissions.IsAuthenticated])
def api_profile(request):
    """Retrieves or modifies authenticated customer profile details."""
    user = request.user
    if request.method == "GET":
        serializer = CustomerProfileSerializer(user, context={"request": request})
        return Response({"status": "success", "profile": serializer.data})

    partial = request.method == "PATCH"
    serializer = CustomerProfileSerializer(user, data=request.data, partial=partial, context={"request": request})
    if serializer.is_valid():
        serializer.save()
        return Response({"status": "success", "message": "Profile updated successfully.", "profile": serializer.data})
    return Response({"status": "error", "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


# ==============================================================================
# SAVED ADDRESSES APIS (AUTHENTICATED)
# ==============================================================================

@api_view(["GET", "POST"])
@permission_classes([permissions.IsAuthenticated])
def api_address_list_create(request):
    """Lists or creates delivery addresses for the authenticated customer."""
    if request.method == "GET":
        addresses = Address.objects.filter(user=request.user).order_by("-is_default", "-updated_at")
        serializer = AddressSerializer(addresses, many=True)
        return Response({"status": "success", "addresses": serializer.data})

    serializer = AddressSerializer(data=request.data, context={"request": request})
    if serializer.is_valid():
        addr = serializer.save()
        return Response(
            {"status": "success", "message": "Address added successfully.", "address": AddressSerializer(addr).data},
            status=status.HTTP_201_CREATED,
        )
    return Response({"status": "error", "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([permissions.IsAuthenticated])
def api_address_detail(request, pk):
    """Inspects, updates, or deletes a specific address owned by the customer."""
    address = get_object_or_404(Address, pk=pk, user=request.user)

    if request.method == "GET":
        serializer = AddressSerializer(address)
        return Response({"status": "success", "address": serializer.data})

    if request.method == "DELETE":
        address.delete()
        return Response({"status": "success", "message": "Address deleted successfully."})

    partial = request.method == "PATCH"
    serializer = AddressSerializer(address, data=request.data, partial=partial, context={"request": request})
    if serializer.is_valid():
        serializer.save()
        return Response({"status": "success", "message": "Address updated.", "address": serializer.data})
    return Response({"status": "error", "errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_address_set_default(request, pk):
    """Sets the designated address as default for the customer."""
    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.is_default = True
    address.save()
    return Response({"status": "success", "message": "Default shipping address updated."})


# ==============================================================================
# SHOPPING CART APIS
# ==============================================================================

def _serialize_cart(cart, request):
    """Helper formatting live cart state for API responses."""
    lines_serializer = CartLineAPISerializer(cart.get_lines(), many=True, context={"request": request})
    return {
        "lines": lines_serializer.data,
        "total_units": cart.total_units,
        "subtotal": str(cart.subtotal),
        "original_subtotal": str(cart.original_subtotal),
        "total_discount": str(cart.total_discount),
        "total_payable": str(cart.total_payable),
        "is_empty": cart.is_empty,
        "is_checkout_ready": cart.is_checkout_ready,
        "has_warnings": cart.has_warnings,
    }


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def api_cart_detail(request):
    """Returns current live shopping cart state and financial breakdown."""
    cart = Cart(request)
    return Response({"status": "success", "cart": _serialize_cart(cart, request)})


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def api_cart_add(request):
    """Adds a product or variant to the cart with server stock verification."""
    cart = Cart(request)
    product_id = request.data.get("product_id")
    variant_id = request.data.get("variant_id")
    quantity = request.data.get("quantity", 1)

    if not product_id:
        return Response({"status": "error", "message": "product_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        product = Product.objects.get(pk=product_id, is_active=True, is_approved=True)
    except Product.DoesNotExist:
        return Response({"status": "error", "message": "Product not found or inactive."}, status=status.HTTP_404_NOT_FOUND)

    variant = None
    if variant_id:
        try:
            variant = ProductVariant.objects.get(pk=variant_id, product=product, is_active=True)
        except ProductVariant.DoesNotExist:
            return Response({"status": "error", "message": "Variant not found or inactive."}, status=status.HTTP_404_NOT_FOUND)

    success, message, key = cart.add(product=product, variant=variant, quantity=quantity)
    if not success:
        return Response({"status": "error", "message": message}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        "status": "success",
        "message": message,
        "key": key,
        "cart": _serialize_cart(cart, request),
    })


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def api_cart_update(request):
    """Updates line item quantity."""
    cart = Cart(request)
    key = request.data.get("key")
    quantity = request.data.get("quantity")

    if not key or quantity is None:
        return Response({"status": "error", "message": "key and quantity are required."}, status=status.HTTP_400_BAD_REQUEST)

    success, message = cart.update_quantity(key=key, quantity=quantity)
    if not success:
        return Response({"status": "error", "message": message}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        "status": "success",
        "message": message,
        "cart": _serialize_cart(cart, request),
    })


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def api_cart_remove(request):
    """Removes a line item from the cart."""
    cart = Cart(request)
    key = request.data.get("key")

    if not key:
        return Response({"status": "error", "message": "key is required."}, status=status.HTTP_400_BAD_REQUEST)

    success, message = cart.remove(key)
    if not success:
        return Response({"status": "error", "message": message}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        "status": "success",
        "message": message,
        "cart": _serialize_cart(cart, request),
    })


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def api_cart_clear(request):
    """Clears the shopping cart."""
    cart = Cart(request)
    success, message = cart.clear()
    return Response({
        "status": "success",
        "message": message,
        "cart": _serialize_cart(cart, request),
    })


# ==============================================================================
# CHECKOUT & ORDER APIS (AUTHENTICATED)
# ==============================================================================

@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_checkout_summary(request):
    """
    Returns server-calculated pricing, shipping charges, and saved addresses
    prior to order submission.
    """
    cart = Cart(request)
    if cart.is_empty:
        return Response({"status": "error", "message": "Cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

    if cart.has_warnings or not cart.is_checkout_ready:
        return Response(
            {
                "status": "error",
                "message": "Cart contains items that are out of stock or exceed available quantities.",
                "cart": _serialize_cart(cart, request),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    subtotal = cart.subtotal
    shipping_fee = Decimal("0.00") if subtotal >= FREE_SHIPPING_THRESHOLD else STANDARD_SHIPPING_FEE
    total_payable = (subtotal + shipping_fee).quantize(Decimal("0.01"))
    addresses = Address.objects.filter(user=request.user).order_by("-is_default", "-updated_at")

    return Response({
        "status": "success",
        "cart": _serialize_cart(cart, request),
        "subtotal": str(subtotal),
        "shipping_fee": str(shipping_fee),
        "free_shipping_threshold": str(FREE_SHIPPING_THRESHOLD),
        "total_payable": str(total_payable),
        "saved_addresses": AddressSerializer(addresses, many=True).data,
    })


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_checkout_place_order(request):
    """
    Places an order via Cash on Delivery (COD).
    Enforces server-side price recalculation, row-level stock locking (select_for_update),
    inventory deduction, snapshot creation, and idempotency protection.
    """
    # 1. Idempotency deduplication check first (handles repeat submissions after cart clearance)
    idempotency_token = request.data.get("idempotency_token", "").strip()
    if idempotency_token:
        existing_order = Order.objects.filter(idempotency_key=idempotency_token, user=request.user).first()
        if existing_order:
            serializer = OrderDetailSerializer(existing_order, context={"request": request})
            return Response({
                "status": "success",
                "message": f"Order #{existing_order.order_number} has already been placed.",
                "order": serializer.data,
            })
    else:
        idempotency_token = uuid.uuid4().hex

    cart = Cart(request)
    if cart.is_empty:
        return Response({"status": "error", "message": "Cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

    if cart.has_warnings or not cart.is_checkout_ready:
        return Response(
            {"status": "error", "message": "Cart items exceed available inventory."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 2. Address resolution
    address_id = request.data.get("address_id")
    address = None
    if address_id:
        address = Address.objects.filter(pk=address_id, user=request.user).first()

    if not address and "address" in request.data:
        # Inline address creation
        addr_serializer = AddressSerializer(data=request.data["address"], context={"request": request})
        if addr_serializer.is_valid():
            address = addr_serializer.save()
        else:
            return Response({"status": "error", "errors": addr_serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    if not address:
        return Response(
            {"status": "error", "message": "Please select a valid delivery address or provide address details."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    payment_method = request.data.get("payment_method", Order.PAYMENT_METHOD_COD).strip().lower()
    if payment_method != Order.PAYMENT_METHOD_COD:
        return Response(
            {"status": "error", "message": "Currently only Cash on Delivery (COD) is supported for mobile checkout."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    customer_notes = request.data.get("customer_notes", "").strip()[:500]

    # 3. Atomic stock lock & deduction via shared service
    try:
        order, is_duplicate = create_order_from_cart(
            user=request.user,
            cart=cart,
            shipping_address=address,
            payment_method=payment_method,
            customer_notes=customer_notes,
            idempotency_token=idempotency_token,
            standard_shipping_fee=STANDARD_SHIPPING_FEE,
            request=request,
        )

        serializer = OrderDetailSerializer(order, context={"request": request})
        if is_duplicate:
            return Response({
                "status": "success",
                "message": f"Order #{order.order_number} has already been placed.",
                "order": serializer.data,
            })

        return Response(
            {
                "status": "success",
                "message": f"Order #{order.order_number} confirmed successfully!",
                "order": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    except CheckoutValidationError as cve:
        return Response(
            {"status": "error", "message": str(cve)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:
        logger.error(f"Order placement failed in API: {exc}", exc_info=True)
        return Response(
            {"status": "error", "message": f"Order placement failed: {str(exc)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ==============================================================================
# CUSTOMER ORDER HISTORY APIS (AUTHENTICATED)
# ==============================================================================

@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_order_list(request):
    """Lists order history for the authenticated customer only."""
    orders = Order.objects.filter(user=request.user).order_by("-created_at")
    paginator = StandardResultsPagination()
    page = paginator.paginate_queryset(orders, request)
    serializer = OrderListSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_order_detail(request, order_number):
    """
    Retrieves full details of a customer's specific order.
    Strictly isolated: returns 404 if the order belongs to another customer.
    """
    order = get_object_or_404(
        Order.objects.prefetch_related("items"),
        order_number=order_number,
        user=request.user,
    )
    serializer = OrderDetailSerializer(order, context={"request": request})
    return Response({"status": "success", "order": serializer.data})


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_order_cancel(request, order_number):
    """
    Cancels an order belonging to the authenticated customer.
    Enforces:
    - Customer Token or Session authentication
    - Object-level ownership isolation (safe 404 lookup without data leakage)
    - Rejects invalid state transitions (Shipped, Delivered, or already Cancelled) with HTTP 400
    - Restores inventory units atomically exactly once
    - Updates refund accounting for online payments without false claims of refund completion
    - Returns updated OrderDetail representation
    """
    order = get_object_or_404(
        Order.objects.prefetch_related("items"),
        order_number=order_number,
        user=request.user,
    )

    reason = request.data.get("reason", "").strip() if isinstance(request.data, dict) else ""
    success, msg, cancelled_order = cancel_order(
        order=order,
        user=request.user,
        cancelled_by="customer",
        reason=reason or "Cancelled by customer via mobile API",
    )

    if not success:
        return Response(
            {"status": "error", "message": msg},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = OrderDetailSerializer(cancelled_order, context={"request": request})
    return Response(
        {
            "status": "success",
            "message": f"Order #{cancelled_order.order_number} has been cancelled successfully.",
            "order": serializer.data,
        },
        status=status.HTTP_200_OK,
    )

