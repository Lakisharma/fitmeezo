"""
URL configuration for Fitmeezo Management Console.
Namespace: management_console
"""

from django.urls import path
from .views import (
    console_audit_logs_view,
    console_dashboard_view,
    console_global_search_view,
    console_inventory_monitor_view,
    console_login_view,
    console_logout_view,
    console_model_add_view,
    console_model_change_view,
    console_model_delete_view,
    console_model_list_view,
    console_order_detail_view,
    console_product_add_view,
    console_seller_approvals_view,
)

app_name = "management_console"

urlpatterns = [
    # Dashboard & Global Actions
    path("", console_dashboard_view, name="dashboard"),
    path("login/", console_login_view, name="login"),
    path("logout/", console_logout_view, name="logout"),
    path("search/", console_global_search_view, name="global_search"),
    path("audit-logs/", console_audit_logs_view, name="audit_logs"),

    # Specialized E-Commerce Workflows
    path("products/add/", console_product_add_view, name="product_add"),
    path("inventory/", console_inventory_monitor_view, name="inventory_monitor"),
    path("sellers/approvals/", console_seller_approvals_view, name="seller_approvals"),
    path("orders/<int:pk>/", console_order_detail_view, name="order_detail"),

    # Dynamic Model CRUD (Supports all 30 registered Django Admin models)
    path("models/<str:app_label>/<str:model_name>/", console_model_list_view, name="model_list"),
    path("models/<str:app_label>/<str:model_name>/add/", console_model_add_view, name="model_add"),
    path("models/<str:app_label>/<str:model_name>/<str:pk>/", console_model_change_view, name="model_change"),
    path("models/<str:app_label>/<str:model_name>/<str:pk>/delete/", console_model_delete_view, name="model_delete"),
]
