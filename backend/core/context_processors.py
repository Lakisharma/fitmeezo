from django.conf import settings


def brand_context(request):
    """
    Exposes brand and market constants to all Django templates.
    """
    return {
        "BRAND_NAME": getattr(settings, "BRAND_NAME", "Fitmeezo"),
        "BRAND_TAGLINE": getattr(settings, "BRAND_TAGLINE", "SHOP SMART • LIVE BETTER"),
        "CURRENCY": getattr(settings, "CURRENCY", "INR"),
        "CURRENCY_SYMBOL": getattr(settings, "CURRENCY_SYMBOL", "₹"),
    }
