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
    admin_user_role = None
    can_manage_products = False
    can_manage_sellers = False
    can_manage_finance = False
    can_manage_support = False
    can_manage_users = False
    can_manage_audit = False
    can_manage_settings = False
    admin_pending_sellers_count = 0
    admin_pending_products_count = 0
    admin_pending_payouts_count = 0
    admin_low_stock_count = 0
    user = getattr(request, "user", None)
    if user and user.is_authenticated and (user.is_staff or user.is_superuser):
        try:
            from core.admin_permissions import (
                get_user_role,
                ROLE_PERMISSIONS,
                ROLE_SUPER_ADMIN,
            )
            from core.models import Seller, ReturnRequest, Product, PayoutBatch
            admin_user_role = get_user_role(user)
            perms = ROLE_PERMISSIONS.get(admin_user_role, {}) if not user.is_superuser else ROLE_PERMISSIONS[ROLE_SUPER_ADMIN]
            can_manage_products = perms.get("can_manage_products", False) or user.is_superuser
            can_manage_sellers = perms.get("can_manage_sellers", False) or user.is_superuser
            can_manage_finance = perms.get("can_manage_finance", False) or user.is_superuser
            can_manage_support = perms.get("can_manage_support", False) or user.is_superuser
            can_manage_users = perms.get("can_manage_users", False) or user.is_superuser
            can_manage_audit = perms.get("can_manage_audit", False) or user.is_superuser
            can_manage_settings = perms.get("can_manage_settings", False) or user.is_superuser

            admin_pending_bank_verifications_count = Seller.objects.filter(bank_verification_status="pending").count()
            admin_pending_returns_count = ReturnRequest.objects.filter(status=ReturnRequest.STATUS_REQUESTED).count()
            admin_pending_sellers_count = Seller.objects.filter(status="pending").count()
            admin_pending_products_count = Product.objects.filter(approval_status="pending").count()
            admin_pending_payouts_count = PayoutBatch.objects.filter(status="pending").count()
            admin_low_stock_count = Product.objects.filter(is_active=True, stock_quantity__lt=10).count()
        except Exception:
            pass

    admin_total_notifications_count = (
        admin_pending_bank_verifications_count
        + admin_pending_returns_count
        + admin_pending_sellers_count
        + admin_pending_products_count
        + admin_pending_payouts_count
    )

    return {
        "BRAND_NAME": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        "BRAND_TAGLINE": getattr(settings, "BRAND_TAGLINE", "SHOP SMART • LIVE BETTER"),
        "SUPPORT_EMAIL": getattr(settings, "SUPPORT_EMAIL", "support@fitmeezo.com"),
        "SUPPORT_PHONE": getattr(settings, "FITMEEZO_SUPPORT_PHONE", getattr(settings, "SUPPORT_PHONE", "+91 940885323")),
        "SUPPORT_PHONE_TEL": getattr(settings, "FITMEEZO_SUPPORT_PHONE", getattr(settings, "SUPPORT_PHONE", "+91 940885323")).replace(" ", "").replace("-", ""),
        "CURRENCY": getattr(settings, "CURRENCY", "INR"),
        "CURRENCY_SYMBOL": getattr(settings, "CURRENCY_SYMBOL", "₹"),
        "SOCIAL_INSTAGRAM_URL": getattr(settings, "SOCIAL_INSTAGRAM_URL", ""),
        "SOCIAL_FACEBOOK_URL": getattr(settings, "SOCIAL_FACEBOOK_URL", ""),
        "SOCIAL_X_URL": getattr(settings, "SOCIAL_X_URL", ""),
        "SOCIAL_LINKEDIN_URL": getattr(settings, "SOCIAL_LINKEDIN_URL", ""),
        "SOCIAL_YOUTUBE_URL": getattr(settings, "SOCIAL_YOUTUBE_URL", ""),
        "nav_categories": nav_categories,
        "admin_user_role": admin_user_role,
        "can_manage_products": can_manage_products,
        "can_manage_sellers": can_manage_sellers,
        "can_manage_finance": can_manage_finance,
        "can_manage_support": can_manage_support,
        "can_manage_users": can_manage_users,
        "can_manage_audit": can_manage_audit,
        "can_manage_settings": can_manage_settings,
        "admin_pending_bank_verifications_count": admin_pending_bank_verifications_count,
        "admin_pending_returns_count": admin_pending_returns_count,
        "admin_pending_sellers_count": admin_pending_sellers_count,
        "admin_pending_products_count": admin_pending_products_count,
        "admin_pending_payouts_count": admin_pending_payouts_count,
        "admin_low_stock_count": admin_low_stock_count,
        "admin_total_notifications_count": admin_total_notifications_count,
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
    Checks environment credentials or database-backed SocialApp record.
    """
    google_client_id = getattr(settings, "GOOGLE_CLIENT_ID", "")
    google_secret = getattr(settings, "GOOGLE_CLIENT_SECRET", "")
    google_enabled = bool(google_client_id and google_secret)
    if not google_enabled:
        try:
            from allauth.socialaccount.models import SocialApp
            google_enabled = SocialApp.objects.filter(provider="google").exists()
        except Exception:
            google_enabled = False

    return {
        "google_oauth_enabled": google_enabled,
        "facebook_oauth_enabled": False,
    }


