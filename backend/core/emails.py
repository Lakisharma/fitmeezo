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


def send_seller_new_order_email(seller_order) -> bool:
    """
    Sends a new-order notification email to the fulfilling seller for their assigned sub-order package.
    Fails safely so email transport errors never block order placement or checkout.
    """
    seller = getattr(seller_order, "seller", None)
    if not seller:
        return False

    recipient_email = (
        (seller.user.email if seller.user else "")
        or seller.support_email
        or ""
    ).strip()
    if not recipient_email:
        logger.info("Skipping seller new order notification: no valid email for seller #%s.", seller.pk)
        return False

    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", f"{brand_name} <support@fitmeezo.com>")
    subject = f"New Order Received: Package #{seller_order.sub_order_number} | {brand_name}"

    order = seller_order.order
    items_summary = "\n".join(
        f"  - {item.quantity}x {item.product_name} (SKU: {item.sku or 'N/A'}) — ₹{item.line_total}"
        for item in seller_order.items.all()
    ) or f"  - Package #{seller_order.sub_order_number} (Total: ₹{seller_order.subtotal})"

    body = (
        f"Dear {seller.store_name} Team,\n\n"
        f"You have received a new fulfillment package #{seller_order.sub_order_number} "
        f"under Customer Order #{order.order_number}.\n\n"
        f"Package Summary:\n"
        f"{items_summary}\n\n"
        f"Total Package Value: ₹{seller_order.subtotal}\n"
        f"Payment Method: {order.get_payment_method_display() if hasattr(order, 'get_payment_method_display') else order.payment_method}\n"
        f"Destination: {order.shipping_city}, {order.shipping_state} ({order.shipping_postal_code})\n\n"
        f"Next Action:\n"
        f"Please log in to your Merchant Dashboard to accept the order and verify packed parcel measurements.\n\n"
        f"Warm regards,\n"
        f"{brand_name} Logistics Operations\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.send(fail_silently=False)
        logger.info("Sent seller new order notification for package #%s to %s", seller_order.sub_order_number, recipient_email)
        return True
    except Exception as exc:
        logger.warning("Failed to send seller new order notification for package #%s: %s", seller_order.sub_order_number, exc)
        return False


def send_customer_shipment_created_email(seller_order) -> bool:
    """
    Sends a shipping dispatch notification to the customer once their package has been booked
    and an AWB tracking code has been issued by the carrier.
    """
    order = getattr(seller_order, "order", None)
    if not order or not order.user or not order.user.email:
        return False

    recipient_email = order.user.email.strip()
    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", f"{brand_name} <support@fitmeezo.com>")
    subject = f"Your Package is Ready for Dispatch! #{seller_order.sub_order_number} | {brand_name}"

    courier = seller_order.courier_name or "Courier Partner"
    awb = seller_order.tracking_number or seller_order.shiprocket_awb_code or "Pending"
    tracking_url = seller_order.tracking_url or f"https://fitmeezo.com/orders/track/?awb={awb}"

    body = (
        f"Dear {order.shipping_full_name},\n\n"
        f"Great news! Your package #{seller_order.sub_order_number} from order #{order.order_number} "
        f"has been packed and booked for shipment.\n\n"
        f"Logistics Details:\n"
        f"- Courier Partner: {courier}\n"
        f"- Tracking / AWB Number: {awb}\n"
        f"- Live Tracking: {tracking_url}\n\n"
        f"You can track the live delivery progress on {brand_name} at any time.\n\n"
        f"Thank you for shopping with us!\n"
        f"{brand_name} Customer Care\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.send(fail_silently=False)
        logger.info("Sent customer shipment created email for package #%s to %s", seller_order.sub_order_number, recipient_email)
        return True
    except Exception as exc:
        logger.warning("Failed to send customer shipment created email for package #%s: %s", seller_order.sub_order_number, exc)
        return False


def send_seller_pickup_scheduled_email(seller_order) -> bool:
    """
    Notifies the seller that courier warehouse pickup has been scheduled for their package.
    """
    seller = getattr(seller_order, "seller", None)
    if not seller:
        return False

    recipient_email = (
        (seller.user.email if seller.user else "")
        or seller.support_email
        or ""
    ).strip()
    if not recipient_email:
        return False

    booking = getattr(seller_order, "shipment_booking", None)
    pickup_date_str = str(booking.pickup_scheduled_date) if booking and booking.pickup_scheduled_date else "As scheduled by carrier"
    token = booking.pickup_token_number if booking else ""

    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", f"{brand_name} <support@fitmeezo.com>")
    subject = f"Courier Pickup Scheduled: Package #{seller_order.sub_order_number} | {brand_name}"

    body = (
        f"Dear {seller.store_name} Team,\n\n"
        f"Carrier pickup has been scheduled for package #{seller_order.sub_order_number}.\n\n"
        f"Pickup Details:\n"
        f"- Scheduled Window: {pickup_date_str}\n"
        f"- Courier Associate / Partner: {seller_order.courier_name or 'Courier'}\n"
        f"- AWB: {seller_order.tracking_number}\n"
        f"- Pickup Token / CRN: {token or 'Confirmed'}\n"
        f"- Origin Warehouse: {seller.pickup_address}, {seller.pickup_city} ({seller.pickup_postal_code})\n\n"
        f"Please ensure the parcel is packed, sealed, and labeled with the 4x6 shipping label "
        f"before the courier arrives.\n\n"
        f"{brand_name} Logistics Operations\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.send(fail_silently=False)
        logger.info("Sent seller pickup scheduled email for package #%s to %s", seller_order.sub_order_number, recipient_email)
        return True
    except Exception as exc:
        logger.warning("Failed to send seller pickup scheduled email for package #%s: %s", seller_order.sub_order_number, exc)
        return False


def send_customer_tracking_update_email(seller_order, current_status: str, location: str = "") -> bool:
    """
    Sends an update to customer when package is physically collected (Shipped) or Delivered.
    """
    order = getattr(seller_order, "order", None)
    if not order or not order.user or not order.user.email:
        return False

    recipient_email = order.user.email.strip()
    brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", f"{brand_name} <support@fitmeezo.com>")
    subject = f"Shipment Status Update: Package #{seller_order.sub_order_number} is {current_status.title()} | {brand_name}"

    loc_str = f" (Location: {location})" if location else ""
    body = (
        f"Dear {order.shipping_full_name},\n\n"
        f"Your package #{seller_order.sub_order_number} status has been updated: {current_status.upper()}{loc_str}.\n\n"
        f"- Courier: {seller_order.courier_name}\n"
        f"- Tracking Code: {seller_order.tracking_number}\n"
        f"- Live Tracking: {seller_order.tracking_url or 'https://fitmeezo.com/track-order/'}\n\n"
        f"Thank you for shopping with {brand_name}!\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient_email],
        )
        msg.send(fail_silently=False)
        return True
    except Exception as exc:
        logger.warning("Failed to send tracking update email for package #%s: %s", seller_order.sub_order_number, exc)
        return False

