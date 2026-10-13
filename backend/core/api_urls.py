"""
Fitmeezo Backend REST API URL Routing.
Exposes endpoints for catalogue browsing, authentication, profile, addresses,
shopping cart, checkout, and customer orders.
"""

from django.urls import path
from .api_views import (
    api_address_detail,
    api_address_list_create,
    api_address_set_default,
    api_cart_add,
    api_cart_clear,
    api_cart_detail,
    api_cart_remove,
    api_cart_update,
    api_category_detail,
    api_category_list,
    api_checkout_place_order,
    api_checkout_summary,
    api_login,
    api_logout,
    api_order_cancel,
    api_order_detail,
    api_order_list,
    api_product_detail,
    api_product_list,
    api_profile,
    api_register,
)
from .views import (
    product_reviews_api,
    product_variants_api,
    wishlist_api,
    wishlist_api_detail,
)

app_name = "api"

urlpatterns = [
    # Catalogue (Public)
    path("categories/", api_category_list, name="category_list"),
    path("categories/<slug:slug>/", api_category_detail, name="category_detail"),
    path("products/", api_product_list, name="product_list"),
    path("products/<slug:slug>/variants/", product_variants_api, name="product_variants"),
    path("products/<slug:slug>/reviews/", product_reviews_api, name="product_reviews"),
    path("products/<slug:slug>/", api_product_detail, name="product_detail"),

    # Wishlist (Authenticated)
    path("wishlist/", wishlist_api, name="wishlist_api"),
    path("wishlist/<int:pk>/", wishlist_api_detail, name="wishlist_api_detail"),

    # Authentication & Profile
    path("auth/register/", api_register, name="register"),
    path("auth/login/", api_login, name="login"),
    path("auth/logout/", api_logout, name="logout"),
    path("auth/profile/", api_profile, name="profile"),

    # Saved Addresses (Authenticated)
    path("addresses/", api_address_list_create, name="address_list_create"),
    path("addresses/<int:pk>/", api_address_detail, name="address_detail"),
    path("addresses/<int:pk>/set-default/", api_address_set_default, name="address_set_default"),

    # Shopping Cart
    path("cart/", api_cart_detail, name="cart_detail"),
    path("cart/add/", api_cart_add, name="cart_add"),
    path("cart/update/", api_cart_update, name="cart_update"),
    path("cart/remove/", api_cart_remove, name="cart_remove"),
    path("cart/clear/", api_cart_clear, name="cart_clear"),

    # Checkout & Orders (Authenticated)
    path("checkout/summary/", api_checkout_summary, name="checkout_summary"),
    path("checkout/place-order/", api_checkout_place_order, name="checkout_place_order"),
    path("orders/", api_order_list, name="order_list"),
    path("orders/<str:order_number>/cancel/", api_order_cancel, name="order_cancel"),
    path("orders/<str:order_number>/", api_order_detail, name="order_detail"),
]
