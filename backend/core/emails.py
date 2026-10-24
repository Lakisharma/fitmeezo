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


def send_contact_inquiry_email(inquiry) -> bool:
    """
    Sends customer acknowledgment and platform notification emails for a submitted contact inquiry.
    Fails silently on network / SMTP errors so inquiry persistence is never interrupted.
    """
    if not inquiry or not inquiry.email:
        return False

    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
    support_email = getattr(settings, "SUPPORT_EMAIL", "support@fitmeezo.com")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", f"{brand_name} <{support_email}>")

    subject = f"We have received your message: #{inquiry.id} | {brand_name} Support"
    text_content = (
        f"Dear {inquiry.full_name},\n\n"
        f"Thank you for contacting {brand_name}. We have received your inquiry regarding "
        f"'{inquiry.get_subject_display()}'.\n\n"
        f"Inquiry Reference: #{inquiry.id}\n"
        f"Your Message:\n{inquiry.message}\n\n"
        f"Our customer care team typically responds within 24 business hours.\n\n"
        f"Warm regards,\n"
        f"{brand_name} Customer Care\n"
        f"Helpline: {getattr(settings, 'SUPPORT_PHONE', '+91 940885323')} (Mon-Sat 9am-9pm IST)\n"
        f"Email: {support_email}\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[inquiry.email.strip()],
        )
        msg.send(fail_silently=False)
        logger.info(f"Inquiry confirmation email dispatched to {inquiry.email} for #{inquiry.id}")
        return True
    except Exception as exc:
        logger.warning(f"Could not dispatch inquiry email for #{inquiry.id} to {inquiry.email}: {exc}")
        return False


def send_order_refund_email(order, refund_record=None) -> bool:
    """
    Sends an automated refund notification email to the customer with refund amount,
    gateway reference, and expected banking settlement timeline.
    Fails silently so background/webhook workflows are never interrupted.
    """
    if not order or not order.user or not order.user.email:
        logger.info(f"Skipping refund notification email: no valid customer email.")
        return False

    recipient_email = order.user.email.strip()
    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", f"{brand_name} <support@fitmeezo.com>")
    subject = f"Refund Update: Order #{order.order_number} | {brand_name}"

    context = {
        "order": order,
        "refund_record": refund_record,
        "brand_name": brand_name,
    }

    try:
        html_content = render_to_string("emails/order_refund.html", context)
        text_content = render_to_string("emails/order_refund.txt", context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(f"Refund notification email sent for #{order.order_number} to {recipient_email}")
        return True
    except Exception as exc:
        logger.warning(
            f"Failed to send refund notification email for #{order.order_number} to {recipient_email}: {exc}"
        )
        return False


def send_seller_closure_finalized_email(seller, notes: str = "") -> bool:
    """
    Sends an account closure finalization notification email to the seller (Phase 5).
    Informs the vendor that all order, return, refund, cooling, and financial clearance
    checks have passed and the account is permanently closed with read-only portal access.

    Fails safely with try-except so email transport errors never roll back the primary
    account closure transaction.
    """
    if not seller or not seller.user or not seller.user.email:
        logger.info(f"Skipping closure email for seller #{getattr(seller, 'id', 'N/A')}: no valid email address.")
        return False

    recipient_email = seller.user.email.strip()
    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", f"{brand_name} <support@fitmeezo.com>")
    subject = f"Account Formally Closed: {seller.store_name} | {brand_name}"

    text_content = (
        f"Dear {seller.store_name} Team,\n\n"
        f"This is official confirmation that your merchant account on {brand_name} has been "
        f"formally and permanently CLOSED following complete operational and financial clearance.\n\n"
        f"Summary of Clearance:\n"
        f"- All customer orders, returns, and refunds are fully settled.\n"
        f"- All delivery cooling hold periods have concluded.\n"
        f"- Final balance and payout settlements have been reconciled.\n"
        f"- Products have been deactivated and unlisted from the storefront.\n\n"
        f"Historical Records & Portal Access:\n"
        f"Your seller account remains accessible in read-only mode. You may log in at any time "
        f"to inspect and download past orders, customer invoices, and financial reports for your tax records.\n\n"
        f"Administrator Audit Notes:\n"
        f"{notes or 'Account offboarding completed in accordance with marketplace terms.'}\n\n"
        f"Thank you for being part of {brand_name}.\n\n"
        f"Warm regards,\n"
        f"{brand_name} Finance & Marketplace Governance Team\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.send(fail_silently=False)
        logger.info(f"Seller closure notification email dispatched to {recipient_email} for seller #{seller.id}")
        return True
    except Exception as exc:
        logger.warning(
            f"Failed to dispatch seller closure notification email to {recipient_email} for seller #{seller.id}: {exc}"
        )
        return False
