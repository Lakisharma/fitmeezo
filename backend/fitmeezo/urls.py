"""
URL configuration for Fitmeezo project.

Fitmeezo - SHOP SMART • LIVE BETTER
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from core.views import health_check

urlpatterns = [
    path("admin/", admin.site.urls),
    # Direct health-check endpoint required by task specification
    path("health/", health_check, name="health-check"),
    # Core app URLs
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
