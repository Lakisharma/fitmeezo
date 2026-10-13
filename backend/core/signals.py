"""
Fitmeezo Signal Handlers for Social Authentication and User Events.
"""

from allauth.account.signals import user_logged_in
from django.contrib import messages
from django.dispatch import receiver

from .cart import Cart
from .models import CustomerProfile


@receiver(user_logged_in)
def handle_allauth_user_logged_in(sender, request, user, **kwargs):
    """
    Hook executed whenever a customer authenticates via Google, Facebook,
    or allauth social login.
    1. Ensures CustomerProfile exists for the user.
    2. Automatically merges any anonymous guest session cart into user's persistent cart.
    3. Adds a welcoming customer notice.
    """
    CustomerProfile.objects.get_or_create(user=user)
    if request:
        cart = Cart(request)
        cart.merge_session_cart()
        name = user.first_name or user.username
        welcome_msg = f"Welcome to Fitmeezo, {name}!"
        try:
            messages.success(request, welcome_msg)
        except Exception:
            pass
