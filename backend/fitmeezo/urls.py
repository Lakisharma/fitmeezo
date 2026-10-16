"""
URL configuration for Fitmeezo project.

Fitmeezo - SHOP SMART • LIVE BETTER
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from core.views import health_check

urlpatterns = [
    path("admin/", admin.site.urls),
    # Fitmeezo Custom Admin Dashboard (Phase 1)
    path("custom-admin/", include("core.admin_urls", namespace="custom_admin")),
    # Fitmeezo Management Console (Third Independent Admin Panel)
    path("management-console/", include("management_console.urls", namespace="management_console")),
    # Compatibility redirect for legacy/alternate 'admin-portal/' prefix
    path("admin-portal/<path:subpath>", RedirectView.as_view(url="/custom-admin/%(subpath)s", permanent=False)),
    path("admin-portal/", RedirectView.as_view(url="/custom-admin/", permanent=False)),
    # Fitmeezo REST API (Storefront & Mobile Backend)
    path("api/", include("core.api_urls", namespace="api")),
    # Direct health-check endpoint required by task specification
    path("health/", health_check, name="health-check"),
    # Allauth & Social OAuth routes (Prompt 11)
    path("accounts/", include("allauth.urls")),
    # Core app URLs
    path("", include("core.urls")),
]

if settings.DEBUG:
    if settings.STATICFILES_DIRS:
        urlpatterns += static(settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0])
    else:
        urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

