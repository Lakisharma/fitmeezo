from django.conf import settings


def brand_context(request):
    """
    Exposes brand, market constants, and active category navigation
    to all Django templates.
    """
    nav_categories = []
    try:
        from core.models import Category
        nav_categories = list(
            Category.objects.filter(is_active=True).order_by("display_order", "name")
        )
    except Exception:
        pass

    admin_pending_bank_verifications_count = 0
    admin_pending_returns_count = 0
    user = getattr(request, "user", None)
    if user and user.is_authenticated and (user.is_staff or user.is_superuser):
        try:
            from core.models import Seller, ReturnRequest
            admin_pending_bank_verifications_count = Seller.objects.filter(bank_verification_status="pending").count()
            admin_pending_returns_count = ReturnRequest.objects.filter(status=ReturnRequest.STATUS_REQUESTED).count()
        except Exception:
            pass

    return {
        "BRAND_NAME": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        "BRAND_TAGLINE": getattr(settings, "BRAND_TAGLINE", "SHOP SMART • LIVE BETTER"),
        "SUPPORT_EMAIL": getattr(settings, "SUPPORT_EMAIL", "support@fitmeezo.com"),
        "CURRENCY": getattr(settings, "CURRENCY", "INR"),
        "CURRENCY_SYMBOL": getattr(settings, "CURRENCY_SYMBOL", "₹"),
        "nav_categories": nav_categories,
        "admin_pending_bank_verifications_count": admin_pending_bank_verifications_count,
        "admin_pending_returns_count": admin_pending_returns_count,
    }


def cart_context(request):
    """
    Exposes real-time cart summary metrics (total units, subtotal)
    to all Django templates for navigation badges and header displays.
    """
    try:
        from core.cart import Cart
        cart = Cart(request)
        return {
            "cart_total_units": cart.total_units,
            "cart_subtotal": cart.subtotal,
        }
    except Exception:
        return {
            "cart_total_units": 0,
            "cart_subtotal": 0,
        }


def wishlist_context(request):
    """
    Exposes customer's wishlist count and set of wishlisted product IDs
    to all templates for navigation badges and heart icon active states.
    """
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        try:
            from core.models import WishlistItem
            user_wishlist_ids = set(
                WishlistItem.objects.filter(user=user).values_list("product_id", flat=True)
            )
            return {
                "wishlist_count": len(user_wishlist_ids),
                "user_wishlist_product_ids": user_wishlist_ids,
            }
        except Exception:
            return {
                "wishlist_count": 0,
                "user_wishlist_product_ids": set(),
            }
    return {
        "wishlist_count": 0,
        "user_wishlist_product_ids": set(),
    }


def oauth_context(request):
    """
    Exposes OAuth provider availability status to templates.
    Allows templates to display setup indicators or handle unconfigured providers safely.
    """
    google_client_id = getattr(settings, "GOOGLE_CLIENT_ID", "")
    google_secret = getattr(settings, "GOOGLE_CLIENT_SECRET", "")
    facebook_client_id = getattr(settings, "FACEBOOK_CLIENT_ID", "")
    facebook_secret = getattr(settings, "FACEBOOK_CLIENT_SECRET", "")

    return {
        "google_oauth_enabled": bool(google_client_id and google_secret),
        "facebook_oauth_enabled": bool(facebook_client_id and facebook_secret),
    }

