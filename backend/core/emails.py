"""
Fitmeezo Email Notification Service.
Handles transactional notifications for:
- Customer Order Confirmation
- Online Payment Verification & Success
- Online Payment Failure / Cancellation
- Admin Alerts

All functions operate safely with try-except blocks and fall back gracefully
so that email failures never block order placement or payment workflows.
"""

import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def send_order_confirmation_email(order) -> bool:
    """
    Sends an order confirmation email to the customer with full line items,
    financial breakdown, address snapshot, and payment method details.
    """
    if not order.user or not order.user.email:
        logger.info(f"Skipping order confirmation email for order #{order.order_number}: no customer email.")
        return False

    recipient_email = order.user.email.strip()
    subject = f"Order Confirmed: #{order.order_number} | Fitmeezo"
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Fitmeezo <support@fitmeezo.com>")

    context = {
        "order": order,
        "items": order.items.all(),
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }

    try:
        html_content = render_to_string("emails/order_confirmation.html", context)
        text_content = render_to_string("emails/order_confirmation.txt", context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Order confirmation email sent for #{order.order_number} to {recipient_email}")
        return True
    except Exception as exc:
        logger.warning(
            f"Failed to send order confirmation email for #{order.order_number} to {recipient_email}: {exc}"
        )
        return False


def send_payment_success_email(order) -> bool:
    """
    Sends a payment receipt email once online payment signature is cryptographically verified.
    """
    if not order.user or not order.user.email:
        logger.info(f"Skipping payment success email for order #{order.order_number}: no customer email.")
        return False

    recipient_email = order.user.email.strip()
    subject = f"Payment Received: Order #{order.order_number} | Fitmeezo"
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Fitmeezo <support@fitmeezo.com>")

    context = {
        "order": order,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }

    try:
        html_content = render_to_string("emails/payment_success.html", context)
        text_content = render_to_string("emails/payment_success.txt", context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Payment success email sent for #{order.order_number} to {recipient_email}")
        return True
    except Exception as exc:
        logger.warning(
            f"Failed to send payment success email for #{order.order_number} to {recipient_email}: {exc}"
        )
        return False


def send_payment_failed_email(order, reason: str = None) -> bool:
    """
    Sends a payment failure notification advising customer about the declined transaction
    and giving instructions to reorder with COD or another payment method.
    """
    if not order.user or not order.user.email:
        logger.info(f"Skipping payment failed email for order #{order.order_number}: no customer email.")
        return False

    recipient_email = order.user.email.strip()
    subject = f"Payment Unsuccessful: Order #{order.order_number} | Fitmeezo"
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Fitmeezo <support@fitmeezo.com>")

    context = {
        "order": order,
        "reason": reason,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }

    try:
        html_content = render_to_string("emails/payment_failed.html", context)
        text_content = render_to_string("emails/payment_failed.txt", context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Payment failed email sent for #{order.order_number} to {recipient_email}")
        return True
    except Exception as exc:
        logger.warning(
            f"Failed to send payment failed email for #{order.order_number} to {recipient_email}: {exc}"
        )
        return False


def send_order_cancellation_email(order, reason="", cancelled_by="customer") -> bool:
    """
    Sends an order cancellation confirmation email to the customer.
    Safe with fail_silently/try-except so cancellation is never blocked by email failures.
    """
    if not order.user or not order.user.email:
        logger.info(f"Skipping cancellation email for #{order.order_number}: no customer email.")
        return False

    recipient_email = order.user.email.strip()
    subject = f"Order Cancelled: #{order.order_number} | Fitmeezo"
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "Fitmeezo <support@fitmeezo.com>")

    context = {
        "order": order,
        "reason": reason,
        "cancelled_by": cancelled_by,
        "brand_name": getattr(settings, "BRAND_NAME", "Fitmeezo"),
    }

    try:
        html_content = render_to_string("emails/order_cancelled.html", context)
        text_content = render_to_string("emails/order_cancelled.txt", context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Cancellation email sent for #{order.order_number} to {recipient_email}")
        return True
    except Exception as exc:
        logger.warning(
            f"Failed to send cancellation email for #{order.order_number} to {recipient_email}: {exc}"
        )
        return False

