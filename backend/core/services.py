"""
Fitmeezo Core Business Services.
Provides centralized, transactional business workflows for:
- Order Cancellation (Customer, Admin, and System)
- Atomic Inventory Restoration
- Refund State Accounting
"""

import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.db import transaction, IntegrityError
from django.db.models import F
from django.utils import timezone

from .models import Order, OrderItem, Product, ProductVariant, Seller, SellerOrder
from core.shipping_dimensions import calculate_package_bounding_dimensions

logger = logging.getLogger(__name__)

FREE_SHIPPING_THRESHOLD = Decimal("499.00")
DEFAULT_STANDARD_SHIPPING_FEE = Decimal("49.00")


def calculate_shipping_allocations(
    package_subtotals: list[Decimal],
    parent_shipping_fee: Decimal,
) -> list[Decimal]:
    """
    Allocates parent customer shipping fee across packages pro-rata based on
    each package's merchandise subtotal weight.

    Approved Business Rules:
    1. Uses Decimal arithmetic and rounds to 2 decimal places (ROUND_HALF_UP).
    2. Sum of allocated shares exactly equals parent_shipping_fee.
    3. The final package deterministically absorbs any penny rounding remainder.
    4. Handles zero shipping fee correctly (allocates Decimal('0.00') to all).
    5. If only 1 package, allocates the full parent_shipping_fee.
    6. If total merchandise subtotal is zero, allocates fee evenly with remainder
       to the final package.
    7. Allocation never exceeds parent_shipping_fee.
    8. Does not deduct catalogue MRP savings; subtotal reflects selling prices.
    """
    if not package_subtotals:
        return []

    parent_fee = Decimal(str(parent_shipping_fee or "0.00")).quantize(Decimal("0.01"))
    if parent_fee <= Decimal("0.00"):
        return [Decimal("0.00") for _ in package_subtotals]

    n = len(package_subtotals)
    if n == 1:
        return [parent_fee]

    dec_subtotals = [
        Decimal(str(sub or "0.00")).quantize(Decimal("0.01"))
        for sub in package_subtotals
    ]
    total_subtotal = sum(dec_subtotals)

    allocations = []
    accumulated = Decimal("0.00")

    if total_subtotal > Decimal("0.00"):
        for i in range(n - 1):
            sub = dec_subtotals[i]
            share = (sub * parent_fee / total_subtotal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            allocations.append(share)
            accumulated += share
        remainder = (parent_fee - accumulated).quantize(Decimal("0.01"))
        allocations.append(remainder)
    else:
        share_per_pkg = (parent_fee / Decimal(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for i in range(n - 1):
            allocations.append(share_per_pkg)
            accumulated += share_per_pkg
        remainder = (parent_fee - accumulated).quantize(Decimal("0.01"))
        allocations.append(remainder)

    return allocations


class CheckoutError(Exception):
    """Base exception for checkout processing failures."""
    pass


class CheckoutValidationError(CheckoutError):
    """Exception for item, stock, address, or merchant validation failures."""
    pass


def create_order_from_cart(
    user,
    cart,
    shipping_address,
    payment_method,
    customer_notes="",
    idempotency_token=None,
    standard_shipping_fee=None,
    request=None,
) -> tuple[Order, bool]:
    """
    Fitmeezo Transactional Multi-Seller Checkout Service.
    Guarantees:
    1. Exactly one parent Order per checkout.
    2. Exactly one SellerOrder package per distinct approved Seller.
    3. Exactly one SellerOrder package for platform-owned items (seller=None, is_platform_fulfilled=True).
    4. OrderItem records linked to both parent Order and appropriate SellerOrder.
    5. Commission snapshotting on OrderItem and SellerOrder using exact Decimal arithmetic.
    6. Atomic row-locking (select_for_update) on Product and ProductVariant to prevent overselling.
    7. Strict server-side validation: rejects inactive/unapproved products or inactive/unapproved merchants.
    8. Exact Decimal calculations server-side for subtotal, shipping fee, total amount.
    9. Cart clearance strictly upon success.
    10. Idempotency preservation: returns (existing_order, True) if idempotency_token matches an existing order.

    Returns:
        tuple[Order, bool]: (order, is_duplicate)
    Raises:
        CheckoutValidationError: when validation fails.
        CheckoutError: when underlying system or payment gateway initialization fails.
    """
    clean_idempotency_token = (idempotency_token or "").strip()
    if clean_idempotency_token:
        existing_order = Order.objects.filter(idempotency_key=clean_idempotency_token, user=user).first()
        if existing_order:
            return existing_order, True

    if cart.is_empty:
        raise CheckoutValidationError("Your shopping cart is empty. Please add items before checking out.")

    # Address snapshot resolution
    if hasattr(shipping_address, "full_name"):
        shipping_full_name = shipping_address.full_name
        shipping_phone_number = shipping_address.phone_number
        shipping_address_line1 = shipping_address.address_line1
        shipping_address_line2 = shipping_address.address_line2 or ""
        shipping_city = shipping_address.city
        shipping_state = shipping_address.state
        shipping_postal_code = shipping_address.postal_code
        shipping_landmark = getattr(shipping_address, "landmark", "") or ""
        shipping_address_type = getattr(shipping_address, "address_type", "home") or "home"
    elif isinstance(shipping_address, dict):
        shipping_full_name = shipping_address.get("full_name", "")
        shipping_phone_number = shipping_address.get("phone_number", "")
        shipping_address_line1 = shipping_address.get("address_line1", "")
        shipping_address_line2 = shipping_address.get("address_line2", "") or ""
        shipping_city = shipping_address.get("city", "")
        shipping_state = shipping_address.get("state", "")
        shipping_postal_code = shipping_address.get("postal_code", "")
        shipping_landmark = shipping_address.get("landmark", "") or ""
        shipping_address_type = shipping_address.get("address_type", "home") or "home"
    else:
        raise CheckoutValidationError("Please provide a valid delivery address.")

    if not shipping_full_name or not shipping_phone_number or not shipping_address_line1 or not shipping_city or not shipping_state or not shipping_postal_code:
        raise CheckoutValidationError("Incomplete delivery address. Please verify name, phone, address, city, state, and PIN code.")

    if standard_shipping_fee is None:
        standard_shipping_fee = DEFAULT_STANDARD_SHIPPING_FEE
    else:
        standard_shipping_fee = Decimal(str(standard_shipping_fee))

    try:
        with transaction.atomic():
            lines = cart.get_lines()
            if not lines:
                raise CheckoutValidationError("Your shopping cart is empty. Please add items before checking out.")

            verified_subtotal = Decimal("0.00")
            items_to_create = []

            for line in lines:
                p_id = line.product.id
                v_id = line.variant.id if line.variant else None
                qty = line.quantity

                if qty <= 0:
                    raise CheckoutValidationError(f"Invalid quantity for '{line.product.name}'.")

                # Acquire row-level lock on Product
                product = Product.objects.select_for_update().select_related("seller").filter(pk=p_id).first()
                if not product or not product.is_active:
                    raise CheckoutValidationError(
                        f"'{line.product.name}' is no longer active or available in catalogue. Please remove it from your cart."
                    )

                # Moderation approval validation
                if not product.is_approved or product.approval_status != Product.APPROVAL_APPROVED:
                    raise CheckoutValidationError(
                        f"'{product.name}' is pending moderation or not approved for purchase. Please remove it from your cart."
                    )

                # Merchant validation for third-party seller merchandise
                seller = product.seller
                if seller is not None:
                    if not seller.is_active or seller.status != Seller.STATUS_APPROVED:
                        raise CheckoutValidationError(
                            f"The merchant for '{product.name}' ({seller.store_name}) is currently unavailable or unapproved. Please remove it from your cart."
                        )

                variant = None
                if v_id:
                    # Acquire row-level lock on ProductVariant
                    variant = ProductVariant.objects.select_for_update().filter(pk=v_id, product=product).first()
                    if not variant or not variant.is_active:
                        raise CheckoutValidationError(
                            f"The selected option for '{product.name}' is inactive or no longer available."
                        )
                    if variant.stock_quantity < qty:
                        raise CheckoutValidationError(
                            f"Insufficient stock for '{product.name} ({variant.option_value})'. Only {variant.stock_quantity} left in inventory."
                        )

                    unit_price = Decimal(str(variant.effective_price))
                    comp_price = Decimal(str(variant.effective_compare_at_price)) if variant.effective_compare_at_price else None
                    sku = variant.sku
                    variant_label = f"{variant.option_type}: {variant.option_value}"
                else:
                    if product.stock_quantity < qty:
                        raise CheckoutValidationError(
                            f"Insufficient stock for '{product.name}'. Only {product.stock_quantity} left in inventory."
                        )

                    unit_price = Decimal(str(product.price))
                    comp_price = Decimal(str(product.compare_at_price)) if product.compare_at_price else None
                    sku = product.sku
                    variant_label = ""

                line_total = (unit_price * Decimal(qty)).quantize(Decimal("0.01"))
                verified_subtotal += line_total

                if variant:
                    w_grams = variant.effective_weight_grams
                    l_cm = variant.effective_length_cm
                    b_cm = variant.effective_breadth_cm
                    h_cm = variant.effective_height_cm
                    v_verified = variant.effective_is_dimensions_verified
                else:
                    w_grams = product.weight_grams
                    l_cm = product.length_cm
                    b_cm = product.breadth_cm
                    h_cm = product.height_cm
                    v_verified = product.is_dimensions_verified

                items_to_create.append({
                    "product": product,
                    "variant": variant,
                    "product_name": product.name,
                    "variant_label": variant_label,
                    "sku": sku,
                    "unit_price": unit_price,
                    "compare_at_price": comp_price,
                    "quantity": qty,
                    "line_total": line_total,
                    "seller": seller,
                    "weight_grams": w_grams,
                    "length_cm": l_cm,
                    "breadth_cm": b_cm,
                    "height_cm": h_cm,
                    "is_dimensions_verified": v_verified,
                })


            # Server-side calculation of shipping and final total
            server_shipping_fee = Decimal("0.00") if verified_subtotal >= FREE_SHIPPING_THRESHOLD else standard_shipping_fee
            server_total_amount = (verified_subtotal + server_shipping_fee).quantize(Decimal("0.01"))
            cart_discount = getattr(cart, "total_discount", Decimal("0.00")) or Decimal("0.00")

            # Unique order number generation
            date_str = timezone.now().strftime("%Y%m%d")
            rand_id = uuid.uuid4().hex[:6].upper()
            order_number = f"FM-{date_str}-{rand_id}"
            while Order.objects.filter(order_number=order_number).exists():
                rand_id = uuid.uuid4().hex[:6].upper()
                order_number = f"FM-{date_str}-{rand_id}"

            # Create immutable parent Order record
            order = Order.objects.create(
                order_number=order_number,
                user=user,
                shipping_full_name=shipping_full_name,
                shipping_phone_number=shipping_phone_number,
                shipping_address_line1=shipping_address_line1,
                shipping_address_line2=shipping_address_line2,
                shipping_city=shipping_city,
                shipping_state=shipping_state,
                shipping_postal_code=shipping_postal_code,
                shipping_landmark=shipping_landmark,
                shipping_address_type=shipping_address_type,
                subtotal=verified_subtotal,
                shipping_fee=server_shipping_fee,
                discount_amount=cart_discount,
                total_amount=server_total_amount,
                currency="INR",
                status=Order.STATUS_PENDING,
                payment_method=payment_method,
                payment_status=Order.PAYMENT_STATUS_UNPAID,
                idempotency_key=clean_idempotency_token or None,
                customer_notes=customer_notes or "",
            )

            # Group items by seller and snapshot commission
            seller_groups = {}
            for it in items_to_create:
                prod_seller = it["seller"]
                seller_key = prod_seller.id if prod_seller else None

                if seller_key not in seller_groups:
                    seller_groups[seller_key] = {
                        "seller": prod_seller,
                        "items": [],
                        "subtotal": Decimal("0.00"),
                        "total_commission": Decimal("0.00"),
                    }

                if prod_seller:
                    comm_rate = prod_seller.commission_rate
                    comm_amt = (it["line_total"] * comm_rate / Decimal("100.00")).quantize(Decimal("0.01"))
                else:
                    comm_rate = Decimal("0.00")
                    comm_amt = Decimal("0.00")

                it["commission_rate"] = comm_rate
                it["commission_amount"] = comm_amt

                seller_groups[seller_key]["items"].append(it)
                seller_groups[seller_key]["subtotal"] += it["line_total"]
                seller_groups[seller_key]["total_commission"] += comm_amt

            # Calculate pro-rata shipping fee allocation across all packages
            group_keys = list(seller_groups.keys())
            package_subtotals = [seller_groups[k]["subtotal"] for k in group_keys]
            allocated_shipping_fees = calculate_shipping_allocations(
                package_subtotals=package_subtotals,
                parent_shipping_fee=order.shipping_fee,
            )

            # Create distinct SellerOrder packages and child OrderItems
            for idx, seller_key in enumerate(group_keys):
                group = seller_groups[seller_key]
                prod_seller = group["seller"]
                is_platform = prod_seller is None
                if is_platform:
                    sub_order_num = f"{order.order_number}-PLT"
                else:
                    sub_order_num = f"{order.order_number}-S{prod_seller.id}"

                group_subtotal = group["subtotal"].quantize(Decimal("0.01"))
                group_commission = group["total_commission"].quantize(Decimal("0.01"))
                net_payable = (group_subtotal - group_commission).quantize(Decimal("0.01"))
                pkg_shipping_charge = allocated_shipping_fees[idx]

                # Compute package-level bounding dimensions from snapshot items (Tasks 2 & 5)
                pkg_dims = calculate_package_bounding_dimensions(group["items"])

                seller_order = SellerOrder(
                    order=order,
                    seller=prod_seller,
                    is_platform_fulfilled=is_platform,
                    sub_order_number=sub_order_num,
                    status=SellerOrder.STATUS_PENDING,
                    subtotal=group_subtotal,
                    total_commission=group_commission,
                    net_seller_payable=net_payable,
                    customer_shipping_charge=pkg_shipping_charge,
                    actual_shipping_cost=Decimal("0.00"),
                    shipping_allocated_to_seller=Decimal("0.00"),
                    shipping_margin=pkg_shipping_charge,
                    package_dimension_status=pkg_dims["status"],
                    estimated_weight_grams=pkg_dims["weight_grams"],
                    estimated_length_cm=pkg_dims["length_cm"],
                    estimated_breadth_cm=pkg_dims["breadth_cm"],
                    estimated_height_cm=pkg_dims["height_cm"],
                )
                seller_order.full_clean()
                seller_order.save()

                for it in group["items"]:
                    OrderItem.objects.create(
                        order=order,
                        seller_order=seller_order,
                        product=it["product"],
                        variant=it["variant"],
                        product_name=it["product_name"],
                        variant_label=it["variant_label"],
                        sku=it["sku"],
                        unit_price=it["unit_price"],
                        compare_at_price=it["compare_at_price"],
                        quantity=it["quantity"],
                        line_total=it["line_total"],
                        seller=it["seller"],
                        commission_rate=it["commission_rate"],
                        commission_amount=it["commission_amount"],
                        weight_grams=it["weight_grams"],
                        length_cm=it["length_cm"],
                        breadth_cm=it["breadth_cm"],
                        height_cm=it["height_cm"],
                        is_dimensions_verified=it["is_dimensions_verified"],
                    )


                    # Deduct inventory stock
                    if it["variant"]:
                        it["variant"].stock_quantity -= it["quantity"]
                        it["variant"].save(update_fields=["stock_quantity", "updated_at"])
                    else:
                        it["product"].stock_quantity -= it["quantity"]
                        it["product"].save(update_fields=["stock_quantity", "updated_at"])

            # If Razorpay payment method, initialize Razorpay order ID on server
            if payment_method == Order.PAYMENT_METHOD_RAZORPAY:
                try:
                    import razorpay
                    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
                    amount_in_paise = int((order.total_amount * 100).to_integral_value())
                    rp_order = client.order.create({
                        "amount": amount_in_paise,
                        "currency": getattr(settings, "RAZORPAY_CURRENCY", "INR"),
                        "receipt": order.order_number,
                        "notes": {
                            "order_number": order.order_number,
                            "user_id": str(user.id),
                            "user_email": getattr(user, "email", "") or "",
                        }
                    })
                    order.razorpay_order_id = rp_order.get("id", "")
                    order.save(update_fields=["razorpay_order_id"])
                except Exception as rp_err:
                    logger.error(f"Failed to create Razorpay order for #{order.order_number}: {rp_err}")
                    raise CheckoutError(f"Razorpay order initialization failed: {rp_err}")

            # Clear cart strictly after successful order creation
            cart.clear()

            # Record initial seller ledger entries (Phase 7)
            try:
                from core.ledger_services import post_order_sale_to_ledger
                if order.payment_method == Order.PAYMENT_METHOD_COD or order.payment_status == Order.PAYMENT_STATUS_PAID:
                    for so in order.seller_orders.all():
                        post_order_sale_to_ledger(so, actor=user)
            except Exception as ledg_err:
                logger.warning(f"Failed to post ledger entries for order {order.order_number}: {ledg_err}")

            return order, False


    except IntegrityError as ie:
        if clean_idempotency_token:
            existing = Order.objects.filter(idempotency_key=clean_idempotency_token, user=user).first()
            if existing:
                return existing, True
        logger.error(f"IntegrityError during checkout for user {user.id}: {ie}")
        raise CheckoutError("A database integrity error occurred while placing your order.")


def cancel_order(order, user=None, cancelled_by="customer", reason="") -> tuple[bool, str, Order | None]:
    """
    Safely and atomically cancels an order:
    1. Validates ownership: If `user` is provided and is not staff/superuser, verifies `order.user == user`.
    2. Validates cancellability:
       - Order must be in PENDING or PROCESSING status.
       - Disallowed if SHIPPED, DELIVERED, or already CANCELLED.
    3. Acquires row lock using `select_for_update()` inside `transaction.atomic()`.
    4. Restores inventory stock for each line item IF AND ONLY IF `order.inventory_restored` is False:
       - Uses F('stock_quantity') + item.quantity for variants or products.
       - Sets `order.inventory_restored = True` to prevent any duplicate stock restoration.
    5. Sets payment and refund status:
       - If order was PAID (e.g. Razorpay): `refund_status` becomes REFUND_STATUS_PENDING.
         `payment_status` remains PAID (we do NOT falsely mark it as refunded).
       - If order was UNPAID (e.g. COD): `refund_status` is REFUND_STATUS_NOT_APPLICABLE.
    6. Sets audit metadata:
       - `status = Order.STATUS_CANCELLED`
       - `cancelled_at = timezone.now()`
       - `cancelled_by = cancelled_by` ('customer' or 'staff')
       - `cancellation_reason = reason`
    7. Dispatches customer notification email safely without blocking transaction.

    Returns:
        tuple (success: bool, message: str, order: Order | None)
    """
    if not order:
        return False, "Order not found.", None

    order_pk = order.pk if isinstance(order, Order) else order

    try:
        with transaction.atomic():
            # Acquire database row lock on order to prevent concurrent race conditions
            locked_order = Order.objects.select_for_update().filter(pk=order_pk).first()
            if not locked_order:
                return False, "Order does not exist.", None

            # Ownership verification (customer access control)
            if user and not (user.is_staff or user.is_superuser):
                if locked_order.user_id != user.id:
                    return False, "You do not have permission to cancel this order.", locked_order

            # Guard against repeated cancellation
            if locked_order.status == Order.STATUS_CANCELLED:
                return False, f"Order #{locked_order.order_number} has already been cancelled.", locked_order

            # State machine verification: only Pending and Processing orders can be cancelled
            if locked_order.status not in [Order.STATUS_PENDING, Order.STATUS_PROCESSING]:
                status_name = locked_order.get_status_display()
                return False, f"Order #{locked_order.order_number} cannot be cancelled because it is already '{status_name}'.", locked_order

            # Guard against cancelling an order where any sub-order has already shipped or delivered
            has_shipped_or_delivered = locked_order.seller_orders.filter(
                status__in=[SellerOrder.STATUS_SHIPPED, SellerOrder.STATUS_DELIVERED]
            ).exists()
            if has_shipped_or_delivered:
                return (
                    False,
                    f"Order #{locked_order.order_number} cannot be cancelled because one or more packages have already been dispatched or delivered.",
                    locked_order,
                )

            # Atomic inventory restoration (guaranteed exactly-once via inventory_restored flag)
            if not locked_order.inventory_restored:
                for item in locked_order.items.select_related("product", "variant").all():
                    if item.variant_id:
                        ProductVariant.objects.filter(pk=item.variant_id).update(
                            stock_quantity=F("stock_quantity") + item.quantity
                        )
                    elif item.product_id:
                        Product.objects.filter(pk=item.product_id).update(
                            stock_quantity=F("stock_quantity") + item.quantity
                        )
                locked_order.inventory_restored = True

            # Refund accounting
            if locked_order.payment_status == Order.PAYMENT_STATUS_PAID:
                locked_order.refund_status = Order.REFUND_STATUS_PENDING
            else:
                locked_order.refund_status = Order.REFUND_STATUS_NOT_APPLICABLE

            # Record reason and timestamps
            clean_reason = (reason or "").strip()[:255]
            if not clean_reason:
                clean_reason = "Cancelled by customer" if cancelled_by == "customer" else "Cancelled by store staff"

            locked_order.status = Order.STATUS_CANCELLED
            locked_order.cancelled_at = timezone.now()
            locked_order.cancelled_by = cancelled_by
            locked_order.cancellation_reason = clean_reason

            locked_order.save(update_fields=[
                "status",
                "inventory_restored",
                "refund_status",
                "cancelled_at",
                "cancelled_by",
                "cancellation_reason",
                "updated_at",
            ])

            # Cascade cancellation to linked seller sub-orders (avoid cancelling delivered items)
            now_ts = timezone.now()
            cancelled_suborders = list(
                locked_order.seller_orders.filter(
                    status__in=[
                        SellerOrder.STATUS_PENDING,
                        SellerOrder.STATUS_PROCESSING,
                        SellerOrder.STATUS_READY_TO_SHIP,
                    ]
                )
            )
            locked_order.seller_orders.filter(
                id__in=[so.id for so in cancelled_suborders]
            ).update(
                status=SellerOrder.STATUS_CANCELLED,
                cancelled_at=now_ts,
                updated_at=now_ts,
            )

            # Record cancellation offsets in seller ledger (Phase 7)
            try:
                from core.ledger_services import post_cancellation_reversal_to_ledger
                for so in cancelled_suborders:
                    post_cancellation_reversal_to_ledger(so, actor=user, reason=clean_reason)
            except Exception as ledg_err:
                logger.warning(f"Failed to record cancellation in ledger for {locked_order.order_number}: {ledg_err}")

        # Safely attempt email dispatch outside the database transaction

        try:
            from .emails import send_order_cancellation_email
            send_order_cancellation_email(locked_order, reason=clean_reason, cancelled_by=cancelled_by)
        except Exception as email_exc:
            logger.warning(
                f"Failed to dispatch cancellation email for #{locked_order.order_number}: {email_exc}"
            )

        return True, f"Order #{locked_order.order_number} was successfully cancelled.", locked_order

    except Exception as exc:
        logger.error(f"Error cancelling order #{order_pk}: {exc}", exc_info=True)
        return False, f"Cancellation failed due to an unexpected error: {str(exc)}", None


def ensure_seller_orders_for_order(order) -> list:
    """
    Guarantees that every item in an Order is linked to a valid SellerOrder package.
    Automatically creates/backfills packages for legacy or platform-fulfilled orders:
    - Groups unlinked items by seller (or platform).
    - Synchronizes package status and delivered_at with the parent order.
    - Preserves existing seller orders and financial calculations.
    """
    unlinked_items = order.items.filter(seller_order__isnull=True).select_related("product__seller", "seller")
    if not unlinked_items.exists() and order.seller_orders.exists():
        return list(order.seller_orders.all())

    with transaction.atomic():
        # Group unlinked items by seller
        seller_groups = {}
        for item in unlinked_items:
            prod_seller = item.seller or (item.product.seller if item.product else None)
            seller_key = prod_seller.id if prod_seller else None

            if seller_key not in seller_groups:
                seller_groups[seller_key] = {
                    "seller": prod_seller,
                    "items": [],
                    "subtotal": Decimal("0.00"),
                }
            seller_groups[seller_key]["items"].append(item)
            seller_groups[seller_key]["subtotal"] += (item.unit_price * Decimal(str(item.quantity))).quantize(Decimal("0.01"))

        now_ts = timezone.now()
        so_status = (
            SellerOrder.STATUS_DELIVERED if order.status == Order.STATUS_DELIVERED else (
                SellerOrder.STATUS_SHIPPED if order.status == Order.STATUS_SHIPPED else (
                    SellerOrder.STATUS_CANCELLED if order.status == Order.STATUS_CANCELLED else SellerOrder.STATUS_PROCESSING
                )
            )
        )
        delivered_date = order.updated_at if order.status == Order.STATUS_DELIVERED else None

        for seller_key, group in seller_groups.items():
            prod_seller = group["seller"]
            is_platform = prod_seller is None
            if is_platform:
                sub_order_num = f"{order.order_number}-PLT"
            else:
                sub_order_num = f"{order.order_number}-S{prod_seller.id}"

            # Check if an existing SellerOrder already exists for this seller in this order
            existing_pkg = order.seller_orders.filter(
                seller=prod_seller,
                is_platform_fulfilled=is_platform,
            ).first()

            if not existing_pkg:
                existing_pkg = SellerOrder.objects.create(
                    order=order,
                    seller=prod_seller,
                    is_platform_fulfilled=is_platform,
                    sub_order_number=sub_order_num,
                    status=so_status,
                    subtotal=group["subtotal"],
                    total_commission=Decimal("0.00"),
                    net_seller_payable=group["subtotal"] if prod_seller else Decimal("0.00"),
                    delivered_at=delivered_date,
                    customer_shipping_charge=Decimal("0.00"),
                    actual_shipping_cost=Decimal("0.00"),
                    shipping_allocated_to_seller=Decimal("0.00"),
                    shipping_margin=Decimal("0.00"),
                )
            else:
                if so_status == SellerOrder.STATUS_DELIVERED and existing_pkg.status != SellerOrder.STATUS_DELIVERED:
                    existing_pkg.status = SellerOrder.STATUS_DELIVERED
                    existing_pkg.delivered_at = delivered_date or now_ts
                    existing_pkg.save(update_fields=["status", "delivered_at", "updated_at"])

            for item in group["items"]:
                item.seller_order = existing_pkg
                if prod_seller and not item.seller:
                    item.seller = prod_seller
                item.save(update_fields=["seller_order", "seller"])

    return list(order.seller_orders.all())

