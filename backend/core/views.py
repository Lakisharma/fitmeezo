from django.conf import settings
from django.db import connection
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response


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
