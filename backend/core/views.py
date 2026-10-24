
import csv
from decimal import Decimal
import json
import logging
import re
from urllib.parse import quote
import uuid

import razorpay

logger = logging.getLogger(__name__)

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction, IntegrityError, models
from django.db.models import Avg, F, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .cart import Cart
from .emails import (
    send_contact_inquiry_email,
    send_order_confirmation_email,
    send_payment_failed_email,
    send_payment_success_email,
)
from .forms import (
    AddressForm,
    ContactForm,
    OrderTrackingForm,
    CustomerLoginForm,
    CustomerProfileForm,
    CustomerRegistrationForm,
    ProductReviewForm,
    SellerFulfillmentUpdateForm,
    PackagePackedMeasurementsForm,
    SellerProductForm,
    SellerProductImageForm,
    SellerProductVariantForm,
    SellerProfileUpdateForm,
    SellerRegistrationForm,
    PublicSellerRegistrationForm,
    SellerLoginForm,
    SellerMobileOTPRequestForm,
    SellerMobileOTPVerifyForm,
    SellerStandaloneInventoryForm,
    ReturnRequestSubmissionForm,
    SellerReturnActionForm,
    ProcessRefundForm,
    SellerBankDetailsForm,
    SellerClosureRequestForm,
)
from .models import (
    Address,
    Category,
    ContactInquiry,
    CustomerProfile,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    RefundRecord,
    ReturnAuditLog,
    ReturnRequest,
    ReturnRequestItem,
    Seller,
    SellerLoginOTP,
    SellerOrder,
    WishlistItem,
    PayoutBatch,
    SellerLedgerEntry,
    CODReceiptRecord,
)
from .otp_services import (
    generate_and_send_seller_otp,
    verify_seller_otp_code,
    get_client_ip,
    get_client_user_agent,
)
from .ledger_services import (
    get_seller_ledger_summary,
    calculate_seller_payout_eligibility,
    LedgerServiceError,
)
from .return_services import (
    create_return_request,
    transition_return_status,
    process_simulated_refund,
    validate_item_eligibility,
    ReturnServiceError,
    ReturnValidationError,
    ReturnPermissionError,
    RefundError,
    VALID_TRANSITIONS,
)
from .permissions import is_approved_seller, seller_required
from .serializers import ProductReviewSerializer, ProductVariantSerializer, WishlistItemSerializer
from .shiprocket import (
    estimate_package_shipping_rate,
    ShiprocketError,
    RateEstimateResult,
    create_mock_shipment_booking,
    check_booking_eligibility,
)
from .seller_services import (
    DEFAULT_LOW_STOCK_THRESHOLD,
    get_seller_dashboard_metrics,
    get_seller_reports_data,
    parse_date_range,
    sanitize_csv_cell,
)
from .services import (
    cancel_order,
    create_order_from_cart,
    CheckoutError,
    CheckoutValidationError,
)
from django.contrib.admin.models import CHANGE
from .admin_permissions import log_admin_action

FREE_SHIPPING_THRESHOLD = Decimal("499.00")
STANDARD_SHIPPING_FEE = Decimal("49.00")
logger = logging.getLogger(__name__)


@api_view(["GET"])
def health_check(request):
    """
    Health check endpoint for Fitmeezo platform.
    Returns JSON response indicating operational status and system context.
    """
    return Response({
        "status": "ok",
        "app": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        "tagline": getattr(settings, "BRAND_TAGLINE", "SHOP SMART • LIVE BETTER"),
        "currency": getattr(settings, "CURRENCY", "INR"),
        "database_engine": connection.vendor,
        "environment": "development" if settings.DEBUG else "production",
        "timestamp": timezone.now().isoformat(),
    })


SAMPLE_CATEGORIES_FALLBACK = [
    {
        "name": "Electronics",
        "slug": "electronics",
        "icon": "bi-laptop",
        "desc": "Smartphones, Audio & Gadgets",
        "badge": "Hot",
    },
    {
        "name": "Fashion",
        "slug": "fashion",
        "icon": "bi-bag",
        "desc": "Men, Women & Footwear",
        "badge": "New Season",
    },
    {
        "name": "Home & Living",
        "slug": "home-living",
        "icon": "bi-house-door",
        "desc": "Kitchen, Decor & Bedsheets",
        "badge": None,
    },
    {
        "name": "Beauty",
        "slug": "beauty",
        "icon": "bi-stars",
        "desc": "Skincare, Hair & Wellness",
        "badge": "Trending",
    },
    {
        "name": "Sports",
        "slug": "sports",
        "icon": "bi-bicycle",
        "desc": "Fitness Gear & Gym Wear",
        "badge": None,
    },
    {
        "name": "Baby & Kids",
        "slug": "baby-kids",
        "icon": "bi-emoji-smile",
        "desc": "Toys, Feeding & Baby Care",
        "badge": None,
    },
]

SAMPLE_SPOTLIGHT_PRODUCT = {
    "id": "hero-spotlight",
    "title": "FitPulse Pro Max Smartwatch (AMOLED Display)",
    "name": "FitPulse Pro Max Smartwatch (AMOLED Display)",
    "category": "Electronics",
    "price": "2,499",
    "original_price": "4,999",
    "discount_percent": 50,
    "rating": 4.8,
    "rating_count": 312,
    "badge": "Deal of the Day",
    "image_url": "https://images.unsplash.com/photo-1523275335684-37898b6baf30?w=600&auto=format&fit=crop&q=80",
}

SAMPLE_FEATURED_PRODUCTS = [
    {
        "id": "feat-1",
        "title": "SonicBass Wireless Noise Cancelling Over-Ear Headphones",
        "name": "SonicBass Wireless Noise Cancelling Over-Ear Headphones",
        "category": "Electronics",
        "price": "1,999",
        "original_price": "3,999",
        "discount_percent": 50,
        "rating": 4.7,
        "rating_count": 142,
        "badge": "Bestseller",
        "image_url": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=600&auto=format&fit=crop&q=80",
    },
    {
        "id": "feat-2",
        "title": "AeroGrip Performance Lightweight Running Shoes",
        "name": "AeroGrip Performance Lightweight Running Shoes",
        "category": "Fashion",
        "price": "1,699",
        "original_price": "2,999",
        "discount_percent": 43,
        "rating": 4.5,
        "rating_count": 98,
        "badge": "Popular",
        "image_url": "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=600&auto=format&fit=crop&q=80",
    },
    {
        "id": "feat-3",
        "title": "PureGlow Vitamin C & Hyaluronic Acid Face Serum (30ml)",
        "name": "PureGlow Vitamin C & Hyaluronic Acid Face Serum (30ml)",
        "category": "Beauty",
        "price": "549",
        "original_price": "999",
        "discount_percent": 45,
        "rating": 4.8,
        "rating_count": 210,
        "badge": "Organic",
        "image_url": "https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=600&auto=format&fit=crop&q=80",
    },
    {
        "id": "feat-4",
        "title": "Artisan Crafted Pure Copper Water Dispenser (5 Litres)",
        "name": "Artisan Crafted Pure Copper Water Dispenser (5 Litres)",
        "category": "Home & Living",
        "price": "1,399",
        "original_price": "2,499",
        "discount_percent": 44,
        "rating": 4.6,
        "rating_count": 67,
        "badge": "Handcrafted",
        "image_url": "https://images.unsplash.com/photo-1584308666744-24d5c474f2ae?w=600&auto=format&fit=crop&q=80",
    },
]

SAMPLE_TRENDING_PRODUCTS = [
    {
        "id": "trend-1",
        "title": "PureLoom 300TC 100% Breathable Cotton King Bedsheet Set",
        "name": "PureLoom 300TC 100% Breathable Cotton King Bedsheet Set",
        "category": "Home & Living",
        "price": "1,199",
        "original_price": "1,999",
        "discount_percent": 40,
        "rating": 4.6,
        "rating_count": 184,
        "badge": "Top Rated",
        "image_url": "https://images.unsplash.com/photo-1522771739844-6a9f6d5f14af?w=600&auto=format&fit=crop&q=80",
    },
    {
        "id": "trend-2",
        "title": "ProGrip Adjustable Hex Rubber Dumbbell Pair (10kg-20kg)",
        "name": "ProGrip Adjustable Hex Rubber Dumbbell Pair (10kg-20kg)",
        "category": "Sports",
        "price": "2,799",
        "original_price": "4,200",
        "discount_percent": 33,
        "rating": 4.7,
        "rating_count": 92,
        "badge": "Heavy Duty",
        "image_url": "https://images.unsplash.com/photo-1583454110551-21f2fa2afe61?w=600&auto=format&fit=crop&q=80",
    },
    {
        "id": "trend-3",
        "title": "SafeStep Ergonomic Anti-Tip Baby Walker & Sensory Activity Center",
        "name": "SafeStep Ergonomic Anti-Tip Baby Walker & Sensory Activity Center",
        "category": "Baby & Kids",
        "price": "1,849",
        "original_price": "2,799",
        "discount_percent": 34,
        "rating": 4.9,
        "rating_count": 115,
        "badge": "BPA Free",
        "image_url": "https://images.unsplash.com/photo-1515488042361-ee00e0ddd4e4?w=600&auto=format&fit=crop&q=80",
    },
    {
        "id": "trend-4",
        "title": "Fitmeezo Signature Urban Commuter Water-Resistant Backpack (28L)",
        "name": "Fitmeezo Signature Urban Commuter Water-Resistant Backpack (28L)",
        "category": "Fashion",
        "price": "999",
        "original_price": "1,999",
        "discount_percent": 50,
        "rating": 4.5,
        "rating_count": 76,
        "badge": "Trending",
        "image_url": "https://images.unsplash.com/photo-1553062407-98eeb64c6a62?w=600&auto=format&fit=crop&q=80",
    },
]

BENEFITS = [
    {
        "icon": "bi-truck",
        "title": "Free Delivery Across India",
        "desc": "On all eligible orders over ₹499 to 19,000+ pin codes.",
    },
    {
        "icon": "bi-shield-check",
        "title": "100% Verified Genuine",
        "desc": "Authentic products sourced directly from trusted brands.",
    },
    {
        "icon": "bi-arrow-repeat",
        "title": "Easy 7-Day Replacement",
        "desc": "Hassle-free replacement for defective or damaged items.",
    },
    {
        "icon": "bi-headset",
        "title": "24/7 Dedicated Support",
        "desc": "Toll-free customer helpline & responsive email assistance.",
    },
]

CAMPAIGN_BANNER = {
    "badge": "Sample Campaign",
    "title": "Great Indian Smart Savings Festival",
    "desc": "Special introductory savings across all categories. Use demo code for extra savings.",
    "coupon_code": "SMARTFIT200",
}


def home(request):
    """
    Homepage view for Fitmeezo multi-category e-commerce storefront.
    Supplies database-driven categories and active featured/trending products
    (falling back to sample layout data if catalogue is unseeded).
    """
    try:
        db_categories = list(
            Category.objects.filter(is_active=True).order_by("display_order", "name")
        )
        categories = db_categories if db_categories else SAMPLE_CATEGORIES_FALLBACK
    except Exception:
        categories = SAMPLE_CATEGORIES_FALLBACK

    # Active featured products from database
    try:
        db_featured = list(
            Product.objects.filter(is_active=True, is_approved=True, is_featured=True)
            .select_related("category")
            .order_by("-created_at")[:8]
        )
    except Exception:
        db_featured = []

    featured_products = db_featured

    # Active trending products from database
    # Ordering rule: Newest active products until popularity metric exists
    try:
        db_trending = list(
            Product.objects.filter(is_active=True, is_approved=True)
            .select_related("category")
            .order_by("-created_at")[:8]
        )
    except Exception:
        db_trending = []

    trending_products = db_trending

    context = {
        "categories": categories,
        "featured_products": featured_products,
        "trending_products": trending_products,
        "benefits": BENEFITS,
        "campaign_banner": CAMPAIGN_BANNER,
    }
    return render(request, "home.html", context)


def category_list(request):
    """
    Public listing page for all active product categories on Fitmeezo.
    Inactive categories are excluded from public listing.
    """
    categories = Category.objects.filter(is_active=True).order_by("display_order", "name")
    return render(request, "categories/category_list.html", {
        "categories": categories,
    })


def category_detail(request, slug):
    """
    Category detail and department landing page.
    Returns 404 if the category slug is unknown or marked inactive.
    Paginates active products belonging to this category.
    """
    category = get_object_or_404(Category, slug=slug, is_active=True)

    products_qs = (
        category.products.filter(is_active=True, is_approved=True)
        .select_related("category")
        .order_by("-created_at")
    )
    paginator = Paginator(products_qs, 12)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    return render(request, "categories/category_detail.html", {
        "category": category,
        "page_obj": page_obj,
        "products": page_obj.object_list,
        "total_products_count": paginator.count,
    })


def product_search(request):
    """
    Backend-powered Product Search view.
    Searches active products across name, short description, full description,
    product SKU, category name, and variant SKUs/option values.
    Supports category filtering, multi-criteria sorting, and pagination.
    Safely handles empty queries, unusual characters, and excessively long inputs.
    """
    raw_query = request.GET.get("q", "")
    # Safeguard: clamp query length to 100 characters to prevent ReDoS / excessive load
    search_query = raw_query.strip()[:100]
    category_slug = request.GET.get("category", "").strip().lower()
    sort = request.GET.get("sort", "featured").strip().lower()
    page_number = request.GET.get("page", 1)

    # Base active products queryset adhering to catalog business rules
    products_qs = (
        Product.objects.filter(is_active=True, is_approved=True, category__is_active=True)
        .select_related("category")
        .prefetch_related("variants", "reviews")
    )

    # Category filtering
    selected_cat_obj = None
    if category_slug and category_slug != "all":
        products_qs = products_qs.filter(category__slug=category_slug)
        selected_cat_obj = Category.objects.filter(slug=category_slug, is_active=True).first()

    # Query term filtering using secure Django ORM Q objects
    is_empty_query = False
    if search_query:
        terms = [t for t in search_query.split() if t]
        if terms:
            term_filter = Q()
            for term in terms:
                term_filter &= (
                    Q(name__icontains=term)
                    | Q(short_description__icontains=term)
                    | Q(description__icontains=term)
                    | Q(sku__icontains=term)
                    | Q(category__name__icontains=term)
                    | Q(variants__sku__icontains=term)
                    | Q(variants__option_value__icontains=term)
                )
            products_qs = products_qs.filter(term_filter).distinct()
    else:
        # If neither query keyword nor specific category is selected, return empty queryset
        if not category_slug or category_slug == "all":
            products_qs = Product.objects.none()
            is_empty_query = True

    # Multi-criteria sorting
    if sort == "price-asc":
        products_qs = products_qs.order_by("price", "-created_at")
    elif sort == "price-desc":
        products_qs = products_qs.order_by("-price", "-created_at")
    elif sort == "newest":
        products_qs = products_qs.order_by("-created_at")
    elif sort == "name-asc":
        products_qs = products_qs.order_by("name")
    elif sort == "rating":
        products_qs = products_qs.annotate(
            avg_rating=Avg("reviews__rating", filter=Q(reviews__status="approved"))
        ).order_by(F("avg_rating").desc(nulls_last=True), "-created_at")
    else:
        sort = "featured"
        products_qs = products_qs.order_by("-is_featured", "-created_at")

    # Pagination: 12 products per page
    paginator = Paginator(products_qs, 12)
    page_obj = paginator.get_page(page_number)

    context = {
        "search_query": search_query,
        "raw_query": raw_query,
        "selected_category": category_slug if category_slug != "all" else "",
        "selected_cat_obj": selected_cat_obj,
        "current_sort": sort,
        "page_obj": page_obj,
        "products": page_obj.object_list,
        "total_products_count": paginator.count,
        "is_empty_query": is_empty_query,
    }
    return render(request, "products/search_results.html", context)



def product_detail(request, slug):
    """
    Product detail page.
    Returns 404 for unknown or inactive products.
    Pre-loads active variants and gallery images for seamless rendering.
    """
    product = get_object_or_404(
        Product.objects.select_related("category").prefetch_related("images", "variants"),
        Q(is_approved=True) | Q(approval_status=Product.APPROVAL_APPROVED),
        slug=slug,
        is_active=True,
    )
    active_variants = list(
        product.variants.filter(is_active=True).order_by("option_type", "option_value", "id")
    )

    # Group variants by option_type for structured UI selectors
    variants_by_type = {}
    for v in active_variants:
        variants_by_type.setdefault(v.option_type, []).append(v)

    # Build JSON payload for interactive client-side selector
    variants_json_data = [
        {
            "id": v.id,
            "sku": v.sku,
            "option_type": v.option_type,
            "option_value": v.option_value,
            "price": str(v.effective_price),
            "formatted_price": f"₹{v.effective_price:,.2f}",
            "compare_at_price": str(v.effective_compare_at_price) if v.effective_compare_at_price else None,
            "formatted_compare_at_price": f"₹{v.effective_compare_at_price:,.2f}" if v.effective_compare_at_price else None,
            "discount_percentage": v.discount_percentage,
            "stock_quantity": v.stock_quantity,
            "is_in_stock": v.is_in_stock,
        }
        for v in active_variants
    ]

    # --- Prompt 9: Reviews & Ratings ---
    # Fetch approved reviews ordered by newest first
    reviews_qs = (
        product.reviews.filter(status=ProductReview.STATUS_APPROVED)
        .select_related("user")
        .order_by("-created_at")
    )
    paginator = Paginator(reviews_qs, 5)
    page_number = request.GET.get("review_page", 1)
    reviews_page = paginator.get_page(page_number)

    # Check if logged-in customer already submitted a review
    user_review = None
    review_form = None
    if request.user.is_authenticated:
        user_review = product.reviews.filter(user=request.user).first()
        if not user_review:
            review_form = ProductReviewForm()
        else:
            review_form = ProductReviewForm(instance=user_review)
    else:
        review_form = ProductReviewForm()

    rating_breakdown = product.get_rating_breakdown()

    has_configured_sizes = product.variants.filter(option_type__iexact="Size").exists()
    has_in_stock_size = any(
        v.is_in_stock for v in active_variants if v.option_type.lower() == "size"
    )

    return render(request, "products/product_detail.html", {
        "product": product,
        "category": product.category,
        "active_variants": active_variants,
        "variants_by_type": variants_by_type,
        "variants_json_data": variants_json_data,
        "has_multiple_types": len(variants_by_type) > 1,
        "has_configured_sizes": has_configured_sizes,
        "has_in_stock_size": has_in_stock_size,
        "reviews_page": reviews_page,
        "total_approved_reviews": paginator.count,
        "user_review": user_review,
        "review_form": review_form,
        "rating_breakdown": rating_breakdown,
    })


@login_required(login_url="core:account_login")
def product_review_submit(request, slug):
    """
    Submits a new customer product review.
    Enforces POST method, authenticated session, 1 review per product,
    and server-side validation.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for review submission.")

    product = get_object_or_404(Product, slug=slug, is_active=True, is_approved=True)

    # Prevent duplicate reviews
    if ProductReview.objects.filter(product=product, user=request.user).exists():
        messages.warning(
            request,
            "You have already submitted a review for this product. You can update your existing review below."
        )
        return redirect(f"{product.get_absolute_url()}#reviews")

    form = ProductReviewForm(request.POST)
    if form.is_valid():
        review = form.save(commit=False)
        review.product = product
        review.user = request.user
        review.status = ProductReview.STATUS_APPROVED
        try:
            review.save()
            messages.success(request, "Thank you! Your product review has been submitted successfully.")
        except IntegrityError:
            messages.error(request, "A review for this product has already been submitted from your account.")
    else:
        for field, errs in form.errors.items():
            for err in errs:
                messages.error(request, f"{err}")

    return redirect(f"{product.get_absolute_url()}#reviews")


@login_required(login_url="core:account_login")
def review_edit(request, pk):
    """
    Edits an existing customer review.
    Enforces customer ownership checks to prevent IDOR vulnerabilities.
    """
    review = get_object_or_404(ProductReview.objects.select_related("product"), pk=pk)

    if review.user != request.user:
        return HttpResponseForbidden("You are not authorized to edit another customer's review.")

    if request.method == "POST":
        form = ProductReviewForm(request.POST, instance=review)
        if form.is_valid():
            form.save()
            messages.success(request, "Your review has been updated successfully.")
            next_url = request.POST.get("next") or request.GET.get("next")
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect(f"{review.product.get_absolute_url()}#reviews")
        else:
            for field, errs in form.errors.items():
                for err in errs:
                    messages.error(request, f"{err}")
    else:
        form = ProductReviewForm(instance=review)

    return render(request, "products/review_edit.html", {
        "review": review,
        "product": review.product,
        "form": form,
        "next": request.GET.get("next", ""),
    })


@login_required(login_url="core:account_login")
def review_delete(request, pk):
    """
    Deletes a customer's review after confirmation.
    Enforces customer ownership checks and POST method for state changes.
    """
    review = get_object_or_404(ProductReview.objects.select_related("product"), pk=pk)

    if review.user != request.user:
        return HttpResponseForbidden("You are not authorized to delete another customer's review.")

    if request.method == "POST":
        product_url = review.product.get_absolute_url()
        next_url = request.POST.get("next") or request.GET.get("next")
        review.delete()
        messages.success(request, "Your review has been removed.")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return redirect(next_url)
        return redirect(f"{product_url}#reviews")

    return render(request, "products/review_delete_confirm.html", {
        "review": review,
        "product": review.product,
        "next": request.GET.get("next", ""),
    })


@login_required(login_url="core:account_login")
def account_reviews(request):
    """
    Customer dashboard view listing all reviews authored by the customer.
    """
    reviews = (
        ProductReview.objects.filter(user=request.user)
        .select_related("product")
        .order_by("-created_at")
    )
    return render(request, "account/reviews.html", {
        "reviews": reviews,
        "total_reviews": reviews.count(),
    })


@api_view(["GET", "POST"])
def product_reviews_api(request, slug):
    """
    Public and customer API endpoint for product reviews.
    GET: Returns list of approved reviews for the product, average rating, and count.
    POST: Allows authenticated customers to submit a review for the product.
    """
    product = get_object_or_404(Product, slug=slug, is_active=True)

    if request.method == "GET":
        approved_reviews = (
            product.reviews.filter(status=ProductReview.STATUS_APPROVED)
            .select_related("user")
            .order_by("-created_at")
        )
        serializer = ProductReviewSerializer(approved_reviews, many=True)
        return Response({
            "product_id": product.id,
            "product_name": product.name,
            "product_slug": product.slug,
            "average_rating": product.average_rating,
            "review_count": product.review_count,
            "reviews": serializer.data,
        })

    elif request.method == "POST":
        if not request.user.is_authenticated:
            return Response(
                {"detail": "Authentication credentials were not provided. Please log in to submit a review."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if ProductReview.objects.filter(product=product, user=request.user).exists():
            return Response(
                {"detail": "You have already submitted a review for this product."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ProductReviewSerializer(data=request.data)
        if serializer.is_valid():
            rating = serializer.validated_data.get("rating")
            if rating is None or rating < 1 or rating > 5:
                return Response({"rating": ["Rating must be between 1 and 5."]}, status=status.HTTP_400_BAD_REQUEST)
            review = serializer.save(product=product, user=request.user, status=ProductReview.STATUS_APPROVED)
            return Response(ProductReviewSerializer(review).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)



@api_view(["GET"])
def product_variants_api(request, slug):
    """
    Public read-only API endpoint exposing active variants for an active product.
    Returns 404 if product is inactive or not found.
    Inactive variants are strictly excluded.
    Designed for future Flutter mobile app and headless integrations.
    """
    product = get_object_or_404(Product, slug=slug, is_active=True)
    active_variants = product.variants.filter(is_active=True).order_by("option_type", "option_value", "id")
    serializer = ProductVariantSerializer(active_variants, many=True)
    return Response({
        "product_id": product.id,
        "product_name": product.name,
        "product_slug": product.slug,
        "has_variants": len(serializer.data) > 0,
        "total_active_variants": len(serializer.data),
        "variants": serializer.data,
    })


def _is_ajax(request):
    """Detects AJAX or JSON API requests."""
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.content_type == "application/json"
        or "application/json" in request.META.get("HTTP_ACCEPT", "")
    )


def cart_detail(request):
    """
    Renders the Fitmeezo shopping cart page.
    Displays all current items, unit prices, line subtotals,
    inventory availability, stock alerts, and totals before checkout.
    """
    cart = Cart(request)
    cart_lines = cart.get_lines()

    return render(request, "cart/cart.html", {
        "cart": cart,
        "cart_lines": cart_lines,
        "is_cart_page": True,
    })


def cart_add(request):
    """
    Adds a product and optional variant to the shopping cart.
    Requires HTTP POST method. Server-side validates product status,
    active variant, and available inventory.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for cart mutations.")

    cart = Cart(request)
    product_id = request.POST.get("product_id")
    variant_id = request.POST.get("variant_id")
    quantity_raw = request.POST.get("quantity", 1)

    # 1. Product existence & status validation
    try:
        product_id = int(product_id)
        product = Product.objects.get(pk=product_id)
    except (TypeError, ValueError, Product.DoesNotExist):
        err_msg = "The specified product could not be found."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:cart_detail")

    if not product.is_active:
        err_msg = f"'{product.name}' is currently unavailable."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:cart_detail")

    # 2. Variant validation
    variant = None
    if variant_id and str(variant_id).strip():
        try:
            variant_id = int(variant_id)
            variant = ProductVariant.objects.get(pk=variant_id, product=product)
        except (ValueError, ProductVariant.DoesNotExist):
            err_msg = "The specified variant is invalid for this product."
            if _is_ajax(request):
                return JsonResponse({"success": False, "message": err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect(product.get_absolute_url())

        if not variant.is_active:
            err_msg = "The selected option is currently inactive."
            if _is_ajax(request):
                return JsonResponse({"success": False, "message": err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect(product.get_absolute_url())

        if variant.stock_quantity <= 0:
            if variant.option_type.lower() == "size":
                err_msg = f"The selected size '{variant.option_value}' is currently out of stock."
            else:
                err_msg = f"The selected option '{variant.option_value}' is currently out of stock."
            if _is_ajax(request):
                return JsonResponse({"success": False, "message": err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect(product.get_absolute_url())
    elif product.has_size_variants:
        err_msg = "Please select a size before adding to your cart."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect(product.get_absolute_url())
    elif product.has_variants:
        err_msg = "Please select an available option before adding to your cart."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect(product.get_absolute_url())
    elif product.variants.filter(option_type__iexact="Size").exists():
        err_msg = "Sizes are currently unavailable for this product."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect(product.get_absolute_url())

    # 3. Quantity parsing
    try:
        quantity = int(quantity_raw)
    except (TypeError, ValueError):
        err_msg = "Please provide a valid quantity number."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect(product.get_absolute_url())

    # 4. Perform cart addition
    success, message, item_key = cart.add(product, variant=variant, quantity=quantity)

    if success:
        messages.success(request, message)
    else:
        messages.error(request, message)

    if _is_ajax(request):
        status_code = 200 if success else 400
        return JsonResponse({
            "success": success,
            "message": message,
            "cart_total_units": cart.total_units,
            "cart_subtotal": str(cart.subtotal),
            "item_key": item_key,
            "redirect_url": reverse("core:cart_detail"),
        }, status=status_code)

    return redirect("core:cart_detail")


def cart_update(request):
    """
    Updates the quantity for a specific cart line item.
    Requires HTTP POST method.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for cart mutations.")

    cart = Cart(request)
    item_key = request.POST.get("item_key")
    quantity_raw = request.POST.get("quantity")

    if not item_key:
        err_msg = "Missing item identifier."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:cart_detail")

    success, message = cart.update_quantity(item_key, quantity_raw)

    if _is_ajax(request):
        status_code = 200 if success else 400
        return JsonResponse({
            "success": success,
            "message": message,
            "cart_total_units": cart.total_units,
            "cart_subtotal": str(cart.subtotal),
        }, status=status_code)

    if success:
        messages.success(request, message)
    else:
        messages.error(request, message)

    return redirect("core:cart_detail")


def cart_remove(request):
    """
    Removes a specific line item from the shopping cart.
    Requires HTTP POST method.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for cart mutations.")

    cart = Cart(request)
    item_key = request.POST.get("item_key")

    if not item_key:
        err_msg = "Missing item identifier."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:cart_detail")

    success, message = cart.remove(item_key)

    if _is_ajax(request):
        status_code = 200 if success else 400
        return JsonResponse({
            "success": success,
            "message": message,
            "cart_total_units": cart.total_units,
            "cart_subtotal": str(cart.subtotal),
        }, status=status_code)

    if success:
        messages.success(request, message)
    else:
        messages.error(request, message)

    return redirect("core:cart_detail")


def cart_clear(request):
    """
    Empties all items from the shopping cart after confirmation.
    Requires HTTP POST method.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for cart mutations.")

    cart = Cart(request)
    success, message = cart.clear()

    if _is_ajax(request):
        return JsonResponse({
            "success": success,
            "message": message,
            "cart_total_units": 0,
            "cart_subtotal": "0.00",
        })

    messages.success(request, message)
    return redirect("core:cart_detail")


# ==============================================================================
# CUSTOMER AUTHENTICATION & PROFILE VIEWS (PROMPT 8)
# ==============================================================================

def account_register(request):
    """
    Customer registration view.
    Validates name, registered mobile number, normalized email, password strength, and terms agreement.
    Signs customer in automatically and merges guest session cart.
    """
    if request.user.is_authenticated:
        return redirect("core:account_dashboard")

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    if request.method == "POST":
        form = CustomerRegistrationForm(request.POST)
        if form.is_valid():
            try:
                user = form.save()
            except IntegrityError as e:
                logger.warning(f"IntegrityError during customer registration: {e}")
                messages.error(
                    request,
                    "An account with this email address or mobile number already exists. Please try signing in."
                )
                return render(request, "account/register.html", {
                    "form": form,
                    "next": next_url,
                })
            except Exception as e:
                logger.exception("Unexpected error saving new customer during registration")
                messages.error(
                    request,
                    "An unexpected error occurred while creating your account. Please try again or contact support."
                )
                return render(request, "account/register.html", {
                    "form": form,
                    "next": next_url,
                })

            try:
                login(request, user, backend="core.backends.EmailOrUsernameModelBackend")
            except Exception as e:
                logger.warning(f"Backend-specific login failed after registration, falling back to standard login: {e}")
                try:
                    login(request, user)
                except Exception:
                    pass

            # Merge any anonymous cart items created before registration
            try:
                cart = Cart(request)
                cart.merge_session_cart()
            except Exception as e:
                logger.warning(f"Guest cart merge failed during registration: {e}")

            name = user.first_name or user.username
            messages.success(request, f"Welcome to Fitmeezo, {name}! Your account has been created successfully.")

            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect("core:account_dashboard")
    else:
        form = CustomerRegistrationForm()

    return render(request, "account/register.html", {
        "form": form,
        "next": next_url,
    })


def account_login(request):
    """
    Customer storefront login view.
    Supports registered mobile number or email address authentication.
    Merges session cart into user's persistent cart upon successful login.
    """
    if request.user.is_authenticated:
        return redirect("core:account_dashboard")

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    if request.method == "POST":
        form = CustomerLoginForm(request.POST)
        if form.is_valid():
            login_identifier = form.cleaned_data.get("mobile_or_email") or form.cleaned_data.get("username_or_email")
            password = form.cleaned_data["password"]
            remember_me = form.cleaned_data.get("remember_me")

            user = authenticate(request, username=login_identifier, password=password)
            if user is not None and user.is_active:
                login(request, user)

                if not remember_me:
                    request.session.set_expiry(0)  # expires on browser close
                else:
                    request.session.set_expiry(1209600)  # 2 weeks

                # Merge anonymous cart into persistent customer cart
                cart = Cart(request)
                cart.merge_session_cart()

                name = user.first_name or user.username
                messages.success(request, f"Welcome back, {name}!")

                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                    return redirect(next_url)
                return redirect("core:account_dashboard")
            else:
                messages.error(request, "Invalid mobile number/email or password. Please check your credentials and try again.")
    else:
        form = CustomerLoginForm()

    return render(request, "account/login.html", {
        "form": form,
        "next": next_url,
    })


def account_logout(request):
    """
    Customer logout view. Requires POST method with CSRF protection.
    Safely clears session without leaking another user's cart.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for logout.")

    logout(request)
    messages.success(request, "You have been securely signed out of your account.")
    return redirect("core:home")


@login_required(login_url="core:account_login")
def account_dashboard(request):
    """
    Customer central account dashboard.
    Summarizes profile info, primary address, cart status, and quick links.
    """
    user = request.user
    profile, _ = CustomerProfile.objects.get_or_create(user=user)
    addresses = Address.objects.filter(user=user)
    default_address = addresses.filter(is_default=True).first() or addresses.first()
    cart = Cart(request)
    reviews_count = ProductReview.objects.filter(user=user).count()
    orders_count = Order.objects.filter(user=user).count()
    recent_orders = Order.objects.filter(user=user).order_by("-created_at")[:3]

    return render(request, "account/dashboard.html", {
        "user": user,
        "profile": profile,
        "addresses_count": addresses.count(),
        "default_address": default_address,
        "cart_total_units": cart.total_units,
        "cart_subtotal": cart.subtotal,
        "reviews_count": reviews_count,
        "orders_count": orders_count,
        "recent_orders": recent_orders,
    })


@login_required(login_url="core:account_login")
def account_profile(request):
    """
    Customer profile details view and update form.
    Allows editing full name, email (with uniqueness check), and optional phone number.
    Displays connected social accounts (Google, Facebook) if any.
    """
    user = request.user
    profile, _ = CustomerProfile.objects.get_or_create(user=user)
    social_accounts = user.socialaccount_set.all()

    if request.method == "POST":
        form = CustomerProfileForm(request.POST, user=user)
        if form.is_valid():
            form.save()
            messages.success(request, "Your profile details have been updated successfully.")
            return redirect("core:account_profile")
    else:
        form = CustomerProfileForm(user=user)

    return render(request, "account/profile.html", {
        "form": form,
        "user": user,
        "profile": profile,
        "social_accounts": social_accounts,
    })


@login_required(login_url="core:account_login")
def account_addresses(request):
    """
    Customer saved addresses listing page.
    """
    addresses = Address.objects.filter(user=request.user)
    return render(request, "account/addresses.html", {
        "addresses": addresses,
    })


@login_required(login_url="core:account_login")
def account_address_add(request):
    """
    Adds a new delivery address for the authenticated customer.
    """
    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            address = form.save(commit=False)
            address.user = request.user
            address.save()
            messages.success(request, "Delivery address added successfully.")
            return redirect("core:account_addresses")
    else:
        form = AddressForm()

    return render(request, "account/address_form.html", {
        "form": form,
        "title": "Add New Delivery Address",
        "action_label": "Save Address",
    })


@login_required(login_url="core:account_login")
def account_address_edit(request, pk):
    """
    Edits an existing delivery address belonging to the authenticated customer.
    Guards against cross-user IDOR access.
    """
    address = get_object_or_404(Address, pk=pk, user=request.user)

    if request.method == "POST":
        form = AddressForm(request.POST, instance=address)
        if form.is_valid():
            form.save()
            messages.success(request, "Delivery address updated successfully.")
            return redirect("core:account_addresses")
    else:
        form = AddressForm(instance=address)

    return render(request, "account/address_form.html", {
        "form": form,
        "title": "Edit Delivery Address",
        "action_label": "Update Address",
        "address": address,
    })


@login_required(login_url="core:account_login")
def account_address_delete(request, pk):
    """
    Deletes a delivery address belonging to the authenticated customer.
    Requires POST method.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for address deletion.")

    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.delete()
    messages.success(request, "Address removed successfully.")
    return redirect("core:account_addresses")


@login_required(login_url="core:account_login")
def account_address_set_default(request, pk):
    """
    Sets a specific address as the primary default address for the customer.
    Requires POST method.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted.")

    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.is_default = True
    address.save()
    messages.success(request, f"'{address.full_name}' has been set as your default delivery address.")
    return redirect("core:account_addresses")


def terms_view(request):
    """
    Renders Fitmeezo Terms of Service & Legal Policies page.
    """
    return render(request, "account/terms.html")


def contact_us_view(request):
    """
    Renders and processes Fitmeezo Customer Support & Contact Us page.
    Includes corporate helpline, email, operating hours, and customer inquiry form.
    """
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            inquiry = form.save(commit=False)
            inquiry.ip_address = get_client_ip(request)
            inquiry.save()
            send_contact_inquiry_email(inquiry)
            messages.success(
                request,
                f"Thank you, {inquiry.full_name}! Your message has been received (Ref #{inquiry.id}). "
                "Our customer care team will review your inquiry and get back to you shortly."
            )
            return redirect("core:contact")
        else:
            messages.error(request, "Please check the highlighted errors in the form below.")
    else:
        initial_data = {}
        if request.user.is_authenticated:
            initial_data["full_name"] = request.user.get_full_name() or request.user.username
            initial_data["email"] = request.user.email
            phone = getattr(request.user, "customer_phone", "")
            if phone:
                initial_data["phone_number"] = phone
        form = ContactForm(initial=initial_data)

    return render(request, "contact.html", {"form": form})


def order_track_view(request):
    """
    Customer Order Tracking Portal with privacy-preserving dual verification.
    Customers look up their order using Order Number + verification (Email or Mobile).
    Authenticated customers can also see their recent active orders.
    """
    order = None
    timeline = []
    packages = []
    verified = False
    searched = False
    form = OrderTrackingForm(request.GET or None)

    order_number = request.GET.get("order_number", "").strip()
    verification = request.GET.get("verification", "").strip()

    if order_number and verification:
        searched = True
        if form.is_valid():
            ord_num = form.cleaned_data["order_number"]
            ver_input = form.cleaned_data["verification"]

            matched_order = Order.objects.filter(order_number__iexact=ord_num).first()
            if matched_order:
                user_email = (matched_order.user.email or "").strip().lower()
                order_phone_digits = re.sub(r"\D", "", matched_order.shipping_phone_number or "")
                input_digits = re.sub(r"\D", "", ver_input)

                is_email_match = ver_input.lower() == user_email and bool(user_email)
                is_phone_match = bool(input_digits) and (
                    input_digits == order_phone_digits or
                    (len(input_digits) >= 10 and order_phone_digits.endswith(input_digits[-10:]))
                )
                is_user_match = request.user.is_authenticated and matched_order.user_id == request.user.id

                if is_email_match or is_phone_match or is_user_match:
                    order = matched_order
                    verified = True
                    timeline = order.status_history.all().order_by("created_at")
                    packages = order.seller_orders.all()
                else:
                    messages.error(
                        request,
                        "We could not find an order matching that order number and contact detail. "
                        "Please verify your order number and contact email/mobile and try again."
                    )
            else:
                messages.error(
                    request,
                    "We could not find an order matching that order number and contact detail. "
                    "Please verify your order number and contact email/mobile and try again."
                )

    recent_orders = []
    if request.user.is_authenticated and not order:
        recent_orders = Order.objects.filter(user=request.user).order_by("-created_at")[:5]

    return render(request, "orders/track.html", {
        "form": form,
        "order": order,
        "timeline": timeline,
        "packages": packages,
        "verified": verified,
        "searched": searched,
        "recent_orders": recent_orders,
    })


def shipping_policy_view(request):
    """Renders Fitmeezo Official Shipping & Delivery Policy."""
    return render(request, "policies/shipping.html")


def return_policy_view(request):
    """Renders Fitmeezo Official 7-Day Return & Replacement Policy."""
    return render(request, "policies/returns.html")


def faqs_view(request):
    """Renders Categorized and Searchable Frequently Asked Questions."""
    return render(request, "policies/faqs.html")


def about_us_view(request):
    """Renders About Fitmeezo Company Story, Mission, and Core Values."""
    return render(request, "about/about_us.html")


def careers_view(request):
    """Renders Fitmeezo Careers & Talent Network."""
    return render(request, "about/careers.html")


def guarantee_view(request):
    """Renders 100% Genuine Authenticity & Quality Guarantee."""
    return render(request, "about/guarantee.html")


def press_view(request):
    """Renders Fitmeezo Media, Newsroom & Official Announcements."""
    return render(request, "about/press.html")


def privacy_policy_view(request):
    """Renders Comprehensive Fitmeezo Privacy & Data Protection Policy."""
    return render(request, "policies/privacy.html")


def refund_cancellation_policy_view(request):
    """Renders Fitmeezo Refund & Order Cancellation Policy."""
    return render(request, "policies/refund_cancellation.html")


def cookie_policy_view(request):
    """Renders Fitmeezo Cookie, Session & Tracking Policy."""
    return render(request, "policies/cookies.html")


def grievance_policy_view(request):
    """Renders Consumer Grievance Redressal Officer Information (India)."""
    return render(request, "policies/grievance.html")


# ==============================================================================
# WISHLIST VIEWS & DRF API (PROMPT 10)
# ==============================================================================

@login_required(login_url="core:account_login")
def wishlist_detail(request):
    """
    Renders the authenticated customer's Wishlist page.
    Displays saved products, current prices, compare-at pricing, stock status,
    add-to-cart controls, and removal actions.
    """
    items = (
        WishlistItem.objects.filter(user=request.user)
        .select_related("product", "product__category")
        .prefetch_related("product__variants")
        .order_by("-created_at")
    )
    return render(request, "wishlist/wishlist.html", {
        "wishlist_items": items,
        "wishlist_count": items.count(),
        "is_wishlist_page": True,
    })


def wishlist_toggle(request):
    """
    Toggles a product's presence in the authenticated customer's wishlist.
    Requires HTTP POST method.
    If customer is anonymous, redirects to login with safe next URL or returns 401 JSON for AJAX.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for wishlist mutations.")

    # Anonymous user handling
    if not request.user.is_authenticated:
        safe_next = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("core:wishlist_detail")
        if not url_has_allowed_host_and_scheme(safe_next, allowed_hosts={request.get_host()}):
            safe_next = reverse("core:wishlist_detail")
        login_url = f"{reverse('core:account_login')}?next={quote(safe_next)}"

        if _is_ajax(request):
            return JsonResponse({
                "success": False,
                "login_required": True,
                "login_url": login_url,
                "message": "Please log in to save items to your wishlist.",
            }, status=401)
        messages.info(request, "Please log in to save items to your wishlist.")
        return redirect(login_url)

    # Parse product_id
    product_id = request.POST.get("product_id")
    if not product_id and request.body:
        try:
            data = json.loads(request.body)
            product_id = data.get("product_id")
        except Exception:
            pass

    try:
        product_id = int(product_id)
        product = Product.objects.get(pk=product_id)
    except (TypeError, ValueError, Product.DoesNotExist):
        err_msg = "The specified product could not be found."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:wishlist_detail")

    if not product.is_active:
        err_msg = f"'{product.name}' is currently unavailable."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:wishlist_detail")

    # Check existing item
    existing = WishlistItem.objects.filter(user=request.user, product=product).first()
    if existing:
        existing.delete()
        is_in_wishlist = False
        msg = f"Removed '{product.name}' from your wishlist."
    else:
        try:
            WishlistItem.objects.create(user=request.user, product=product)
        except IntegrityError:
            pass
        is_in_wishlist = True
        msg = f"Saved '{product.name}' to your wishlist!"

    wishlist_count = WishlistItem.objects.filter(user=request.user).count()

    if _is_ajax(request):
        return JsonResponse({
            "success": True,
            "is_in_wishlist": is_in_wishlist,
            "wishlist_count": wishlist_count,
            "product_id": product.id,
            "message": msg,
        })

    messages.success(request, msg)
    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("core:wishlist_detail")


def wishlist_add(request):
    """
    Explicitly adds a product to the authenticated customer's wishlist.
    Requires HTTP POST method.
    Idempotent: Prevents duplicate entries.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for wishlist mutations.")

    if not request.user.is_authenticated:
        safe_next = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("core:wishlist_detail")
        if not url_has_allowed_host_and_scheme(safe_next, allowed_hosts={request.get_host()}):
            safe_next = reverse("core:wishlist_detail")
        login_url = f"{reverse('core:account_login')}?next={quote(safe_next)}"

        if _is_ajax(request):
            return JsonResponse({
                "success": False,
                "login_required": True,
                "login_url": login_url,
                "message": "Please log in to save items to your wishlist.",
            }, status=401)
        messages.info(request, "Please log in to save items to your wishlist.")
        return redirect(login_url)

    product_id = request.POST.get("product_id")
    if not product_id and request.body:
        try:
            data = json.loads(request.body)
            product_id = data.get("product_id")
        except Exception:
            pass

    try:
        product_id = int(product_id)
        product = Product.objects.get(pk=product_id)
    except (TypeError, ValueError, Product.DoesNotExist):
        err_msg = "The specified product could not be found."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:wishlist_detail")

    if not product.is_active:
        err_msg = f"'{product.name}' is currently unavailable."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:wishlist_detail")

    item, created = WishlistItem.objects.get_or_create(user=request.user, product=product)
    wishlist_count = WishlistItem.objects.filter(user=request.user).count()

    if created:
        msg = f"Saved '{product.name}' to your wishlist!"
    else:
        msg = f"'{product.name}' is already in your wishlist."

    if _is_ajax(request):
        return JsonResponse({
            "success": True,
            "created": created,
            "is_in_wishlist": True,
            "wishlist_count": wishlist_count,
            "product_id": product.id,
            "message": msg,
        })

    messages.success(request, msg)
    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("core:wishlist_detail")


@login_required(login_url="core:account_login")
def wishlist_remove(request, pk=None):
    """
    Removes a specific wishlist item belonging to the authenticated customer.
    Requires HTTP POST method.
    Supports item pk in URL or product_id/item_id in POST body.
    Guards against cross-customer IDOR access.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted for wishlist mutations.")

    if pk:
        item = get_object_or_404(WishlistItem, pk=pk, user=request.user)
    else:
        product_id = request.POST.get("product_id") or request.POST.get("item_id")
        item = get_object_or_404(WishlistItem, product_id=product_id, user=request.user)

    product_name = item.product.name
    product_id = item.product_id
    item.delete()

    wishlist_count = WishlistItem.objects.filter(user=request.user).count()
    msg = f"Removed '{product_name}' from your wishlist."

    if _is_ajax(request):
        return JsonResponse({
            "success": True,
            "wishlist_count": wishlist_count,
            "product_id": product_id,
            "message": msg,
        })

    messages.success(request, msg)
    return redirect("core:wishlist_detail")


@login_required(login_url="core:account_login")
def wishlist_move_to_cart(request, pk):
    """
    Adds a wishlist product to the shopping cart reusing the existing Cart service.
    Validates product status, variant, and available inventory.
    Requires HTTP POST method.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"], content="Only POST requests are permitted.")

    item = get_object_or_404(WishlistItem, pk=pk, user=request.user)
    product = item.product

    if not product.is_active:
        err_msg = f"'{product.name}' is currently unavailable."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:wishlist_detail")

    if not product.is_in_stock:
        err_msg = f"'{product.name}' is currently out of stock."
        if _is_ajax(request):
            return JsonResponse({"success": False, "message": err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect("core:wishlist_detail")

    # Variant resolution
    variant_id = request.POST.get("variant_id")
    variant = None
    if variant_id and str(variant_id).strip():
        try:
            variant = ProductVariant.objects.get(pk=variant_id, product=product, is_active=True)
            if not variant.is_in_stock:
                err_msg = f"The selected option for '{product.name}' is out of stock."
                if _is_ajax(request):
                    return JsonResponse({"success": False, "message": err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect("core:wishlist_detail")
        except ProductVariant.DoesNotExist:
            err_msg = "Selected option does not exist."
            if _is_ajax(request):
                return JsonResponse({"success": False, "message": err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect("core:wishlist_detail")
    elif product.has_variants:
        # Pick the first active in-stock variant
        variant = product.variants.filter(is_active=True, stock_quantity__gt=0).first()
        if not variant:
            err_msg = f"'{product.name}' is currently out of stock."
            if _is_ajax(request):
                return JsonResponse({"success": False, "message": err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect("core:wishlist_detail")

    cart = Cart(request)
    success, cart_msg, item_key = cart.add(product, variant=variant, quantity=1)

    if _is_ajax(request):
        status_code = 200 if success else 400
        return JsonResponse({
            "success": success,
            "message": cart_msg,
            "cart_total_units": cart.total_units,
            "cart_subtotal": str(cart.subtotal),
            "item_key": item_key,
        }, status=status_code)

    if success:
        messages.success(request, cart_msg)
    else:
        messages.error(request, cart_msg)

    return redirect("core:wishlist_detail")


@api_view(["GET", "POST"])
def wishlist_api(request):
    """
    Authenticated customer API endpoint for viewing and adding to personal wishlist.
    GET: Returns list of current customer's wishlist items.
    POST: Adds a product to current customer's wishlist.
    """
    if not request.user.is_authenticated:
        return Response(
            {"detail": "Authentication credentials were not provided."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if request.method == "GET":
        items = (
            WishlistItem.objects.filter(user=request.user)
            .select_related("product")
            .order_by("-created_at")
        )
        serializer = WishlistItemSerializer(items, many=True)
        return Response({
            "count": items.count(),
            "items": serializer.data,
        })

    elif request.method == "POST":
        product_id = request.data.get("product_id")
        if not product_id:
            return Response(
                {"product_id": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            product = Product.objects.get(pk=product_id)
        except (Product.DoesNotExist, ValueError, TypeError):
            return Response(
                {"product_id": ["Product does not exist."]},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not product.is_active:
            return Response(
                {"detail": "This product is currently inactive."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        item, created = WishlistItem.objects.get_or_create(user=request.user, product=product)
        serializer = WishlistItemSerializer(item)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@api_view(["DELETE"])
def wishlist_api_detail(request, pk):
    """
    Authenticated customer API endpoint for removing an item from their wishlist.
    Guards customer ownership.
    """
    if not request.user.is_authenticated:
        return Response(
            {"detail": "Authentication credentials were not provided."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        item = WishlistItem.objects.get(pk=pk, user=request.user)
    except WishlistItem.DoesNotExist:
        return Response(
            {"detail": "Wishlist item not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    item.delete()
    return Response(
        {"detail": "Item successfully removed from wishlist."},
        status=status.HTTP_204_NO_CONTENT,
    )


# ==============================================================================
# SOCIAL OAUTH LOGIN VIEWS (PROMPT 11)
# ==============================================================================

def oauth_google_login(request):
    """
    Initiates Google OAuth 2.0 / OpenID Connect login via allauth.
    If Google credentials are not configured in environment or database, displays
    a safe setup notice and redirects to the login page.
    """
    client_id = getattr(settings, "GOOGLE_CLIENT_ID", "").strip()
    secret = getattr(settings, "GOOGLE_CLIENT_SECRET", "").strip()
    has_credentials = bool(client_id and secret and not client_id.lower().startswith(("replace_", "your_", "todo")))
    if not has_credentials:
        try:
            from allauth.socialaccount.models import SocialApp
            has_credentials = SocialApp.objects.filter(provider="google").exists()
        except Exception:
            has_credentials = False

    if not has_credentials:
        messages.info(
            request,
            "Google Sign-In is not configured yet. "
            "Please configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in your .env file."
        )
        return redirect("core:account_login")

    next_url = request.GET.get("next") or request.POST.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        request.session["socialaccount_next"] = next_url

    return redirect(reverse("google_login"))


# ==============================================================================
# CHECKOUT & ORDER MANAGEMENT VIEWS
# ==============================================================================

@login_required(login_url="core:account_login")
def checkout_view(request):
    """
    Fitmeezo Customer Checkout view.
    Handles shipping address selection or inline address creation,
    recalculates all product prices, quantities, shipping charges, and totals on the server,
    verifies stock availability and applies database locking (select_for_update),
    supports Cash on Delivery (COD) with unpaid payment status,
    records immutable address/item snapshots, and prevents duplicate submissions.
    """
    cart = Cart(request)

    if request.method == "POST":
        idempotency_token = request.POST.get("idempotency_token", "").strip()

        # Check duplicate submission first to handle repeat requests after cart clearance
        if idempotency_token:
            existing_order = Order.objects.filter(idempotency_key=idempotency_token, user=request.user).first()
            if existing_order:
                if existing_order.payment_method == Order.PAYMENT_METHOD_RAZORPAY and existing_order.payment_status == Order.PAYMENT_STATUS_UNPAID:
                    return redirect("core:razorpay_payment_page", order_number=existing_order.order_number)
                messages.info(request, f"Your order #{existing_order.order_number} has already been placed.")
                return redirect("core:order_confirmation", order_number=existing_order.order_number)

    if cart.is_empty:
        messages.warning(request, "Your shopping cart is empty. Please add items before checking out.")
        return redirect("core:cart_detail")

    if cart.has_warnings or not cart.is_checkout_ready:
        messages.warning(
            request,
            "Some items in your cart are currently out of stock or exceed available quantities. "
            "Please update your cart to proceed."
        )
        return redirect("core:cart_detail")

    user_addresses = Address.objects.filter(user=request.user).order_by("-is_default", "-updated_at")
    default_address = user_addresses.filter(is_default=True).first() or user_addresses.first()

    # Recalculate server-side totals
    cart_lines = cart.get_lines()
    subtotal = cart.subtotal
    shipping_fee = Decimal("0.00") if subtotal >= FREE_SHIPPING_THRESHOLD else STANDARD_SHIPPING_FEE
    total_payable = (subtotal + shipping_fee).quantize(Decimal("0.01"))
    total_discount = cart.total_discount
    razorpay_enabled = getattr(settings, "RAZORPAY_ENABLED", True) and bool(
        getattr(settings, "RAZORPAY_KEY_ID", "")
        and getattr(settings, "RAZORPAY_KEY_SECRET", "")
        and not getattr(settings, "RAZORPAY_KEY_ID", "").lower().startswith(("replace_", "your_", "todo"))
        and not getattr(settings, "RAZORPAY_KEY_SECRET", "").lower().startswith(("replace_", "your_", "todo"))
    )

    if request.method == "POST":
        address_mode = request.POST.get("address_mode", "").strip() or ("existing" if user_addresses.exists() else "new")
        address = None
        new_address_form = None

        if address_mode == "new" or not user_addresses.exists():
            new_address_form = AddressForm(request.POST)
            if new_address_form.is_valid():
                address = new_address_form.save(commit=False)
                address.user = request.user
                address.save()
            else:
                messages.error(request, "Please check the delivery address details and resolve any highlighted errors.")
                context = {
                    "cart": cart,
                    "cart_lines": cart_lines,
                    "subtotal": subtotal,
                    "shipping_fee": shipping_fee,
                    "total_payable": total_payable,
                    "total_discount": total_discount,
                    "free_shipping_threshold": FREE_SHIPPING_THRESHOLD,
                    "user_addresses": user_addresses,
                    "default_address": default_address,
                    "address_form": new_address_form,
                    "selected_address_id": None,
                    "address_mode": "new",
                    "idempotency_token": idempotency_token or uuid.uuid4().hex,
                    "razorpay_enabled": razorpay_enabled,
                }
                return render(request, "checkout/checkout.html", context, status=400)
        else:
            address_id = request.POST.get("address_id") or request.POST.get("selected_address")
            if address_id:
                address = user_addresses.filter(pk=address_id).first()
            if not address:
                messages.error(request, "Please select an existing delivery address or enter a new destination.")
                context = {
                    "cart": cart,
                    "cart_lines": cart_lines,
                    "subtotal": subtotal,
                    "shipping_fee": shipping_fee,
                    "total_payable": total_payable,
                    "total_discount": total_discount,
                    "free_shipping_threshold": FREE_SHIPPING_THRESHOLD,
                    "user_addresses": user_addresses,
                    "default_address": default_address,
                    "address_form": AddressForm(),
                    "selected_address_id": None,
                    "address_mode": "existing",
                    "idempotency_token": idempotency_token or uuid.uuid4().hex,
                    "razorpay_enabled": razorpay_enabled,
                }
                return render(request, "checkout/checkout.html", context, status=400)

        # Support COD and Razorpay payment methods
        payment_method = request.POST.get("payment_method", "cod").strip().lower()
        if payment_method not in (Order.PAYMENT_METHOD_COD, Order.PAYMENT_METHOD_RAZORPAY):
            messages.error(request, "Please choose a valid payment method.")
            return redirect("core:checkout")

        if payment_method == Order.PAYMENT_METHOD_RAZORPAY and not razorpay_enabled:
            messages.error(
                request,
                "Online payment gateway is temporarily unavailable. Please place your order using Cash on Delivery."
            )
            return redirect("core:checkout")

        customer_notes = request.POST.get("customer_notes", "").strip()[:500]

        # Execute stock locking, inventory deduction, order snapshotting via shared service
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
        except CheckoutValidationError as cve:
            messages.error(request, str(cve))
            return redirect("core:cart_detail")
        except CheckoutError as ce:
            messages.error(request, f"Order placement failed: {str(ce)}")
            return redirect("core:checkout")
        except Exception as exc:
            logger.error(f"Unexpected error in checkout_view: {exc}", exc_info=True)
            messages.error(request, "An unexpected error occurred while placing your order. Please try again.")
            return redirect("core:checkout")

        if is_duplicate:
            if order.payment_method == Order.PAYMENT_METHOD_RAZORPAY and order.payment_status == Order.PAYMENT_STATUS_UNPAID:
                return redirect("core:razorpay_payment_page", order_number=order.order_number)
            messages.info(request, f"Your order #{order.order_number} has already been placed.")
            return redirect("core:order_confirmation", order_number=order.order_number)

        # Dispatch order confirmation email for new COD order
        if payment_method != Order.PAYMENT_METHOD_RAZORPAY:
            send_order_confirmation_email(order)
            messages.success(request, f"Order #{order.order_number} placed successfully!")
            return redirect("core:order_confirmation", order_number=order.order_number)

        # For Razorpay, redirect to the online payment interface
        return redirect("core:razorpay_payment_page", order_number=order.order_number)

    # GET request
    idempotency_token = uuid.uuid4().hex
    context = {
        "cart": cart,
        "cart_lines": cart_lines,
        "subtotal": subtotal,
        "shipping_fee": shipping_fee,
        "total_payable": total_payable,
        "total_discount": total_discount,
        "free_shipping_threshold": FREE_SHIPPING_THRESHOLD,
        "user_addresses": user_addresses,
        "default_address": default_address,
        "address_form": AddressForm(),
        "selected_address_id": default_address.pk if default_address else None,
        "address_mode": "existing" if user_addresses.exists() else "new",
        "idempotency_token": idempotency_token,
        "razorpay_enabled": razorpay_enabled,
    }
    return render(request, "checkout/checkout.html", context)


@login_required(login_url="core:account_login")
def razorpay_payment_page(request, order_number):
    """
    Renders Razorpay online checkout interface for an unpaid order.
    Enforces customer ownership and checks that order is eligible for online payment.
    """
    order = get_object_or_404(Order, order_number=order_number, user=request.user)

    if order.payment_status == Order.PAYMENT_STATUS_PAID:
        messages.info(request, f"Order #{order.order_number} has already been paid.")
        return redirect("core:order_confirmation", order_number=order.order_number)

    if order.status == Order.STATUS_CANCELLED:
        messages.warning(request, f"Order #{order.order_number} is cancelled and cannot be paid.")
        return redirect("core:order_detail", order_number=order.order_number)

    razorpay_enabled = getattr(settings, "RAZORPAY_ENABLED", True) and bool(
        getattr(settings, "RAZORPAY_KEY_ID", "")
        and getattr(settings, "RAZORPAY_KEY_SECRET", "")
        and not getattr(settings, "RAZORPAY_KEY_ID", "").lower().startswith(("replace_", "your_", "todo"))
        and not getattr(settings, "RAZORPAY_KEY_SECRET", "").lower().startswith(("replace_", "your_", "todo"))
    )
    if not razorpay_enabled:
        messages.error(request, "Online payments are currently disabled.")
        return redirect("core:order_detail", order_number=order.order_number)

    items = order.items.select_related("product", "variant").all()
    amount_in_paise = int((order.total_amount * 100).to_integral_value())

    context = {
        "order": order,
        "items": items,
        "amount_in_paise": amount_in_paise,
        "razorpay_key_id": getattr(settings, "RAZORPAY_KEY_ID", ""),
        "razorpay_currency": getattr(settings, "RAZORPAY_CURRENCY", "INR"),
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "orders/payment_razorpay.html", context)


@login_required(login_url="core:account_login")
@require_POST
def razorpay_payment_verify(request):
    """
    Server-side cryptographic signature verification endpoint.
    Verifies that razorpay_signature matches HMAC-SHA256(order_id|payment_id, secret).
    Never marks order as paid based on client assertion alone.
    """
    order_number = request.POST.get("order_number", "").strip()
    razorpay_payment_id = request.POST.get("razorpay_payment_id", "").strip()
    razorpay_order_id = request.POST.get("razorpay_order_id", "").strip()
    razorpay_signature = request.POST.get("razorpay_signature", "").strip()

    order = get_object_or_404(Order, order_number=order_number, user=request.user)

    # Ensure this order was created for Razorpay
    if order.payment_method != Order.PAYMENT_METHOD_RAZORPAY:
        messages.error(request, "Invalid payment method for online verification.")
        return redirect("core:order_detail", order_number=order.order_number)

    # Idempotency: Prevent duplicate payment processing
    if order.payment_status == Order.PAYMENT_STATUS_PAID:
        messages.info(request, f"Payment for order #{order.order_number} has already been recorded.")
        return redirect("core:order_confirmation", order_number=order.order_number)

    if not razorpay_payment_id or not razorpay_order_id or not razorpay_signature:
        messages.error(request, "Incomplete payment callback parameters.")
        return redirect("core:order_detail", order_number=order.order_number)

    if not order.razorpay_order_id or order.razorpay_order_id != razorpay_order_id:
        logger.warning(
            f"Razorpay order mismatch for #{order.order_number}: db={order.razorpay_order_id}, got={razorpay_order_id}"
        )
        messages.error(request, "Payment order identifier verification failed.")
        return redirect("core:order_detail", order_number=order.order_number)

    # Verify signature using official Razorpay utility
    key_id = getattr(settings, "RAZORPAY_KEY_ID", "")
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")
    client = razorpay.Client(auth=(key_id, key_secret))

    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature,
        })
    except Exception as exc:
        logger.warning(
            f"Cryptographic signature verification failed for #{order.order_number}: {exc}"
        )
        messages.error(
            request,
            "Security verification failed: payment signature could not be verified. If amount was debited, contact customer support."
        )
        return redirect("core:order_detail", order_number=order.order_number)

    # Atomically lock and mark order as paid
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
        if not locked_order or locked_order.payment_status == Order.PAYMENT_STATUS_PAID:
            messages.info(request, f"Payment for order #{order.order_number} has already been recorded.")
            return redirect("core:order_confirmation", order_number=order.order_number)

        locked_order.mark_as_paid(payment_id=razorpay_payment_id, signature=razorpay_signature)

        # Post verified prepaid sales to seller ledger (Phase 7)
        try:
            from core.ledger_services import post_order_sale_to_ledger
            for so in locked_order.seller_orders.all():
                post_order_sale_to_ledger(so, actor=request.user, force_posted=True)
        except Exception as ledg_err:
            logger.warning(f"Failed to post ledger entries for verified order #{locked_order.order_number}: {ledg_err}")

    # Clear user shopping cart if any items remain

    Cart(request).clear()

    # Dispatch payment success email notification
    send_payment_success_email(locked_order)

    messages.success(request, f"Payment verified successfully! Order #{locked_order.order_number} is confirmed.")
    return redirect("core:order_confirmation", order_number=locked_order.order_number)


@login_required(login_url="core:account_login")
@require_POST
def razorpay_payment_failed(request):
    """
    Handles cancelled or failed online transactions.
    Restores inventory stock for the cancelled order items and records failed state.
    """
    order_number = request.POST.get("order_number", "").strip()
    error_code = request.POST.get("error_code", "PAYMENT_FAILED").strip()
    error_description = request.POST.get("error_description", "Payment was not completed.").strip()

    order = get_object_or_404(Order, order_number=order_number, user=request.user)

    if order.payment_status != Order.PAYMENT_STATUS_PAID and order.status != Order.STATUS_CANCELLED:
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
            if not locked_order or locked_order.payment_status == Order.PAYMENT_STATUS_PAID or locked_order.status == Order.STATUS_CANCELLED:
                messages.warning(request, f"Order #{order.order_number} is already finalized.")
                return redirect("core:order_detail", order_number=order.order_number)

            # Restore inventory units exactly once
            if not locked_order.inventory_restored:
                for item in locked_order.items.select_related("product", "variant").all():
                    if item.variant:
                        ProductVariant.objects.filter(pk=item.variant.pk).update(
                            stock_quantity=F("stock_quantity") + item.quantity
                        )
                        if item.product:
                            Product.objects.filter(pk=item.product.pk).update(
                                stock_quantity=F("stock_quantity") + item.quantity
                            )
                    elif item.product:
                        Product.objects.filter(pk=item.product.pk).update(
                            stock_quantity=F("stock_quantity") + item.quantity
                        )
                locked_order.inventory_restored = True

            now_ts = timezone.now()
            locked_order.payment_status = Order.PAYMENT_STATUS_FAILED
            locked_order.status = Order.STATUS_CANCELLED
            locked_order.refund_status = Order.REFUND_STATUS_NOT_APPLICABLE
            locked_order.cancelled_at = now_ts
            locked_order.cancelled_by = "customer"
            locked_order.cancellation_reason = error_description[:255]
            locked_order.save(update_fields=[
                "payment_status", "status", "inventory_restored", "refund_status",
                "cancelled_at", "cancelled_by", "cancellation_reason", "updated_at"
            ])

            # Synchronize eligible child SellerOrders to CANCELLED within the same atomic transaction
            locked_order.seller_orders.filter(
                status__in=[
                    SellerOrder.STATUS_PENDING,
                    SellerOrder.STATUS_PROCESSING,
                    SellerOrder.STATUS_READY_TO_SHIP,
                ]
            ).update(
                status=SellerOrder.STATUS_CANCELLED,
                cancelled_at=now_ts,
                updated_at=now_ts,
            )

        # Send failure email notification
        send_payment_failed_email(locked_order, reason=error_description)

    messages.warning(
        request,
        f"Payment for order #{order.order_number} could not be completed: {error_description}"
    )
    return redirect("core:order_detail", order_number=order.order_number)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """
    Razorpay Webhook receiver endpoint.
    Cryptographically verifies the webhook signature and processes asynchronous payment events.
    """
    webhook_secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        return HttpResponseForbidden("Webhook secret not configured.")

    webhook_signature = request.headers.get("X-Razorpay-Signature", "")
    if not webhook_signature:
        return HttpResponseForbidden("Missing webhook signature header.")

    payload = request.body.decode("utf-8")
    client = razorpay.Client(
        auth=(getattr(settings, "RAZORPAY_KEY_ID", ""), getattr(settings, "RAZORPAY_KEY_SECRET", ""))
    )

    try:
        client.utility.verify_webhook_signature(payload, webhook_signature, webhook_secret)
    except Exception as exc:
        logger.warning(f"Razorpay webhook signature verification error: {exc}")
        return HttpResponseForbidden("Invalid webhook signature.")

    try:
        data = json.loads(payload)
    except Exception:
        return HttpResponse("Invalid JSON", status=400)

    event = data.get("event", "")
    logger.info(f"Razorpay webhook received event: {event}")

    if event in ("order.paid", "payment.captured"):
        payment_entity = data.get("payload", {}).get("payment", {}).get("entity", {})
        rp_order_id = payment_entity.get("order_id")
        rp_payment_id = payment_entity.get("id")

        if rp_order_id:
            order = Order.objects.filter(razorpay_order_id=rp_order_id).first()
            if order and order.payment_status != Order.PAYMENT_STATUS_PAID:
                order.mark_as_paid(payment_id=rp_payment_id)
                send_payment_success_email(order)
        return HttpResponse("OK", status=200)

    elif event in ("refund.processed", "refund.created", "refund.failed", "refund.speed_changed"):
        refund_entity = data.get("payload", {}).get("refund", {}).get("entity", {})
        rfnd_id = refund_entity.get("id", "").strip()
        payment_id = refund_entity.get("payment_id", "").strip()
        amount_paise = refund_entity.get("amount", 0)
        amount_inr = (
            Decimal(str(amount_paise / 100)).quantize(Decimal("0.01"))
            if amount_paise
            else Decimal("0.00")
        )
        notes = refund_entity.get("notes", {}) or {}
        order_number = notes.get("order_number", "").strip()

        # Locate target Order through authoritative IDs (refund ID, payment ID, or verified order number)
        target_order = None
        if rfnd_id:
            target_order = Order.objects.filter(razorpay_refund_id=rfnd_id).first()
            if not target_order:
                ref_match = RefundRecord.objects.filter(external_reference=rfnd_id).select_related("order").first()
                if ref_match:
                    target_order = ref_match.order

        if not target_order and payment_id:
            target_order = Order.objects.filter(razorpay_payment_id=payment_id).first()

        if not target_order and order_number:
            candidate_order = Order.objects.filter(order_number=order_number).first()
            if candidate_order:
                # Fallback to order_number requires positive verification that payment_id matches candidate order
                if not payment_id or not candidate_order.razorpay_payment_id or candidate_order.razorpay_payment_id != payment_id:
                    logger.error(
                        f"Razorpay webhook {event}: Cross-order safety reject! "
                        f"Webhook for order_number '{order_number}' has unverified or mismatched payment_id "
                        f"'{payment_id}' vs order payment_id '{getattr(candidate_order, 'razorpay_payment_id', None)}'."
                    )
                    return HttpResponse("Payment identity unverified", status=200)
                target_order = candidate_order

        if not target_order:
            logger.warning(
                f"Razorpay webhook {event}: no matching Order found for rfnd={rfnd_id}, pay={payment_id}, order={order_number}."
            )
            return HttpResponse("Order not found", status=200)

        # Authoritative payment identity verification:
        # Every refund event (refund.created, refund.processed, refund.failed)
        # MUST have a non-empty payment_id that strictly matches the target order's razorpay_payment_id.
        # Even if the order was located via refund ID, unverified or mismatched payment identity is rejected.
        if not payment_id or not target_order.razorpay_payment_id or target_order.razorpay_payment_id != payment_id:
            logger.error(
                f"Razorpay webhook {event}: Cross-order / unverified payment identity reject! "
                f"Target Order #{target_order.order_number} razorpay_payment_id '{getattr(target_order, 'razorpay_payment_id', '')}' "
                f"does not match webhook payment_id '{payment_id}'. Aborting without updating order."
            )
            return HttpResponse("Payment identity unverified or mismatched", status=200)

        with transaction.atomic():
            locked_order = Order.objects.select_for_update().get(pk=target_order.pk)

            if event == "refund.processed":
                # Find or associate RefundRecord for this specific refund transaction:
                # 1. Exact match by external_reference
                ref_rec = RefundRecord.objects.select_for_update().filter(
                    order=locked_order, external_reference=rfnd_id
                ).first()

                # 2. Verified match by internal voucher reference (receipt or notes.refund_reference) ONLY
                if not ref_rec and rfnd_id:
                    receipt_ref = refund_entity.get("receipt", "").strip() or notes.get("refund_reference", "").strip()
                    if receipt_ref:
                        ref_rec = RefundRecord.objects.select_for_update().filter(
                            order=locked_order,
                            refund_reference=receipt_ref,
                            external_reference="",
                        ).first()

                # Idempotency check: if this refund ID is already settled, do not duplicate amount or notifications
                if ref_rec and ref_rec.status == RefundRecord.STATUS_PROCESSED and ref_rec.external_reference == rfnd_id:
                    logger.info(
                        f"Razorpay webhook refund.processed: #{locked_order.order_number} refund {rfnd_id} already processed (idempotent)."
                    )
                    return HttpResponse("OK", status=200)

                now_ts = timezone.now()
                effective_amount = (
                    amount_inr if amount_inr > Decimal("0.00")
                    else (ref_rec.approved_amount if ref_rec else Decimal("0.00"))
                )

                if ref_rec:
                    ref_rec.external_reference = rfnd_id or ref_rec.external_reference
                    ref_rec.status = RefundRecord.STATUS_PROCESSED
                    if effective_amount > Decimal("0.00"):
                        ref_rec.approved_amount = effective_amount
                    ref_rec.processed_at = now_ts
                    ref_rec.audit_note = (
                        f"Settlement confirmed via Razorpay webhook ({event}): {rfnd_id}."
                    )
                    ref_rec.save(update_fields=[
                        "external_reference", "status", "approved_amount", "processed_at", "audit_note", "updated_at"
                    ])
                else:
                    date_str = now_ts.strftime("%Y%m%d")
                    rand_id = uuid.uuid4().hex[:6].upper()
                    ref_rec = RefundRecord.objects.create(
                        refund_reference=f"REF-{date_str}-{rand_id}",
                        external_reference=rfnd_id,
                        order=locked_order,
                        requested_amount=effective_amount,
                        approved_amount=effective_amount,
                        currency=locked_order.currency or "INR",
                        payment_method=Order.PAYMENT_METHOD_RAZORPAY,
                        status=RefundRecord.STATUS_PROCESSED,
                        processed_at=now_ts,
                        audit_note=f"Settlement recorded via Razorpay webhook ({event}): {rfnd_id}.",
                    )

                # Cumulative processed refunds calculation across all RefundRecords for this order
                cumulative_refunded = (
                    RefundRecord.objects.filter(
                        order=locked_order,
                        status=RefundRecord.STATUS_PROCESSED,
                    ).aggregate(total=Sum("approved_amount"))["total"]
                    or Decimal("0.00")
                )

                locked_order.refund_amount = cumulative_refunded
                if rfnd_id:
                    locked_order.razorpay_refund_id = rfnd_id
                locked_order.refunded_at = now_ts
                locked_order.refund_failure_reason = ""

                # Full refund vs Partial refund status transitions
                if cumulative_refunded >= locked_order.total_amount and locked_order.total_amount > Decimal("0.00"):
                    locked_order.payment_status = Order.PAYMENT_STATUS_REFUNDED
                    locked_order.refund_status = Order.REFUND_STATUS_PROCESSED
                else:
                    # Partial refund leaves payment_status as PAID and refund_status as PROCESSING
                    if locked_order.payment_status != Order.PAYMENT_STATUS_REFUNDED:
                        locked_order.payment_status = Order.PAYMENT_STATUS_PAID
                    locked_order.refund_status = Order.REFUND_STATUS_PROCESSING

                locked_order.save(update_fields=[
                    "refund_status",
                    "payment_status",
                    "refund_amount",
                    "razorpay_refund_id",
                    "refunded_at",
                    "refund_failure_reason",
                    "updated_at",
                ])

                try:
                    from core.emails import send_order_refund_email
                    send_order_refund_email(locked_order, refund_record=ref_rec)
                except Exception as mail_err:
                    logger.warning(
                        f"Failed to send webhook refund email for #{locked_order.order_number}: {mail_err}"
                    )

            elif event == "refund.created":
                # Find or associate RefundRecord for this specific refund transaction
                # 1. Exact match by external_reference
                ref_rec = RefundRecord.objects.select_for_update().filter(
                    order=locked_order, external_reference=rfnd_id
                ).first()

                # 2. Verified match by internal voucher reference (receipt or notes.refund_reference) ONLY
                if not ref_rec and rfnd_id:
                    receipt_ref = refund_entity.get("receipt", "").strip() or notes.get("refund_reference", "").strip()
                    if receipt_ref:
                        ref_rec = RefundRecord.objects.select_for_update().filter(
                            order=locked_order,
                            refund_reference=receipt_ref,
                            external_reference="",
                        ).first()

                # Anti-downgrade check: if this specific refund is already processed, ignore out-of-order created event
                if ref_rec and ref_rec.status == RefundRecord.STATUS_PROCESSED:
                    logger.info(
                        f"Razorpay webhook refund.created: #{locked_order.order_number} refund {rfnd_id} already processed. Ignoring."
                    )
                    return HttpResponse("OK", status=200)

                # Order-level anti-downgrade check: if order is fully processed, do not change order status
                if locked_order.refund_status != Order.REFUND_STATUS_PROCESSED:
                    locked_order.refund_status = Order.REFUND_STATUS_PROCESSING
                    if rfnd_id and not locked_order.razorpay_refund_id:
                        locked_order.razorpay_refund_id = rfnd_id
                    locked_order.save(update_fields=[
                        "refund_status", "razorpay_refund_id", "updated_at"
                    ])

                if ref_rec:
                    ref_rec.external_reference = rfnd_id or ref_rec.external_reference
                    if ref_rec.status != RefundRecord.STATUS_PROCESSED:
                        ref_rec.status = RefundRecord.STATUS_PROCESSING
                    ref_rec.save(update_fields=["external_reference", "status", "updated_at"])
                elif rfnd_id:
                    # New distinct refund created without prior internal voucher; do NOT touch unverified empty vouchers
                    now_ts = timezone.now()
                    date_str = now_ts.strftime("%Y%m%d")
                    rand_id = uuid.uuid4().hex[:6].upper()
                    RefundRecord.objects.create(
                        refund_reference=f"REF-{date_str}-{rand_id}",
                        external_reference=rfnd_id,
                        order=locked_order,
                        requested_amount=amount_inr,
                        approved_amount=amount_inr,
                        currency=locked_order.currency or "INR",
                        payment_method=Order.PAYMENT_METHOD_RAZORPAY,
                        status=RefundRecord.STATUS_PROCESSING,
                        audit_note=f"Refund created via Razorpay webhook ({event}): {rfnd_id}.",
                    )

            elif event == "refund.failed":
                error_desc = refund_entity.get("error_description") or "Refund failed at payment gateway."

                # Find or associate RefundRecord for this specific refund transaction
                # 1. Exact match by external_reference
                ref_rec = RefundRecord.objects.select_for_update().filter(
                    order=locked_order, external_reference=rfnd_id
                ).first()

                # 2. Verified match by internal voucher reference (receipt or notes.refund_reference) ONLY
                if not ref_rec and rfnd_id:
                    receipt_ref = refund_entity.get("receipt", "").strip() or notes.get("refund_reference", "").strip()
                    if receipt_ref:
                        ref_rec = RefundRecord.objects.select_for_update().filter(
                            order=locked_order,
                            refund_reference=receipt_ref,
                            external_reference="",
                        ).first()

                # Anti-downgrade check: if this specific refund is already processed, ignore spurious failed event
                if ref_rec and ref_rec.status == RefundRecord.STATUS_PROCESSED:
                    logger.warning(
                        f"Razorpay webhook refund.failed: #{locked_order.order_number} refund {rfnd_id} already processed. Ignoring spurious failure."
                    )
                    return HttpResponse("OK", status=200)

                # Order-level anti-downgrade: if order is fully processed, do not downgrade to failed
                if locked_order.refund_status != Order.REFUND_STATUS_PROCESSED:
                    locked_order.refund_status = Order.REFUND_STATUS_FAILED
                    locked_order.refund_failure_reason = str(error_desc)[:255]
                    locked_order.save(update_fields=["refund_status", "refund_failure_reason", "updated_at"])

                if ref_rec:
                    if ref_rec.status != RefundRecord.STATUS_PROCESSED:
                        ref_rec.status = RefundRecord.STATUS_FAILED
                        ref_rec.audit_note = f"Razorpay refund failed: {error_desc}"
                        if rfnd_id and not ref_rec.external_reference:
                            ref_rec.external_reference = rfnd_id
                        ref_rec.save(update_fields=["external_reference", "status", "audit_note", "updated_at"])
                elif rfnd_id:
                    # New distinct failed refund recorded without altering unverified vouchers
                    now_ts = timezone.now()
                    date_str = now_ts.strftime("%Y%m%d")
                    rand_id = uuid.uuid4().hex[:6].upper()
                    RefundRecord.objects.create(
                        refund_reference=f"REF-{date_str}-{rand_id}",
                        external_reference=rfnd_id,
                        order=locked_order,
                        requested_amount=amount_inr,
                        approved_amount=amount_inr,
                        currency=locked_order.currency or "INR",
                        payment_method=Order.PAYMENT_METHOD_RAZORPAY,
                        status=RefundRecord.STATUS_FAILED,
                        audit_note=f"Razorpay refund failed: {error_desc}",
                    )

                logger.warning(
                    f"Razorpay webhook: refund {rfnd_id} for #{locked_order.order_number} failed: {error_desc}"
                )

            elif event == "refund.speed_changed":
                speed_req = refund_entity.get("speed_requested", "")
                speed_proc = refund_entity.get("speed_processed", "")
                logger.info(
                    f"Razorpay refund speed changed for #{locked_order.order_number} ({rfnd_id}): "
                    f"requested={speed_req}, processed={speed_proc}"
                )

    return HttpResponse("OK", status=200)


@login_required(login_url="core:account_login")
def order_confirmation_view(request, order_number):
    """
    Displays the order confirmation screen right after order placement.
    Enforces strict customer ownership check.
    """
    order = get_object_or_404(Order, order_number=order_number, user=request.user)
    items = order.items.select_related("product", "variant").all()
    context = {
        "order": order,
        "items": items,
    }
    return render(request, "orders/order_confirmation.html", context)


@login_required(login_url="core:account_login")
def order_detail_view(request, order_number):
    """
    Detailed order breakdown and tracking view for authenticated customers.
    Guards customer ownership: customers can only view their own orders.
    """
    order = get_object_or_404(Order, order_number=order_number, user=request.user)
    items = order.items.select_related("product", "variant", "seller", "seller_order").all()
    seller_orders = order.seller_orders.select_related("seller").prefetch_related("items").all()

    from core.invoice_services import is_order_eligible_for_invoice
    can_download_invoice, invoice_reason = is_order_eligible_for_invoice(order)
    existing_invoice = order.invoices.filter(invoice_type="customer_parent").first()

    existing_returns = (
        order.return_requests.select_related("seller_order", "seller")
        .prefetch_related("items__order_item")
        .order_by("-created_at")
    )

    context = {
        "order": order,
        "items": items,
        "seller_orders": seller_orders,
        "can_download_invoice": can_download_invoice,
        "invoice_reason": invoice_reason,
        "existing_invoice": existing_invoice,
        "has_returnable_items": order.has_return_eligible_items,
        "existing_returns": existing_returns,
        "window_days": getattr(settings, "FITMEEZO_RETURN_WINDOW_DAYS", 7),
        "exchange_enabled": getattr(settings, "FITMEEZO_EXCHANGE_ENABLED", True),
    }
    return render(request, "orders/order_detail.html", context)


@login_required(login_url="core:account_login")
def customer_order_invoice_pdf_view(request, order_number):
    """
    Authorized view returning the customer parent invoice PDF (Phase 8).

    Access Control:
    - User must be authenticated.
    - Staff / superusers can access any order's invoice.
    - Regular customers can strictly only access their own orders (returns 404 on mismatch).
    - Checks issuance eligibility via is_order_eligible_for_invoice(order).
    - Generates or retrieves immutable Invoice record.
    - Renders high-quality ReportLab PDF response with nosniff and cache prevention.
    """
    if request.user.is_staff or request.user.is_superuser:
        order = get_object_or_404(Order.objects.prefetch_related("items", "invoices"), order_number=order_number)
    else:
        order = get_object_or_404(Order.objects.prefetch_related("items", "invoices"), order_number=order_number, user=request.user)

    from core.invoice_services import (
        is_order_eligible_for_invoice,
        get_or_create_customer_invoice,
        generate_customer_invoice_pdf,
    )

    eligible, reason = is_order_eligible_for_invoice(order)
    if not eligible:
        messages.warning(request, f"Invoice is not currently available: {reason}")
        return redirect("core:order_detail", order_number=order.order_number)

    invoice = get_or_create_customer_invoice(order)
    pdf_bytes = generate_customer_invoice_pdf(invoice)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="invoice_{invoice.invoice_number}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return response



@login_required(login_url="core:account_login")
@require_POST
def order_cancel_view(request, order_number):
    """
    Customer self-service order cancellation view.
    Ensures:
    - Customer is authenticated
    - Order belongs to currently logged-in customer (safe object-level 404 lookup)
    - Enforces state machine rules (Pending and Processing only) via centralized service
    - Restores inventory atomically exactly once
    - Prevents double restoration and records refund requirements for paid orders
    """
    order = get_object_or_404(Order, order_number=order_number, user=request.user)

    reason = request.POST.get("reason", "").strip()
    success, msg, _ = cancel_order(
        order=order,
        user=request.user,
        cancelled_by="customer",
        reason=reason or "Cancelled by customer via website",
    )

    if success:
        messages.success(request, f"Order #{order.order_number} has been cancelled successfully.")
    else:
        messages.error(request, msg)

    return redirect("core:order_detail", order_number=order.order_number)


@login_required(login_url="core:account_login")
def account_orders_view(request):
    """
    Authenticated My Orders history view.
    Displays paginated order history with item counts, status badges, and details links.
    """
    orders_qs = Order.objects.filter(user=request.user).prefetch_related("items").order_by("-created_at")
    paginator = Paginator(orders_qs, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)
    context = {
        "orders_page": page_obj,
        "orders": page_obj.object_list,
        "total_orders_count": orders_qs.count(),
    }
    return render(request, "account/orders.html", context)


def robots_txt(request):
    """Generates standard crawler directives for search engine robots."""
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /custom-admin/",
        "Disallow: /account/",
        "Disallow: /checkout/",
        "Disallow: /cart/",
        "Disallow: /api/auth/",
        "Disallow: /api/addresses/",
        "Disallow: /api/cart/",
        "Disallow: /api/checkout/",
        "Disallow: /api/orders/",
        "Allow: /",
        "Allow: /categories/",
        "Allow: /products/",
        "Allow: /search/",
        "Allow: /terms/",
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def sitemap_xml(request):
    """Generates dynamic XML sitemap containing active categories and products."""
    categories = Category.objects.filter(is_active=True).order_by("name")
    products = Product.objects.filter(is_active=True).order_by("-updated_at")

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f'  <url><loc>{request.build_absolute_uri("/")}</loc><changefreq>daily</changefreq><priority>1.0</priority></url>',
        f'  <url><loc>{request.build_absolute_uri(reverse("core:category_list"))}</loc><changefreq>daily</changefreq><priority>0.8</priority></url>',
        f'  <url><loc>{request.build_absolute_uri(reverse("core:terms"))}</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>',
    ]
    for cat in categories:
        loc = request.build_absolute_uri(cat.get_absolute_url())
        xml_lines.append(f'  <url><loc>{loc}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>')

    for prod in products:
        loc = request.build_absolute_uri(prod.get_absolute_url())
        lastmod = prod.updated_at.strftime("%Y-%m-%d")
        xml_lines.append(f'  <url><loc>{loc}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>0.9</priority></url>')

    xml_lines.append("</urlset>")
    return HttpResponse("\n".join(xml_lines), content_type="application/xml")


def seller_register_view(request):
    """
    Public and customer seller onboarding view.
    - If user is anonymous: Presents PublicSellerRegistrationForm to collect
      merchant account credentials (name, email, mobile, username, password)
      and full business registration details, saves with STATUS_PENDING,
      and logs the new merchant in.
    - If user is authenticated customer: Presents SellerRegistrationForm to attach
      a merchant profile with STATUS_PENDING to their existing account.
    - If user is staff or superuser: Rejects seller application with clear guidance.
    - If user already has a seller profile: Redirects to seller status view.
    """
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            messages.error(
                request,
                "Platform administrators and staff members cannot register as sellers. "
                "Please sign out and create a dedicated merchant account."
            )
            return redirect("core:home")

        existing_seller = getattr(request.user, "seller_profile", None)
        if existing_seller:
            messages.info(request, f"You already have a seller application for '{existing_seller.store_name}'.")
            return redirect("core:seller_status")

        if request.method == "POST":
            form = SellerRegistrationForm(request.POST)
            if form.is_valid():
                try:
                    seller = form.save(commit=False)
                    seller.user = request.user
                    seller.status = Seller.STATUS_PENDING
                    seller.is_active = True
                    seller.commission_rate = Decimal("0.00")
                    user_mobile = getattr(getattr(request.user, "profile", None), "mobile_number", None)
                    if user_mobile and not seller.registered_mobile:
                        if not Seller.objects.filter(registered_mobile=user_mobile).exists():
                            seller.registered_mobile = user_mobile
                    seller.save()
                    messages.success(
                        request,
                        f"Congratulations! Your seller application for '{seller.store_name}' has been submitted. "
                        "Our platform administration team will review your business details and credentials shortly."
                    )
                    return redirect("core:seller_status")
                except IntegrityError as exc:
                    logger.warning("Integrity error during seller application: %s", exc)
                    form.add_error(None, "A seller application with these store details, mobile number, or GSTIN already exists.")
                    messages.error(request, "A seller application with these business details already exists. Please review your entries.")
                except ValidationError as exc:
                    logger.warning("Validation error during seller application: %s", exc)
                    form.add_error(None, str(exc))
                    messages.error(request, "Please review the highlighted errors and correct your application.")
                except Exception as exc:
                    logger.exception("Unexpected error during seller application: %s", exc)
                    form.add_error(None, "An unexpected technical error occurred while submitting your application. Please try again.")
                    messages.error(request, "Something went wrong while processing your application. Please try again.")
            else:
                messages.error(request, "Please review the highlighted errors and correct your application.")
        else:
            initial = {}
            if request.user.email:
                initial["support_email"] = request.user.email
            form = SellerRegistrationForm(initial=initial)

        context = {
            "form": form,
            "is_public_registration": False,
            "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        }
        return render(request, "seller/register.html", context)

    # Anonymous user self-service onboarding
    if request.method == "POST":
        form = PublicSellerRegistrationForm(request.POST)
        if form.is_valid():
            try:
                user, seller = form.save()
                login(request, user, backend="core.backends.EmailOrUsernameModelBackend")
                messages.success(
                    request,
                    f"Congratulations! Your merchant account and seller application for '{seller.store_name}' "
                    "have been created. Your profile is currently pending administrator review."
                )
                return redirect("core:seller_status")
            except IntegrityError as exc:
                logger.warning("Integrity error during public seller registration: %s", exc)
                form.add_error(None, "An account or seller application with this email, mobile number, store name, or GSTIN already exists.")
                messages.error(
                    request,
                    "An account or seller application with this email, mobile number, store name, or GSTIN already exists. "
                    "Please verify your credentials or sign in."
                )
            except ValidationError as exc:
                logger.warning("Validation error during public seller registration: %s", exc)
                form.add_error(None, str(exc))
                messages.error(request, "Please review the highlighted errors and correct your application.")
            except Exception as exc:
                logger.exception("Unexpected error during public seller registration: %s", exc)
                form.add_error(None, "An unexpected technical error occurred while creating your account. Please try again.")
                messages.error(
                    request,
                    "A technical error occurred while submitting your registration. Please check your details and try again."
                )
        else:
            messages.error(request, "Please review the highlighted errors and correct your application.")
    else:
        form = PublicSellerRegistrationForm()

    context = {
        "form": form,
        "is_public_registration": True,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/register.html", context)


def seller_login_view(request):
    """
    Dedicated Seller Hub login portal.
    Authenticates marketplace vendors and directs them based on authorization:
    - Approved & Active -> Seller Dashboard (or safe next redirect)
    - Pending -> Seller Application Status
    - Rejected / Suspended / Deactivated -> Seller Status with appropriate alert
    - Customer without seller profile -> Guidance to register as a seller
    - Staff / Superuser -> Directed to Platform Admin (/admin/)
    """
    next_url = request.GET.get("next") or request.POST.get("next") or ""

    if request.user.is_authenticated:
        if is_approved_seller(request.user):
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect("core:seller_dashboard")
        seller = getattr(request.user, "seller_profile", None)
        if seller:
            return redirect("core:seller_status")
        if request.user.is_staff or request.user.is_superuser:
            messages.info(request, "Platform administrators must use the administrative console at /admin/.")
            return redirect("/admin/")
        messages.info(request, "You are logged in as a customer. Complete seller registration below to activate merchant access.")
        return redirect("core:seller_register")

    if request.method == "POST":
        form = SellerLoginForm(request.POST)
        if form.is_valid():
            username_or_email = form.cleaned_data["username_or_email"]
            password = form.cleaned_data["password"]
            remember_me = form.cleaned_data.get("remember_me")

            user = authenticate(request, username=username_or_email, password=password)
            if user is not None and user.is_active:
                if user.is_staff or user.is_superuser:
                    messages.error(
                        request,
                        "Staff and administrator accounts cannot log in through the vendor portal. "
                        "Please use the platform management console at /admin/."
                    )
                    return redirect("core:seller_login")

                login(request, user)
                if not remember_me:
                    request.session.set_expiry(0)
                else:
                    request.session.set_expiry(1209600)

                seller = getattr(user, "seller_profile", None)
                if seller:
                    if seller.status == Seller.STATUS_APPROVED and seller.is_active and seller.has_valid_gstin:
                        messages.success(request, f"Welcome back to your Seller Hub, {seller.store_name}!")
                        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                            return redirect(next_url)
                        return redirect("core:seller_dashboard")
                    elif seller.status == Seller.STATUS_PENDING:
                        messages.info(
                            request,
                            f"Your seller application for '{seller.store_name}' is currently pending administrator review."
                        )
                        return redirect("core:seller_status")
                    elif seller.status == Seller.STATUS_REJECTED:
                        messages.error(
                            request,
                            f"Your seller application for '{seller.store_name}' was not approved. Please contact platform support."
                        )
                        return redirect("core:seller_status")
                    elif seller.status == Seller.STATUS_SUSPENDED:
                        messages.error(
                            request,
                            f"Your seller account '{seller.store_name}' is suspended. Please contact platform administration."
                        )
                        return redirect("core:seller_status")
                    else:
                        messages.warning(
                            request,
                            f"Your seller account '{seller.store_name}' is currently inactive."
                        )
                        return redirect("core:seller_status")
                else:
                    messages.warning(
                        request,
                        "Your account does not have a registered seller store yet. "
                        "Please submit your vendor application below."
                    )
                    return redirect("core:seller_register")
            else:
                messages.error(request, "Invalid username/email or password. Please check your credentials.")
    else:
        form = SellerLoginForm()

    otp_request_form = SellerMobileOTPRequestForm()
    otp_verify_form = SellerMobileOTPVerifyForm()
    active_otp_mobile = request.session.get("seller_otp_mobile", "")

    return render(request, "seller/login.html", {
        "form": form,
        "otp_request_form": otp_request_form,
        "otp_verify_form": otp_verify_form,
        "active_otp_mobile": active_otp_mobile,
        "next": next_url,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    })


@require_POST
def seller_send_otp_view(request):
    """
    API/Form endpoint to request a 6-digit login OTP for an approved seller.
    Enforces format validation, 60s resend cooldown, and hourly rate limits.
    """
    mobile = request.POST.get("mobile", "").strip()
    ip_addr = get_client_ip(request)
    user_agent = get_client_user_agent(request)

    success, message, cooldown = generate_and_send_seller_otp(
        mobile=mobile,
        ip_address=ip_addr,
        user_agent=user_agent,
    )

    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")

    if is_ajax:
        status_code = 200 if success else 400
        return JsonResponse({
            "success": success,
            "message": message,
            "cooldown": cooldown,
            "mobile": mobile,
        }, status=status_code)

    if success:
        messages.success(request, message)
        request.session["seller_otp_mobile"] = mobile
    else:
        messages.error(request, message)

    return redirect("core:seller_login")


@require_POST
def seller_verify_otp_view(request):
    """
    API/Form endpoint to verify the 6-digit OTP and establish an authenticated Django session.
    Redirects to the Seller Dashboard for approved and active sellers.
    """
    mobile = request.POST.get("mobile", "").strip() or request.session.get("seller_otp_mobile", "")
    otp = request.POST.get("otp", "").strip()
    remember_me = request.POST.get("remember_me") in ("true", "1", "on", True)
    next_url = request.POST.get("next") or request.GET.get("next") or ""

    success, message, seller = verify_seller_otp_code(mobile=mobile, raw_otp=otp)

    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")

    if not success:
        if is_ajax:
            return JsonResponse({"success": False, "message": message}, status=400)
        messages.error(request, message)
        return redirect("core:seller_login")

    # Establish authenticated Django session
    user = seller.user
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    if not remember_me:
        request.session.set_expiry(0)
    else:
        request.session.set_expiry(1209600)

    if "seller_otp_mobile" in request.session:
        del request.session["seller_otp_mobile"]

    messages.success(request, f"Welcome back to your Seller Hub, {seller.store_name}!")

    redirect_target = reverse("core:seller_dashboard")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        redirect_target = next_url

    if is_ajax:
        return JsonResponse({
            "success": True,
            "message": message,
            "redirect_url": redirect_target,
        })

    return redirect(redirect_target)


def seller_logout_view(request):
    """
    Seller Hub logout view.
    Securely signs the merchant out and redirects to the seller login page.
    Supports both POST and GET for clean session termination.
    """
    if request.user.is_authenticated:
        logout(request)
        messages.success(request, "You have been securely signed out of the Seller Hub.")
    return redirect("core:seller_login")


@login_required(login_url="core:account_login")
def seller_status_view(request):
    """
    Displays current application/membership status for the authenticated seller.
    Shows clear guidance for pending, approved, rejected, and suspended states.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller application. Register below to begin selling.")
        return redirect("core:seller_register")

    context = {
        "seller": seller,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/status.html", context)


@seller_required
def seller_profile_view(request):
    """
    Allows a registered seller to review and update operational contact/pickup info.
    Tax fields and status are protected from direct seller mutation.
    Requires approved seller status and valid GSTIN.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have a seller profile.")
        return redirect("core:seller_register")

    if request.method == "POST":
        form = SellerProfileUpdateForm(request.POST, instance=seller)
        if form.is_valid():
            form.save()
            messages.success(request, "Your seller profile has been updated successfully.")
            return redirect("core:seller_profile")
        else:
            messages.error(request, "Please correct the highlighted errors.")
    else:
        form = SellerProfileUpdateForm(instance=seller)

    context = {
        "seller": seller,
        "form": form,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/profile.html", context)


@login_required(login_url="core:account_login")
def seller_closure_request_view(request):
    """
    Dedicated seller-facing account closure request view (Phase 3).
    - Requires authenticated seller.
    - Strictly checks that user owns the seller profile.
    - Only merchants in STATUS_APPROVED can initiate an account closure request.
    - Idempotent and concurrency-safe: duplicate/stale requests are handled safely.
    - Halts new orders immediately by transitioning status to STATUS_CLOSURE_REQUESTED.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have a registered seller account.")
        return redirect("core:seller_register")

    # If seller is already in a closure lifecycle state, show status and prevent double submission
    if seller.status == Seller.STATUS_CLOSURE_REQUESTED:
        if request.method == "POST":
            messages.info(request, "A closure request for your store is already pending administrator review.")
            return redirect("core:seller_close_account")
        context = {
            "seller": seller,
            "status_code": "closure_requested",
            "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        }
        return render(request, "seller/closure_request.html", context)

    if seller.status == Seller.STATUS_CLOSING:
        if request.method == "POST":
            messages.info(request, "Your store is already approved for closure and is currently in run-off.")
            return redirect("core:seller_close_account")
        context = {
            "seller": seller,
            "status_code": "closing",
            "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        }
        return render(request, "seller/closure_request.html", context)

    if seller.status == Seller.STATUS_CLOSED:
        if request.method == "POST":
            return HttpResponseForbidden("Your account is already permanently closed. No further closure actions are available.")
        context = {
            "seller": seller,
            "status_code": "closed",
            "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        }
        return render(request, "seller/closure_request.html", context)

    if seller.status != Seller.STATUS_APPROVED:
        messages.error(
            request,
            f"Cannot request closure: merchant account status is '{seller.get_status_display()}'. Only active, approved sellers can request closure."
        )
        return redirect("core:seller_status")

    if request.method == "POST":
        form = SellerClosureRequestForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                locked_seller = Seller.objects.select_for_update().get(pk=seller.pk)
                if locked_seller.status != Seller.STATUS_APPROVED:
                    messages.warning(
                        request,
                        f"Cannot submit closure request: store status has changed to '{locked_seller.get_status_display()}'."
                    )
                    return redirect("core:seller_close_account")

                reason = form.cleaned_data["reason"]
                now = timezone.now()
                locked_seller.status = Seller.STATUS_CLOSURE_REQUESTED
                locked_seller.closure_requested_at = now
                locked_seller.closure_request_reason = reason
                locked_seller.save(
                    update_fields=[
                        "status",
                        "closure_requested_at",
                        "closure_request_reason",
                        "updated_at",
                    ]
                )

                log_admin_action(
                    request.user,
                    locked_seller,
                    CHANGE,
                    f"Seller requested account closure. Reason: {reason[:120]}",
                )

            messages.success(
                request,
                "Your account closure request has been submitted successfully and is pending administrator review. "
                "New customer orders have been halted immediately. You must fulfill any existing accepted orders."
            )
            return redirect("core:seller_close_account")
        else:
            messages.error(request, "Please provide a valid explanation reason (10 to 1000 characters) and confirm the terms.")
    else:
        form = SellerClosureRequestForm()

    context = {
        "seller": seller,
        "form": form,
        "status_code": "eligible",
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/closure_request.html", context)


@seller_required
def seller_dashboard_view(request):
    """
    Dedicated seller operations dashboard for approved marketplace vendors.
    Displays scoped performance metrics, package fulfillment status breakdown,
    inventory alerts, and date-filtered snapshot sales summaries.
    Guarded by @seller_required (requires approved status, active account, and valid GSTIN).
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    start_date_str = request.GET.get("start_date", "").strip()
    end_date_str = request.GET.get("end_date", "").strip()

    start_dt, end_dt, date_error = parse_date_range(start_date_str, end_date_str)
    if date_error:
        messages.warning(request, date_error)
        start_dt = None
        end_dt = None
        start_date_str = ""
        end_date_str = ""

    metrics = get_seller_dashboard_metrics(seller=seller, start_dt=start_dt, end_dt=end_dt)

    context = {
        "seller": seller,
        "metrics": metrics,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "has_date_filter": bool(start_dt or end_dt),
        "date_filter_error": date_error,
        "low_stock_threshold": metrics.get("low_stock_threshold", DEFAULT_LOW_STOCK_THRESHOLD),
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/dashboard.html", context)


# ==============================================================================
# SELLER PRODUCT & INVENTORY MANAGEMENT VIEWS (PHASE 3 PROMPT 2)
# ==============================================================================

@seller_required
def seller_product_list_view(request):
    """
    Lists catalogue products owned strictly by the authenticated seller.
    Includes search, approval/active status filtering, stock metrics, and pagination.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    queryset = (
        Product.objects.filter(seller=seller)
        .select_related("category")
        .prefetch_related("variants", "images")
        .order_by("-created_at")
    )

    # Search by product name or SKU
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(Q(name__icontains=query) | Q(sku__icontains=query))

    # Filter by moderation or active status
    status_filter = request.GET.get("status", "").strip().lower()
    if status_filter == "pending":
        queryset = queryset.filter(approval_status=Product.APPROVAL_PENDING)
    elif status_filter == "approved":
        queryset = queryset.filter(approval_status=Product.APPROVAL_APPROVED)
    elif status_filter == "rejected":
        queryset = queryset.filter(approval_status=Product.APPROVAL_REJECTED)
    elif status_filter == "active":
        queryset = queryset.filter(is_active=True)
    elif status_filter == "inactive":
        queryset = queryset.filter(is_active=False)

    # Overview counters for filter pills
    base_qs = Product.objects.filter(seller=seller)
    total_count = base_qs.count()
    approved_count = base_qs.filter(approval_status=Product.APPROVAL_APPROVED).count()
    pending_count = base_qs.filter(approval_status=Product.APPROVAL_PENDING).count()
    rejected_count = base_qs.filter(approval_status=Product.APPROVAL_REJECTED).count()

    paginator = Paginator(queryset, 15)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "seller": seller,
        "products": page_obj,
        "page_obj": page_obj,
        "query": query,
        "status_filter": status_filter,
        "total_count": total_count,
        "approved_count": approved_count,
        "pending_count": pending_count,
        "rejected_count": rejected_count,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/products/list.html", context)


@seller_required
def seller_product_add_view(request):
    """
    Creates a new product owned exclusively by the authenticated seller.
    Enforces Option B Platform Moderation: newly created seller items
    always start with is_approved=False and approval_status='pending'.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    if seller.status in (Seller.STATUS_CLOSURE_REQUESTED, Seller.STATUS_CLOSING, Seller.STATUS_CLOSED):
        messages.error(request, "Adding new products is disabled because your merchant account is undergoing account closure.")
        return redirect("core:seller_product_list")

    if request.method == "POST":
        form = SellerProductForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                with transaction.atomic():
                    product = form.save(commit=False)
                    # Strict ownership enforcement
                    product.seller = seller
                    # Option B Moderation: requires admin approval
                    product.is_approved = False
                    product.approval_status = Product.APPROVAL_PENDING
                    product.rejection_reason = ""
                    product.save()
                    form.save_m2m()

                messages.success(
                    request,
                    f"Product '{product.name}' was created successfully! It is now pending Platform Admin approval before appearing on the public storefront.",
                )
                return redirect("core:seller_product_edit", pk=product.pk)
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for field, err_list in exc.message_dict.items():
                        for err in err_list:
                            form.add_error(field if field in form.fields else None, err)
                elif hasattr(exc, "messages"):
                    for err in exc.messages:
                        form.add_error(None, err)
                else:
                    form.add_error(None, str(exc))
                messages.error(request, "Please resolve the validation errors highlighted below.")
        else:
            messages.error(request, "Please resolve the validation errors highlighted below.")
    else:
        form = SellerProductForm()

    context = {
        "seller": seller,
        "form": form,
        "is_create": True,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/products/form.html", context)


@seller_required
def seller_product_edit_view(request, pk):
    """
    Edits a seller-owned product, manages its variants, and manages gallery images.
    Strictly scoped via get_object_or_404(Product, pk=pk, seller=seller).
    If core material fields of an already-approved product are modified,
    it is safely returned to pending moderation status.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    product = get_object_or_404(Product, pk=pk, seller=seller)

    if request.method == "POST":
        form = SellerProductForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            material_fields = {
                "name",
                "category",
                "price",
                "compare_at_price",
                "description",
                "short_description",
                "sku",
                "image",
            }
            was_approved = product.approval_status == Product.APPROVAL_APPROVED
            materially_changed = was_approved and any(f in form.changed_data for f in material_fields)

            try:
                with transaction.atomic():
                    updated_product = form.save(commit=False)
                    # Enforce ownership retention
                    updated_product.seller = seller
                    if materially_changed:
                        updated_product.is_approved = False
                        updated_product.approval_status = Product.APPROVAL_PENDING
                        updated_product.rejection_reason = ""
                    updated_product.save()
                    form.save_m2m()

                if materially_changed:
                    messages.info(
                        request,
                        f"Product details updated. Because core listing details were modified, '{product.name}' has been resubmitted for admin review.",
                    )
                else:
                    messages.success(request, f"Product '{product.name}' was updated successfully.")
                return redirect("core:seller_product_edit", pk=product.pk)
            except ValidationError as exc:
                if hasattr(exc, "message_dict"):
                    for field, err_list in exc.message_dict.items():
                        for err in err_list:
                            form.add_error(field if field in form.fields else None, err)
                elif hasattr(exc, "messages"):
                    for err in exc.messages:
                        form.add_error(None, err)
                else:
                    form.add_error(None, str(exc))
                messages.error(request, "Please resolve the validation errors highlighted below.")
        else:
            messages.error(request, "Please resolve the validation errors highlighted below.")
    else:
        form = SellerProductForm(instance=product)

    variants = product.variants.all().order_by("option_type", "option_value", "id")
    images = product.images.all().order_by("display_order", "id")
    variant_form = SellerProductVariantForm(product=product)
    image_form = SellerProductImageForm()

    context = {
        "seller": seller,
        "product": product,
        "form": form,
        "variants": variants,
        "images": images,
        "variant_form": variant_form,
        "image_form": image_form,
        "is_create": False,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/products/form.html", context)


@seller_required
def seller_product_inventory_view(request, pk):
    """
    Fast inventory stock adjustment view for seller-owned products.
    Respects existing inventory architecture:
    - If product has variants, stock is updated atomically on each variant.
    - If product has no variants, stock is updated on Product.stock_quantity.
    - Routine stock adjustments DO NOT reset approval status.
    - GET requests NEVER alter inventory.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    product = get_object_or_404(Product, pk=pk, seller=seller)
    has_variants = product.variants.exists()
    variants = product.variants.all().order_by("option_type", "option_value", "id")

    if request.method == "POST":
        if has_variants:
            # Atomic update for all submitted variant quantities
            updates = {}
            has_error = False

            for variant in variants:
                field_key = f"stock_variant_{variant.pk}"
                val_raw = request.POST.get(field_key, "").strip()
                if val_raw == "":
                    continue
                try:
                    qty = int(val_raw)
                    if qty < 0:
                        has_error = True
                        messages.error(
                            request,
                            f"Stock for variant '{variant.option_value}' cannot be negative.",
                        )
                    else:
                        updates[variant.pk] = qty
                except (ValueError, TypeError):
                    has_error = True
                    messages.error(
                        request,
                        f"Invalid numerical stock for variant '{variant.option_value}'.",
                    )

            if not has_error:
                with transaction.atomic():
                    for v_pk, qty in updates.items():
                        ProductVariant.objects.filter(pk=v_pk, product=product).update(stock_quantity=qty)
                messages.success(request, f"Variant inventory successfully updated for '{product.name}'.")
                return redirect("core:seller_product_inventory", pk=product.pk)

        else:
            # Standalone product stock update
            standalone_form = SellerStandaloneInventoryForm(request.POST)
            if standalone_form.is_valid():
                qty = standalone_form.cleaned_data["stock_quantity"]
                with transaction.atomic():
                    Product.objects.filter(pk=product.pk, seller=seller).update(stock_quantity=qty)
                messages.success(
                    request,
                    f"Inventory updated for '{product.name}' ({qty} units currently available).",
                )
                return redirect("core:seller_product_inventory", pk=product.pk)
            else:
                messages.error(request, "Invalid stock quantity. Stock cannot be negative.")

    # Form initialization for standalone products
    standalone_form = SellerStandaloneInventoryForm(initial={"stock_quantity": product.stock_quantity})

    context = {
        "seller": seller,
        "product": product,
        "has_variants": has_variants,
        "variants": variants,
        "standalone_form": standalone_form,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/products/inventory.html", context)


@seller_required
@require_POST
def seller_product_variant_add_view(request, pk):
    """
    Adds a new variant to a seller-owned product.
    Guarded against cross-seller alteration.
    """
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, pk=pk, seller=seller)

    form = SellerProductVariantForm(request.POST, product=product)
    if form.is_valid():
        with transaction.atomic():
            variant = form.save(commit=False)
            variant.product = product
            variant.save()
        messages.success(request, f"Variant '{variant.option_type}: {variant.option_value}' added.")
    else:
        err_msg = "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
        messages.error(request, f"Could not add variant: {err_msg}")

    return redirect("core:seller_product_edit", pk=product.pk)


@seller_required
def seller_product_variant_edit_view(request, pk, variant_pk):
    """
    Edits an existing variant belonging strictly to the seller's product.
    """
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, pk=pk, seller=seller)
    variant = get_object_or_404(ProductVariant, pk=variant_pk, product=product)

    if request.method == "POST":
        form = SellerProductVariantForm(request.POST, instance=variant, product=product)
        if form.is_valid():
            form.save()
            messages.success(request, f"Variant '{variant.option_type}: {variant.option_value}' updated.")
            return redirect("core:seller_product_edit", pk=product.pk)
        else:
            messages.error(request, "Please resolve the errors below.")
    else:
        form = SellerProductVariantForm(instance=variant, product=product)

    context = {
        "seller": seller,
        "product": product,
        "variant": variant,
        "form": form,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/products/variant_form.html", context)


@seller_required
@require_POST
def seller_product_variant_toggle_view(request, pk, variant_pk):
    """
    Toggles the active availability status of a variant.
    """
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, pk=pk, seller=seller)
    variant = get_object_or_404(ProductVariant, pk=variant_pk, product=product)

    variant.is_active = not variant.is_active
    variant.save(update_fields=["is_active"])

    status_str = "activated" if variant.is_active else "deactivated"
    messages.success(request, f"Variant '{variant.option_value}' {status_str}.")
    return redirect("core:seller_product_edit", pk=product.pk)


@seller_required
@require_POST
def seller_product_variant_delete_view(request, pk, variant_pk):
    """
    Deletes a variant strictly belonging to the seller's product.
    """
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, pk=pk, seller=seller)
    variant = get_object_or_404(ProductVariant, pk=variant_pk, product=product)

    variant_desc = f"{variant.option_type}: {variant.option_value}"
    variant.delete()
    messages.success(request, f"Variant '{variant_desc}' deleted.")
    return redirect("core:seller_product_edit", pk=product.pk)


@seller_required
@require_POST
def seller_product_image_add_view(request, pk):
    """
    Uploads an additional showcase image for a seller-owned product.
    """
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, pk=pk, seller=seller)

    form = SellerProductImageForm(request.POST, request.FILES)
    if form.is_valid():
        with transaction.atomic():
            img = form.save(commit=False)
            img.product = product
            img.save()
        messages.success(request, "Image added to product gallery.")
    else:
        err_msg = "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
        messages.error(request, f"Could not upload image: {err_msg}")

    return redirect("core:seller_product_edit", pk=product.pk)


@seller_required
@require_POST
def seller_product_image_delete_view(request, pk, image_pk):
    """
    Removes a gallery showcase photo strictly belonging to the seller's product.
    """
    seller = getattr(request.user, "seller_profile", None)
    product = get_object_or_404(Product, pk=pk, seller=seller)
    img = get_object_or_404(ProductImage, pk=image_pk, product=product)

    img.delete()
    messages.success(request, "Gallery image removed.")
    return redirect("core:seller_product_edit", pk=product.pk)


# ==============================================================================
# SELLER ORDER FULFILLMENT MANAGEMENT VIEWS (PHASE 3 PROMPT 3)
# ==============================================================================

@seller_required
def seller_order_list_view(request):
    """
    Lists assigned fulfillment packages owned strictly by the authenticated seller.
    Guards customer privacy: excludes commission rates/amounts, net seller payable,
    payment gateway internals, and items belonging to other sellers in the parent order.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    queryset = (
        SellerOrder.objects.filter(seller=seller)
        .select_related("order")
        .prefetch_related("items")
        .order_by("-created_at")
    )

    # Search filter by sub-order number or SKU
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(
            Q(sub_order_number__icontains=query) | Q(items__sku__icontains=query)
        ).distinct()

    # Fulfillment status filter
    status_filter = request.GET.get("status", "").strip().lower()
    valid_statuses = dict(SellerOrder.STATUS_CHOICES)
    if status_filter in valid_statuses:
        queryset = queryset.filter(status=status_filter)

    # Status counts for filter pills (strictly scoped to this seller)
    base_qs = SellerOrder.objects.filter(seller=seller)
    total_count = base_qs.count()
    pending_count = base_qs.filter(status=SellerOrder.STATUS_PENDING).count()
    processing_count = base_qs.filter(status=SellerOrder.STATUS_PROCESSING).count()
    ready_to_ship_count = base_qs.filter(status=SellerOrder.STATUS_READY_TO_SHIP).count()
    shipped_count = base_qs.filter(status=SellerOrder.STATUS_SHIPPED).count()
    delivered_count = base_qs.filter(status=SellerOrder.STATUS_DELIVERED).count()
    cancelled_count = base_qs.filter(status=SellerOrder.STATUS_CANCELLED).count()

    paginator = Paginator(queryset, 15)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "seller": seller,
        "packages": page_obj,
        "page_obj": page_obj,
        "query": query,
        "status_filter": status_filter,
        "total_count": total_count,
        "pending_count": pending_count,
        "processing_count": processing_count,
        "ready_to_ship_count": ready_to_ship_count,
        "shipped_count": shipped_count,
        "delivered_count": delivered_count,
        "cancelled_count": cancelled_count,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/orders/list.html", context)


@seller_required
def seller_order_detail_view(request, pk):
    """
    Displays fulfillment details for a seller-owned package.
    Strictly scoped via get_object_or_404(SellerOrder, pk=pk, seller=seller).
    Displays only seller-owned items and delivery info required for dispatch.
    Omits platform financials, commissions, net payable, and gateway identifiers.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    package = get_object_or_404(
        SellerOrder.objects.select_related("order").prefetch_related("items__product", "items__variant"),
        pk=pk,
        seller=seller,
    )

    items = package.items.all().order_by("id")
    order = package.order

    # Determine allowed next fulfillment transition
    allowed_transitions = SellerFulfillmentUpdateForm.ALLOWED_TRANSITIONS.get(package.status, [])
    next_status = allowed_transitions[0] if allowed_transitions else None

    form = SellerFulfillmentUpdateForm(
        package=package,
        initial={
            "status": next_status or "",
            "courier_name": package.courier_name,
            "tracking_number": package.tracking_number,
        },
    )

    measurements_form = PackagePackedMeasurementsForm(
        initial={
            "actual_weight_grams": package.actual_weight_grams,
            "actual_length_cm": package.actual_length_cm,
            "actual_breadth_cm": package.actual_breadth_cm,
            "actual_height_cm": package.actual_height_cm,
        }
    )

    is_booking_eligible, booking_ineligible_reason, _ = check_booking_eligibility(package)
    shipment_booking = getattr(package, "shipment_booking", None)

    context = {
        "seller": seller,
        "package": package,
        "order": order,
        "items": items,
        "next_status": next_status,
        "form": form,
        "measurements_form": measurements_form,
        "effective_measurements": package.get_effective_measurements(),
        "dead_weight_kg": package.get_dead_weight_kg(),
        "volumetric_weight_kg": package.get_volumetric_weight_kg(),
        "chargeable_weight_kg": package.get_chargeable_weight_kg(),
        "pickup_details": package.get_pickup_location_details(),
        "rate_estimate": cache.get(f"shiprocket_rate_estimate_pkg_{package.pk}"),
        "is_booking_eligible": is_booking_eligible,
        "booking_ineligible_reason": booking_ineligible_reason,
        "shipment_booking": shipment_booking,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/orders/detail.html", context)


@seller_required
@require_POST
def seller_order_update_status_view(request, pk):
    """
    Safely advances fulfillment status for a seller-owned package.
    Transitions allowed:
    - Pending -> Processing
    - Processing -> Ready to Ship
    - Ready to Ship -> Shipped (requires courier_name and tracking_number)
    Rejects jumps, backward moves, marking delivered, or reactivating cancelled packages.
    Leaves parent Order status, payment status, inventory, and commission snapshots untouched.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    package = get_object_or_404(SellerOrder, pk=pk, seller=seller)

    form = SellerFulfillmentUpdateForm(request.POST, package=package)
    if form.is_valid():
        new_status = form.cleaned_data["status"]
        with transaction.atomic():
            locked = (
                SellerOrder.objects.select_for_update()
                .filter(pk=package.pk, seller=seller)
                .first()
            )
            if not locked:
                messages.error(request, "Package was not found.")
                return redirect("core:seller_order_list")

            # Re-check transition against locked record
            allowed = SellerFulfillmentUpdateForm.ALLOWED_TRANSITIONS.get(locked.status, [])
            if new_status not in allowed:
                messages.error(
                    request,
                    f"Invalid fulfillment transition from '{locked.status}' to '{new_status}'.",
                )
                return redirect("core:seller_order_detail", pk=package.pk)

            if new_status == SellerOrder.STATUS_SHIPPED:
                courier = form.cleaned_data["courier_name"]
                tracking = form.cleaned_data["tracking_number"]
                locked.mark_shipped(courier_name=courier, tracking_number=tracking, commit=True)
                messages.success(
                    request,
                    f"Package #{locked.sub_order_number} marked as Shipped via {courier} (Tracking: {tracking}).",
                )
            else:
                locked.status = new_status
                locked.save(update_fields=["status", "updated_at"])
                messages.success(
                    request,
                    f"Package #{locked.sub_order_number} status updated to '{locked.get_status_display()}'.",
                )

        return redirect("core:seller_order_detail", pk=package.pk)
    else:
        err_msg = "; ".join(f"{', '.join(v)}" for v in form.errors.values())
        messages.error(request, f"Unable to update fulfillment status: {err_msg}")
        return redirect("core:seller_order_detail", pk=package.pk)


@seller_required
@require_POST
def seller_order_record_measurements_view(request, pk):
    """
    Allows authorized merchant staff to record verified packed parcel measurements
    (Task 2 & 6) before courier pickup.
    Preserves historical OrderItem snapshots untouched.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    package = get_object_or_404(SellerOrder, pk=pk, seller=seller)
    if package.status in [SellerOrder.STATUS_SHIPPED, SellerOrder.STATUS_DELIVERED, SellerOrder.STATUS_CANCELLED]:
        messages.error(request, f"Cannot edit packed measurements for a package that is {package.get_status_display()}.")
        return redirect("core:seller_order_detail", pk=package.pk)

    form = PackagePackedMeasurementsForm(request.POST)
    if form.is_valid():
        package.record_verified_measurements(
            weight_grams=form.cleaned_data["actual_weight_grams"],
            length_cm=form.cleaned_data["actual_length_cm"],
            breadth_cm=form.cleaned_data["actual_breadth_cm"],
            height_cm=form.cleaned_data["actual_height_cm"],
            user=request.user,
            commit=True,
        )
        messages.success(request, f"Packed parcel measurements verified and recorded for package #{package.sub_order_number}.")
    else:
        err_msg = "; ".join(f"{', '.join(v)}" for v in form.errors.values())
        messages.error(request, f"Invalid measurements: {err_msg}")

    return redirect("core:seller_order_detail", pk=package.pk)


@seller_required
@require_POST
def seller_order_estimate_rates_view(request, pk):
    """
    Authorized seller endpoint to query courier serviceability and non-binding
    rate estimates from Shiprocket for an eligible SellerOrder parcel.

    Enforces:
    - Seller package ownership via get_object_or_404(SellerOrder, pk=pk, seller=seller).
    - Rate limits / abuse prevention using cache locks or rapid request suppression.
    - Zero modification to financial columns (actual_shipping_cost, commission, payable).
    - Clear distinction between live quote estimates and confirmed courier expenses.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": "Unauthorized seller profile."}, status=403)
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    package = get_object_or_404(SellerOrder, pk=pk, seller=seller)

    # Basic throttling: avoid hammering provider for the same package within 5 seconds
    throttle_key = f"rate_estimate_lock_pkg_{package.pk}"
    if cache.get(throttle_key):
        err_msg = "Rate estimate was requested very recently. Please wait a few seconds before refreshing."
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": err_msg}, status=429)
        messages.warning(request, err_msg)
        return redirect("core:seller_order_detail", pk=package.pk)

    cache.set(throttle_key, True, 5)

    try:
        result = estimate_package_shipping_rate(package)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(result.to_dict())

        if result.is_serviceable and result.cheapest_courier:
            messages.success(
                request,
                f"Courier serviceability verified: {len(result.available_couriers)} couriers available. "
                f"Cheapest quote: {result.cheapest_courier.courier_name} at ₹{result.cheapest_courier.rate}. "
                f"(Non-binding estimate for logistics planning; actual courier expense remains unchanged)."
            )
        else:
            messages.warning(
                request,
                f"Serviceability lookup returned no available couriers: {result.message}"
            )
    except ShiprocketError as err:
        logger.warning(
            "Shiprocket rate estimate failure for package #%s (seller %s): %s",
            package.sub_order_number,
            seller.pk,
            err.user_message,
        )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": err.user_message}, status=400)
        messages.error(request, f"Unable to estimate shipping rates: {err.user_message}")
    except Exception as exc:
        logger.exception("Unexpected error during rate estimation for package #%s", package.sub_order_number)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": "An internal error occurred during rate estimation."}, status=500)
        messages.error(request, "An unexpected error occurred while checking courier rates. Please try again later.")

    return redirect("core:seller_order_detail", pk=package.pk)


@seller_required
@require_POST
def seller_order_create_mock_shipment_view(request, pk):
    """
    Authorized seller endpoint to create a simulated (mock) shipment booking
    for an eligible SellerOrder package (Phase 5 Prompt 5B).

    Enforces:
    - @seller_required (active, approved seller, valid GSTIN)
    - Ownership: get_object_or_404(SellerOrder, pk=pk, seller=seller)
    - Third-party seller isolation: rejects platform packages.
    - POST only with CSRF protection.
    - Idempotency and row-level locking via create_mock_shipment_booking.
    - Safe error reporting on eligibility failure.
    - Zero external network requests.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    package = get_object_or_404(SellerOrder, pk=pk, seller=seller)

    if package.is_platform_fulfilled:
        msg = "Platform-fulfilled packages cannot be booked through the third-party seller dashboard."
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": msg}, status=403)
        messages.error(request, msg)
        return redirect("core:seller_order_detail", pk=package.pk)

    idempotency_key = (request.POST.get("idempotency_key") or "").strip() or None

    try:
        booking = create_mock_shipment_booking(
            package_id=package.pk,
            idempotency_key=idempotency_key,
            user=request.user,
        )
        success_msg = (
            f"Simulated shipment created successfully! "
            f"Reference: {booking.shipment_id} (AWB: {booking.awb_code}, Courier: {booking.courier_name}). "
            "Please note: This is a simulated booking for testing; no carrier has been contacted."
        )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "success": True,
                "is_mock": True,
                "shipment_id": booking.shipment_id,
                "awb_code": booking.awb_code,
                "courier_name": booking.courier_name,
                "status": booking.status,
                "message": success_msg,
            })
        messages.success(request, success_msg)
    except ShiprocketError as err:
        logger.warning(
            "Mock shipment booking failed for package #%s (seller %s): %s",
            package.sub_order_number,
            seller.pk,
            err.user_message,
        )
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": err.user_message}, status=400)
        messages.error(request, f"Unable to create mock shipment: {err.user_message}")
    except Exception as exc:
        logger.exception("Unexpected error during mock booking for package #%s", package.sub_order_number)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"error": "An internal error occurred during mock shipment creation."}, status=500)
        messages.error(request, "An unexpected error occurred while creating mock shipment. Please try again.")

    return redirect("core:seller_order_detail", pk=package.pk)


@login_required
def seller_order_packing_summary_view(request, pk):
    """
    Authorized view returning a secure internal warehouse packing summary PDF (Phase 5C).

    Access Control:
    - User must be authenticated.
    - Platform staff/superusers can access any package.
    - Marketplace sellers can only access packages belonging to their approved active profile.
    - Platform-fulfilled packages cannot be downloaded by third-party sellers.
    - Unapproved, inactive, or non-seller users are rejected with PermissionDenied (HTTP 403).
    - Customer address and phone are masked in the document to protect PII.
    - Zero external network requests; zero fake carrier AWBs.
    """
    if request.user.is_staff or request.user.is_superuser:
        package = get_object_or_404(
            SellerOrder.objects.select_related("order", "seller").prefetch_related("items"),
            pk=pk,
        )
    else:
        seller = getattr(request.user, "seller_profile", None)
        allowed_statuses = (Seller.STATUS_APPROVED, Seller.STATUS_CLOSING, Seller.STATUS_CLOSED)
        if (
            not seller
            or not seller.has_valid_gstin
            or seller.status not in allowed_statuses
            or (seller.status == Seller.STATUS_APPROVED and not seller.is_active)
        ):
            raise PermissionDenied("Access restricted to authorized marketplace sellers.")

        package = get_object_or_404(
            SellerOrder.objects.select_related("order", "seller").prefetch_related("items"),
            pk=pk,
            seller=seller,
        )
        if package.is_platform_fulfilled:
            raise PermissionDenied("Platform-fulfilled packages cannot be accessed by third-party sellers.")

    # Generate professional vector ReportLab A4 packing slip PDF
    from core.packing_slip import generate_internal_packing_summary_pdf
    pdf_bytes = generate_internal_packing_summary_pdf(package)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="packing_summary_{package.sub_order_number}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return response


@login_required
def seller_order_shipping_label_view(request, pk):
    """
    Authorized view returning a print-ready courier-style shipping label PDF.
    
    Access Control:
    - User must be authenticated.
    - Platform staff/superusers can access any package.
    - Marketplace sellers can only access packages belonging to their approved active profile.
    - Platform-fulfilled packages cannot be downloaded by third-party sellers.
    
    Courier Integration:
    - If real courier integration has an official courier-generated label URL and
      the caller requests provider source (?source=courier), safely redirects or streams
      without modifying barcode or tracking data.
      
    Layout & Formatting:
    - Defaults to standard 4x6 inch thermal sticker label (or ?format=a4 for A4 sheet).
    - If unbooked or simulated, clearly labeled as internal dispatch without fake AWBs.
    """
    if request.user.is_staff or request.user.is_superuser:
        package = get_object_or_404(
            SellerOrder.objects.select_related("order", "seller").prefetch_related("items"),
            pk=pk,
        )
    else:
        seller = getattr(request.user, "seller_profile", None)
        allowed_statuses = (Seller.STATUS_APPROVED, Seller.STATUS_CLOSING, Seller.STATUS_CLOSED)
        if (
            not seller
            or not seller.has_valid_gstin
            or seller.status not in allowed_statuses
            or (seller.status == Seller.STATUS_APPROVED and not seller.is_active)
        ):
            raise PermissionDenied("Access restricted to authorized marketplace sellers.")

        package = get_object_or_404(
            SellerOrder.objects.select_related("order", "seller").prefetch_related("items"),
            pk=pk,
            seller=seller,
        )
        if package.is_platform_fulfilled:
            raise PermissionDenied("Platform-fulfilled packages cannot be accessed by third-party sellers.")

    # 1. Official carrier label retrieval if supported and available (Requirement 9)
    booking = getattr(package, "shipment_booking", None)
    courier_label_url = None
    if booking and not getattr(booking, "is_mock", True) and booking.metadata:
        courier_label_url = booking.metadata.get("label_download_url") or booking.metadata.get("label_url")

    if request.GET.get("source") == "courier" and courier_label_url:
        return redirect(courier_label_url)

    # 2. Determine format (4x6 thermal vs A4 sheet)
    fmt = request.GET.get("format") or request.GET.get("size") or "4x6"

    from core.packing_slip import generate_courier_shipping_label_pdf
    pdf_bytes = generate_courier_shipping_label_pdf(package, page_size=fmt)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="shipping_label_{package.sub_order_number}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return response


@login_required
def seller_order_invoice_pdf_view(request, pk):
    """
    Authorized view returning a seller package commercial invoice PDF (Phase 8).

    Access Control:
    - User must be authenticated.
    - Staff / superusers can access any package invoice.
    - Fulfilling sellers can only access package invoices belonging to their active profile.
    - Cross-seller access is rejected (404/403).
    - Platform-fulfilled packages cannot be downloaded by third-party sellers.
    - Strictly prevents leaking commission rates or internal net payable calculations.
    """
    if request.user.is_staff or request.user.is_superuser:
        package = get_object_or_404(
            SellerOrder.objects.select_related("order", "seller").prefetch_related("items", "invoices"),
            pk=pk,
        )
    else:
        seller = getattr(request.user, "seller_profile", None)
        allowed_statuses = (Seller.STATUS_APPROVED, Seller.STATUS_CLOSING, Seller.STATUS_CLOSED)
        if (
            not seller
            or not seller.has_valid_gstin
            or seller.status not in allowed_statuses
            or (seller.status == Seller.STATUS_APPROVED and not seller.is_active)
        ):
            raise PermissionDenied("Access restricted to authorized marketplace sellers.")

        package = get_object_or_404(
            SellerOrder.objects.select_related("order", "seller").prefetch_related("items", "invoices"),
            pk=pk,
            seller=seller,
        )
        if package.is_platform_fulfilled:
            raise PermissionDenied("Platform-fulfilled packages cannot be accessed by third-party sellers.")

    from core.invoice_services import (
        get_or_create_seller_package_invoice,
        generate_seller_package_invoice_pdf,
    )

    invoice = get_or_create_seller_package_invoice(package)
    pdf_bytes = generate_seller_package_invoice_pdf(invoice)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="seller_invoice_{invoice.invoice_number}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return response





# ==============================================================================
# SELLER REPORTS & SECURE CSV EXPORT VIEWS (PHASE 3 PROMPT 4)
# ==============================================================================

@seller_required
@require_GET
def seller_reports_view(request):
    """
    Dedicated seller reports and analytics dashboard.
    Provides date range filtering, fulfillment status counts, product sales volumes,
    and historical financial snapshot summaries strictly scoped to request.user.seller_profile.
    Clearly distinguishes active vs cancelled packages and historical snapshots from settled payouts.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    start_date_str = request.GET.get("start_date", "").strip()
    end_date_str = request.GET.get("end_date", "").strip()

    start_dt, end_dt, date_error = parse_date_range(start_date_str, end_date_str)
    if date_error:
        messages.warning(request, date_error)
        start_dt = None
        end_dt = None
        start_date_str = ""
        end_date_str = ""

    reports_data = get_seller_reports_data(seller=seller, start_dt=start_dt, end_dt=end_dt)

    context = {
        "seller": seller,
        "reports": reports_data,
        "start_date": start_date_str,
        "end_date": end_date_str,
        "has_date_filter": bool(start_dt or end_dt),
        "date_filter_error": date_error,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }
    return render(request, "seller/reports/index.html", context)


@seller_required
@require_GET
def seller_export_orders_csv_view(request):
    """
    Generates a secure, seller-scoped CSV export of order packages.
    Excludes other sellers' data, platform-direct items, customer emails,
    passwords, payment signatures, and sensitive customer data.
    Sanitizes values against CSV Formula Injection (CWE-1236).
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    start_date_str = request.GET.get("start_date", "").strip()
    end_date_str = request.GET.get("end_date", "").strip()

    start_dt, end_dt, date_error = parse_date_range(start_date_str, end_date_str)
    if date_error:
        messages.error(request, f"Unable to generate CSV export: {date_error}")
        return redirect("core:seller_reports")

    qs = SellerOrder.objects.filter(seller=seller)
    if start_dt:
        qs = qs.filter(created_at__gte=start_dt)
    if end_dt:
        qs = qs.filter(created_at__lte=end_dt)

    packages = qs.select_related("order").prefetch_related("items").order_by("-created_at")

    date_tag = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"fitmeezo_seller_orders_{date_tag}.csv"

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow([
        "Sub-Order Number",
        "Order Date",
        "Fulfillment Status",
        "Item Lines Count",
        "Total Quantity",
        "Subtotal (INR)",
        "Commission Snapshot (INR)",
        "Net Payable Snapshot (INR)",
        "Courier Name",
        "Tracking Number",
        "Shipped At",
        "Delivered At",
        "Cancelled At",
    ])

    for pkg in packages:
        items = list(pkg.items.all())
        total_units = sum(item.quantity for item in items)
        writer.writerow([
            sanitize_csv_cell(pkg.sub_order_number),
            pkg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            sanitize_csv_cell(pkg.get_status_display()),
            len(items),
            total_units,
            sanitize_csv_cell(f"{Decimal(str(pkg.subtotal)):.2f}"),
            sanitize_csv_cell(f"{Decimal(str(pkg.total_commission)):.2f}"),
            sanitize_csv_cell(f"{Decimal(str(pkg.net_seller_payable)):.2f}"),
            sanitize_csv_cell(pkg.courier_name or "N/A"),
            sanitize_csv_cell(pkg.tracking_number or "N/A"),
            pkg.shipped_at.strftime("%Y-%m-%d %H:%M:%S") if pkg.shipped_at else "N/A",
            pkg.delivered_at.strftime("%Y-%m-%d %H:%M:%S") if pkg.delivered_at else "N/A",
            pkg.cancelled_at.strftime("%Y-%m-%d %H:%M:%S") if pkg.cancelled_at else "N/A",
        ])

    return response


@seller_required
@require_GET
def seller_export_products_csv_view(request):
    """
    Generates a secure, seller-scoped CSV export of product sales quantities and revenues.
    Calculated exclusively from non-cancelled packages owned by the authenticated seller.
    Sanitizes values against CSV Formula Injection (CWE-1236).
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    start_date_str = request.GET.get("start_date", "").strip()
    end_date_str = request.GET.get("end_date", "").strip()

    start_dt, end_dt, date_error = parse_date_range(start_date_str, end_date_str)
    if date_error:
        messages.error(request, f"Unable to generate CSV export: {date_error}")
        return redirect("core:seller_reports")

    active_packages_qs = SellerOrder.objects.filter(seller=seller).exclude(status=SellerOrder.STATUS_CANCELLED)
    if start_dt:
        active_packages_qs = active_packages_qs.filter(created_at__gte=start_dt)
    if end_dt:
        active_packages_qs = active_packages_qs.filter(created_at__lte=end_dt)

    from django.db.models import Count, Sum
    from django.db.models.functions import Coalesce

    product_sales = (
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

    date_tag = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"fitmeezo_seller_products_{date_tag}.csv"

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow([
        "Product Name",
        "Variant Label",
        "SKU",
        "Units Sold",
        "Gross Sales (INR)",
        "Orders Count",
    ])

    for row in product_sales:
        writer.writerow([
            sanitize_csv_cell(row["product_name"]),
            sanitize_csv_cell(row["variant_label"] or "Standard"),
            sanitize_csv_cell(row["sku"] or "N/A"),
            row["units_sold"],
            sanitize_csv_cell(f"{Decimal(str(row['gross_sales'])):.2f}"),
            row["orders_count"],
        ])

    return response


@seller_required
@require_GET
def seller_export_financial_csv_view(request):
    """
    Generates a secure, seller-scoped CSV export of recorded financial snapshot summaries.
    Clearly notes that totals are recorded order snapshots and NOT settled payouts or transfers.
    Sanitizes values against CSV Formula Injection (CWE-1236).
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    start_date_str = request.GET.get("start_date", "").strip()
    end_date_str = request.GET.get("end_date", "").strip()

    start_dt, end_dt, date_error = parse_date_range(start_date_str, end_date_str)
    if date_error:
        messages.error(request, f"Unable to generate CSV export: {date_error}")
        return redirect("core:seller_reports")

    reports_data = get_seller_reports_data(seller=seller, start_dt=start_dt, end_dt=end_dt)

    date_tag = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"fitmeezo_seller_financial_{date_tag}.csv"

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow(["Fitmeezo Marketplace - Seller Financial Snapshot Summary"])
    writer.writerow(["Store Name", sanitize_csv_cell(seller.store_name)])
    writer.writerow(["Date Range", f"{start_date_str or 'Beginning'} to {end_date_str or 'Present'}"])
    writer.writerow(["Notice", "Recorded order snapshots only. Not settled payouts, transfers, or balance records."])
    writer.writerow([])

    writer.writerow(["Summary Metric", "Value", "Notes"])
    writer.writerow(["Total Packages Assigned", reports_data["total_packages"], "All packages within filtered period"])
    writer.writerow(["Active (Non-Cancelled) Packages", reports_data["active_packages_count"], "Packages eligible for fulfillment"])
    writer.writerow(["Gross Subtotal (Active Orders)", sanitize_csv_cell(f"{Decimal(str(reports_data['gross_sales'])):.2f}"), "INR - Sum of subtotal snapshots"])
    writer.writerow(["Platform Commission Snapshot (Active Orders)", sanitize_csv_cell(f"{Decimal(str(reports_data['total_commission'])):.2f}"), "INR - Stored snapshot totals"])
    writer.writerow(["Net Seller Payable Snapshot (Active Orders)", sanitize_csv_cell(f"{Decimal(str(reports_data['net_seller_payable'])):.2f}"), "INR - Recorded order snapshots (Not a settled payout)"])
    writer.writerow(["Cancelled Packages Count", reports_data["cancelled_count"], "Cancelled orders excluded from active totals"])
    writer.writerow(["Cancelled Packages Subtotal", sanitize_csv_cell(f"{Decimal(str(reports_data['cancelled_subtotal'])):.2f}"), "INR - Cancelled subtotal snapshots"])
    writer.writerow([])

    writer.writerow(["Package Breakdown (Financial Snapshots):"])
    writer.writerow([
        "Sub-Order Number",
        "Order Date",
        "Fulfillment Status",
        "Subtotal (INR)",
        "Commission Snapshot (INR)",
        "Net Payable Snapshot (INR)",
    ])

    base_qs = SellerOrder.objects.filter(seller=seller)
    if start_dt:
        base_qs = base_qs.filter(created_at__gte=start_dt)
    if end_dt:
        base_qs = base_qs.filter(created_at__lte=end_dt)

    for pkg in base_qs.order_by("-created_at"):
        writer.writerow([
            sanitize_csv_cell(pkg.sub_order_number),
            pkg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            sanitize_csv_cell(pkg.get_status_display()),
            sanitize_csv_cell(f"{Decimal(str(pkg.subtotal)):.2f}"),
            sanitize_csv_cell(f"{Decimal(str(pkg.total_commission)):.2f}"),
            sanitize_csv_cell(f"{Decimal(str(pkg.net_seller_payable)):.2f}"),
        ])

    return response


# ==============================================================================
# PHASE 6: RETURNS, EXCHANGES & REFUND MANAGEMENT VIEWS
# ==============================================================================

@login_required(login_url="core:account_login")
def order_return_request_view(request, order_number):
    """
    Customer return/exchange request submission view:
    - Scoped strictly to request.user and delivered packages.
    - Validates delivery status, return window, and remaining returnable quantity.
    - Supports selecting specific items and quantities without affecting other packages.
    """
    order = get_object_or_404(Order, order_number=order_number, user=request.user)
    if order.items.filter(seller_order__isnull=True).exists():
        from core.services import ensure_seller_orders_for_order
        ensure_seller_orders_for_order(order)

    delivered_packages = order.seller_orders.filter(
        status=SellerOrder.STATUS_DELIVERED
    ).select_related("seller").prefetch_related("items__product__variants", "items__variant")

    package_id = request.GET.get("package_id") or request.POST.get("package_id")
    target_package = None
    if package_id:
        target_package = delivered_packages.filter(pk=package_id).first()
    if not target_package and delivered_packages.count() == 1:
        target_package = delivered_packages.first()

    if request.method == "POST":
        if not target_package:
            messages.error(request, "Please select an eligible delivered package to initiate a return.")
            return redirect("core:order_return_request", order_number=order.order_number)

        form = ReturnRequestSubmissionForm(request.POST, request.FILES)
        if form.is_valid():
            req_type = form.cleaned_data["request_type"]
            reason = form.cleaned_data["reason"]
            notes = form.cleaned_data["customer_notes"]
            p1 = form.cleaned_data.get("photo_1")
            p2 = form.cleaned_data.get("photo_2")
            p3 = form.cleaned_data.get("photo_3")

            items_data = []
            for item in target_package.items.all():
                is_selected = request.POST.get(f"item_{item.id}_selected") == "on"
                if is_selected:
                    raw_qty = request.POST.get(f"item_{item.id}_quantity", "1")
                    try:
                        qty = int(raw_qty)
                    except ValueError:
                        qty = 1
                    var_id = request.POST.get(f"item_{item.id}_exchange_variant") or None
                    items_data.append({
                        "order_item": item,
                        "quantity": qty,
                        "exchange_variant_id": var_id,
                    })

            if not items_data:
                messages.error(request, "Please select at least one item to return or exchange.")
            else:
                try:
                    ret_req = create_return_request(
                        customer=request.user,
                        seller_order=target_package,
                        items_data=items_data,
                        request_type=req_type,
                        reason=reason,
                        customer_notes=notes,
                        photo_1=p1,
                        photo_2=p2,
                        photo_3=p3,
                    )
                    messages.success(
                        request,
                        f"Your {ret_req.get_request_type_display().lower()} request #{ret_req.request_number} has been submitted successfully."
                    )
                    return redirect("core:customer_return_detail", request_number=ret_req.request_number)
                except (ReturnValidationError, ReturnPermissionError, ReturnServiceError) as err:
                    messages.error(request, str(err))
        else:
            messages.error(request, "Please correct the errors in the form below.")
    else:
        form = ReturnRequestSubmissionForm(initial={"request_type": "return", "reason": "size_fit"})

    preselected_item_id = request.GET.get("item_id")
    try:
        preselected_item_id = int(preselected_item_id) if preselected_item_id else None
    except (ValueError, TypeError):
        preselected_item_id = None

    context = {
        "order": order,
        "delivered_packages": delivered_packages,
        "target_package": target_package,
        "preselected_item_id": preselected_item_id,
        "form": form,
        "window_days": getattr(settings, "FITMEEZO_RETURN_WINDOW_DAYS", 7),
        "exchange_enabled": getattr(settings, "FITMEEZO_EXCHANGE_ENABLED", True),
    }
    return render(request, "orders/return_request_form.html", context)


@login_required(login_url="core:account_login")
def customer_return_list_view(request):
    """
    Displays the customer's return and exchange history.
    """
    return_requests = ReturnRequest.objects.filter(
        customer=request.user
    ).select_related("order", "seller_order", "seller").prefetch_related("items__order_item").order_by("-created_at")

    return render(request, "orders/return_list.html", {"return_requests": return_requests})


@login_required(login_url="core:account_login")
def customer_return_detail_view(request, request_number):
    """
    Detailed status and lifecycle timeline view for a customer return request.
    """
    return_request = get_object_or_404(
        ReturnRequest.objects.select_related("order", "seller_order", "seller")
        .prefetch_related(
            "items__order_item__product",
            "items__exchange_variant",
            "audit_logs__actor",
            "refund_records",
        ),
        request_number=request_number,
        customer=request.user,
    )
    return render(request, "orders/return_detail.html", {"return_request": return_request})


@login_required(login_url="core:account_login")
@require_POST
def customer_return_cancel_view(request, request_number):
    """
    Allows a customer to cancel a return request before pickup is scheduled.
    """
    return_request = get_object_or_404(
        ReturnRequest,
        request_number=request_number,
        customer=request.user,
    )
    try:
        transition_return_status(
            return_request=return_request,
            new_status=ReturnRequest.STATUS_CANCELLED,
            actor=request.user,
            actor_role="customer",
            notes="Cancelled by customer",
        )
        messages.success(request, f"Return request #{return_request.request_number} was successfully cancelled.")
    except (ReturnValidationError, ReturnPermissionError, ReturnServiceError) as err:
        messages.error(request, str(err))

    return redirect("core:customer_return_detail", request_number=return_request.request_number)


@seller_required
def seller_return_list_view(request):
    """
    Displays return requests for a specific seller's packages only.
    Platform-fulfilled merchandise is excluded.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    status_filter = request.GET.get("status", "").strip()
    return_requests = ReturnRequest.objects.filter(
        seller=seller,
        is_platform_fulfilled=False,
    ).select_related("order", "seller_order", "customer").prefetch_related("items__order_item")

    if status_filter:
        return_requests = return_requests.filter(status=status_filter)

    return_requests = return_requests.order_by("-created_at")

    context = {
        "return_requests": return_requests,
        "current_status": status_filter,
        "status_choices": ReturnRequest.STATUS_CHOICES,
    }
    return render(request, "seller/returns/list.html", context)


@seller_required
def seller_return_detail_view(request, request_number):
    """
    Seller portal return management view:
    Shows customer reasons, inspected items, allowed transition actions, and refund options.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    return_request = get_object_or_404(
        ReturnRequest.objects.select_related("order", "seller_order", "seller", "customer")
        .prefetch_related(
            "items__order_item__product",
            "items__exchange_variant",
            "audit_logs__actor",
            "refund_records",
        ),
        request_number=request_number,
        seller=seller,
        is_platform_fulfilled=False,
    )

    allowed_transitions = VALID_TRANSITIONS.get(return_request.status, [])
    action_form = SellerReturnActionForm(
        current_status=return_request.status,
        allowed_transitions=allowed_transitions,
    )
    refund_form = ProcessRefundForm(
        initial={"approved_amount": return_request.total_refund_amount}
    )

    context = {
        "return_request": return_request,
        "allowed_transitions": allowed_transitions,
        "action_form": action_form,
        "refund_form": refund_form,
    }
    return render(request, "seller/returns/detail.html", context)


@seller_required
@require_POST
def seller_return_action_view(request, request_number):
    """
    Seller action handler: updates return status, records inspection results, or triggers simulated refund.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    return_request = get_object_or_404(
        ReturnRequest,
        request_number=request_number,
        seller=seller,
        is_platform_fulfilled=False,
    )

    action = request.POST.get("action", "transition")

    if action == "refund":
        refund_form = ProcessRefundForm(request.POST)
        if refund_form.is_valid():
            amt = refund_form.cleaned_data["approved_amount"]
            reason = refund_form.cleaned_data["reason"]
            try:
                refund_rec = process_simulated_refund(
                    return_request=return_request,
                    actor=request.user,
                    approved_amount=amt,
                    reason=reason,
                )
                messages.success(
                    request,
                    f"Mock refund #{refund_rec.refund_reference} (₹{refund_rec.approved_amount}) settled successfully."
                )
            except (RefundError, ReturnServiceError) as err:
                messages.error(request, str(err))
        else:
            messages.error(request, "Invalid refund form submission.")
        return redirect("core:seller_return_detail", request_number=return_request.request_number)

    # Status transition
    new_status = request.POST.get("status")
    notes = request.POST.get("notes", "")
    rejection_reason = request.POST.get("rejection_reason", "")

    # Parse inspection decisions if under inspection
    inspection_decisions = {}
    for item in return_request.items.all():
        insp_st = request.POST.get(f"item_{item.id}_inspection_status")
        restock = request.POST.get(f"item_{item.id}_restock") == "on"
        insp_notes = request.POST.get(f"item_{item.id}_inspection_notes", "")
        if insp_st:
            inspection_decisions[item.id] = {
                "status": insp_st,
                "restock": restock,
                "notes": insp_notes,
            }

    try:
        transition_return_status(
            return_request=return_request,
            new_status=new_status,
            actor=request.user,
            actor_role="seller",
            notes=notes,
            rejection_reason=rejection_reason,
            inspection_decisions=inspection_decisions if inspection_decisions else None,
        )
        messages.success(
            request,
            f"Return request status updated to '{dict(ReturnRequest.STATUS_CHOICES).get(new_status, new_status)}'."
        )
    except (ReturnValidationError, ReturnPermissionError, ReturnServiceError) as err:
        messages.error(request, str(err))

    return redirect("core:seller_return_detail", request_number=return_request.request_number)


# ==============================================================================
# PHASE 7: SELLER FINANCIAL LEDGER & PAYOUT VIEWS (TENANT ISOLATED)
# ==============================================================================

@seller_required
def seller_ledger_view(request):
    """
    Seller portal financial ledger view.
    Strictly isolated to request.user.seller_profile.
    Displays all immutable credit and debit entries, running balance, COD hold, and category filters.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    qs = SellerLedgerEntry.objects.filter(
        seller=seller,
        is_platform_fulfilled=False,
    ).select_related("order", "seller_order", "payout_batch", "refund_record").order_by("-created_at", "-id")

    category_param = request.GET.get("category", "").strip()
    status_param = request.GET.get("status", "").strip()
    type_param = request.GET.get("type", "").strip()
    q_param = request.GET.get("q", "").strip()

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
        )

    summary = get_seller_ledger_summary(seller=seller)

    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "seller": seller,
        "page_obj": page_obj,
        "summary": summary,
        "selected_category": category_param,
        "selected_status": status_param,
        "selected_type": type_param,
        "search_query": q_param,
        "category_choices": SellerLedgerEntry.CATEGORY_CHOICES,
        "status_choices": SellerLedgerEntry.STATUS_CHOICES,
        "entry_type_choices": SellerLedgerEntry.ENTRY_TYPE_CHOICES,
        "total_count": paginator.count,
    }
    return render(request, "seller/ledger/index.html", context)


@seller_required
@require_GET
def seller_export_ledger_csv_view(request):
    """
    Generates a secure, seller-scoped CSV export of ledger entries.
    Strictly excludes other merchants' records and platform-direct accounting.
    Sanitizes values against CSV Formula Injection (CWE-1236).
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    qs = SellerLedgerEntry.objects.filter(
        seller=seller,
        is_platform_fulfilled=False,
    ).select_related("order", "seller_order", "payout_batch").order_by("-created_at")

    date_tag = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"seller_ledger_{seller.slug}_{date_tag}.csv"

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow([
        "Reference Key",
        "Timestamp",
        "Entry Type",
        "Category",
        "Amount (INR)",
        "Signed Amount (INR)",
        "Posting Status",
        "Source Type",
        "Source Reference",
        "Sub-Order Number",
        "Payout Batch",
        "Description",
    ])

    for entry in qs:
        sub_order_num = entry.seller_order.sub_order_number if entry.seller_order else ""
        batch_ref = entry.payout_batch.batch_reference if entry.payout_batch else "Unbatched"

        writer.writerow([
            sanitize_csv_cell(entry.idempotency_key),
            sanitize_csv_cell(entry.created_at.strftime("%Y-%m-%d %H:%M:%S")),
            sanitize_csv_cell(entry.entry_type.upper()),
            sanitize_csv_cell(entry.get_category_display()),
            sanitize_csv_cell(str(entry.amount)),
            sanitize_csv_cell(str(entry.signed_amount)),
            sanitize_csv_cell(entry.get_status_display()),
            sanitize_csv_cell(entry.source_type),
            sanitize_csv_cell(entry.source_reference),
            sanitize_csv_cell(sub_order_num),
            sanitize_csv_cell(batch_ref),
            sanitize_csv_cell(entry.description),
        ])

    return response


@seller_required
def seller_payout_list_view(request):
    """
    Seller portal payout history dashboard.
    Lists all mock payout batches disbursed or prepared for this seller.
    Displays real-time payout eligibility status.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    qs = PayoutBatch.objects.filter(
        seller=seller,
        is_platform_fulfilled=False,
    ).order_by("-created_at")

    eligibility = calculate_seller_payout_eligibility(seller=seller)

    paginator = Paginator(qs, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "seller": seller,
        "page_obj": page_obj,
        "eligibility": eligibility,
        "total_batches": paginator.count,
    }
    return render(request, "seller/payouts/index.html", context)


@seller_required
def seller_payout_detail_view(request, batch_reference):
    """
    Seller portal view of a specific PayoutBatch.
    Shows the covered order packages and financial entries.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    batch = get_object_or_404(
        PayoutBatch,
        batch_reference=batch_reference,
        seller=seller,
        is_platform_fulfilled=False,
    )
    covered_entries = batch.ledger_entries.select_related("order", "seller_order").order_by("-created_at")

    context = {
        "seller": seller,
        "batch": batch,
        "covered_entries": covered_entries,
    }
    return render(request, "seller/payouts/detail.html", context)


@seller_required
def seller_bank_details_view(request):
    """
    Seller portal view to manage and review payout settlement bank details.
    Enforces seller ownership, masks account numbers,
    and requires admin verification before unlocking payouts.
    """
    seller = getattr(request.user, "seller_profile", None)
    if not seller:
        messages.warning(request, "You do not have an active seller profile.")
        return redirect("core:seller_register")

    if request.method == "POST":
        form = SellerBankDetailsForm(request.POST, seller=seller)
        if form.is_valid():
            form.save(seller, actor=request.user)
            messages.success(
                request,
                "Bank account details submitted successfully. Verification by platform finance compliance is pending. "
                "Your account number is securely stored and masked for privacy."
            )
            return redirect("core:seller_bank_details")
        else:
            messages.error(request, "Please correct the errors in the bank details form below.")
    else:
        form = SellerBankDetailsForm(seller=seller)

    context = {
        "seller": seller,
        "form": form,
        "masked_account": seller.get_masked_account_number(),
        "has_bank_details": bool(seller.bank_account_number and seller.bank_ifsc_code),
        "bank_status": seller.bank_verification_status,
        "is_verified": seller.is_bank_payout_eligible,
        "rejection_reason": seller.bank_rejection_reason,
    }
    return render(request, "seller/bank_details.html", context)




