"""
Fitmeezo Shopping Cart Service Layer.
Encapsulates all cart operations (add, remove, update, clear, totals,
pricing resolution, stock integrity, and warnings).

Supports:
- Anonymous visitors via Django sessions (storing minimal IDs and quantities).
- Authenticated users via persistent Cart & CartItem database models.
- Automatic migration of guest session cart upon user login.
- Real-time stock validation and price re-fetching from database.
- Decimal-based monetary calculations.
"""

from decimal import Decimal
from django.conf import settings
from django.db import transaction
from .models import Cart as CartModel, CartItem as CartItemModel, Product, ProductVariant

SESSION_CART_KEY = "fitmeezo_cart"


def make_cart_key(product_id, variant_id=None):
    """
    Constructs a deterministic unique string key for a product and optional variant.
    E.g. '12' (product only) or '12_45' (product + variant).
    """
    if variant_id:
        return f"{product_id}_{variant_id}"
    return str(product_id)


def parse_cart_key(key):
    """
    Parses cart key into (product_id, variant_id).
    """
    parts = str(key).split("_")
    if len(parts) >= 2:
        return int(parts[0]), int(parts[1])
    return int(parts[0]), None


class CartLine:
    """
    Data Transfer Object (DTO) representing a single verified line item in the cart.
    Holds fresh model references, effective prices, line subtotals, and status flags.
    """

    def __init__(self, key, product, variant, quantity):
        self.key = key
        self.product = product
        self.variant = variant
        self.quantity = int(quantity)

        # Integrity, approval, and stock validation
        self.is_product_active = product.is_active if product else False
        self.is_product_approved = (
            (getattr(product, "is_approved", False) or getattr(product, "approval_status", "") == "approved")
            and getattr(product, "approval_status", "approved") == "approved"
        ) if product else False
        seller = getattr(product, "seller", None) if product else None
        self.is_seller_valid = (seller.is_active and seller.status == "approved") if seller else True

        self.is_variant_active = variant.is_active if variant else (True if not variant else False)
        self.is_variant_valid = (variant.product_id == product.id) if (variant and product) else True

        # Determine available inventory
        if variant:
            self.available_stock = variant.stock_quantity
        elif product:
            self.available_stock = product.stock_quantity
        else:
            self.available_stock = 0

        # Calculate unit selling price and compare-at price
        if variant:
            self.unit_price = Decimal(str(variant.effective_price))
            comp_override = variant.effective_compare_at_price
            self.compare_at_price = Decimal(str(comp_override)) if comp_override else None
            self.sku = variant.sku
            self.variant_label = f"{variant.option_type}: {variant.option_value}"
        elif product:
            self.unit_price = Decimal(str(product.price))
            self.compare_at_price = Decimal(str(product.compare_at_price)) if product.compare_at_price else None
            self.sku = product.sku
            self.variant_label = ""
        else:
            self.unit_price = Decimal("0.00")
            self.compare_at_price = None
            self.sku = ""
            self.variant_label = ""

        # Line subtotals
        self.line_subtotal = (self.unit_price * Decimal(self.quantity)).quantize(Decimal("0.01"))

        if self.compare_at_price and self.compare_at_price > self.unit_price:
            self.line_compare_subtotal = (self.compare_at_price * Decimal(self.quantity)).quantize(Decimal("0.01"))
            self.line_discount = self.line_compare_subtotal - self.line_subtotal
            if self.line_compare_subtotal > Decimal("0.00"):
                self.discount_percent = round(((self.line_compare_subtotal - self.line_subtotal) / self.line_compare_subtotal) * 100)
            else:
                self.discount_percent = 0
        else:
            self.line_compare_subtotal = self.line_subtotal
            self.line_discount = Decimal("0.00")
            self.discount_percent = 0

        # Status and warnings
        self.is_available = True
        self.has_stock_warning = False
        self.warning_message = None

        if not self.is_product_active:
            self.is_available = False
            self.warning_message = f"'{product.name if product else 'Item'}' is currently unavailable."
        elif not self.is_product_approved:
            self.is_available = False
            self.warning_message = f"'{product.name if product else 'Item'}' is pending moderation or not approved."
        elif not self.is_seller_valid:
            self.is_available = False
            self.warning_message = f"The vendor for '{product.name if product else 'Item'}' is currently inactive or unapproved."
        elif self.variant and (not self.is_variant_active or not self.is_variant_valid):
            self.is_available = False
            self.warning_message = f"The selected option ({self.variant_label}) is no longer available."
        elif self.product and (getattr(self.product, "has_size_variants", False) or getattr(self.product, "has_variants", False)) and not self.variant:
            self.is_available = False
            self.warning_message = f"Please select an available size/option for '{self.product.name}'."
        elif self.available_stock <= 0:
            self.is_available = False
            self.warning_message = f"'{product.name}' is currently out of stock."
        elif self.quantity > self.available_stock:
            self.has_stock_warning = True
            unit_str = "unit" if self.available_stock == 1 else "units"
            self.warning_message = (
                f"Only {self.available_stock} {unit_str} in stock (you have {self.quantity} in cart). "
                "Please update your quantity to proceed."
            )

    @property
    def is_valid(self):
        return self.is_available and not self.has_stock_warning


class Cart:
    """
    Main Cart service coordinator.
    Handles session and persistent database operations interchangeably.
    """

    def __init__(self, request):
        self.request = request
        self.session = request.session
        self.user = getattr(request, "user", None)
        self.is_authenticated = bool(self.user and self.user.is_authenticated)

        if self.is_authenticated:
            # Authenticated user: persistent Cart in DB
            self.db_cart, _ = CartModel.objects.get_or_create(user=self.user)
            # If session has items from anonymous browsing prior to login, merge them
            if SESSION_CART_KEY in self.session and self.session[SESSION_CART_KEY]:
                self.merge_session_cart()
        else:
            self.db_cart = None
            # Anonymous visitor: Django session store
            if SESSION_CART_KEY not in self.session or not isinstance(self.session.get(SESSION_CART_KEY), dict):
                self.session[SESSION_CART_KEY] = {}

    def _get_session_dict(self):
        return self.session.setdefault(SESSION_CART_KEY, {})

    def _save_session(self):
        self.session.modified = True

    def merge_session_cart(self):
        """
        Merges guest session cart items into authenticated user's DB cart,
        handling stock ceilings and deduplication cleanly, then empties the session.
        """
        if not self.is_authenticated or not self.db_cart:
            return

        session_dict = self.session.get(SESSION_CART_KEY, {})
        if not session_dict:
            return

        with transaction.atomic():
            for key, data in session_dict.items():
                p_id = data.get("product_id")
                v_id = data.get("variant_id")
                qty = data.get("quantity", 1)

                try:
                    product = Product.objects.get(pk=p_id, is_active=True, is_approved=True)
                except Product.DoesNotExist:
                    continue

                variant = None
                if v_id:
                    try:
                        variant = ProductVariant.objects.get(pk=v_id, product=product, is_active=True)
                    except ProductVariant.DoesNotExist:
                        continue

                stock = variant.stock_quantity if variant else product.stock_quantity
                if stock <= 0:
                    continue

                item, created = CartItemModel.objects.get_or_create(
                    cart=self.db_cart,
                    product=product,
                    variant=variant,
                    defaults={"quantity": min(qty, stock)},
                )
                if not created:
                    new_qty = min(item.quantity + qty, stock)
                    item.quantity = max(1, new_qty)
                    item.save(update_fields=["quantity", "updated_at"])

            # Clear session cart once merged
            self.session[SESSION_CART_KEY] = {}
            self._save_session()

    def get_quantity(self, key):
        """Returns current quantity for a given cart key (or 0 if not in cart)."""
        if self.is_authenticated:
            p_id, v_id = parse_cart_key(key)
            item = CartItemModel.objects.filter(cart=self.db_cart, product_id=p_id, variant_id=v_id).first()
            return item.quantity if item else 0
        else:
            session_dict = self._get_session_dict()
            return session_dict.get(key, {}).get("quantity", 0)

    def add(self, product, variant=None, quantity=1, override_quantity=False):
        """
        Adds a product (and optional variant) to cart with full server validation.
        Returns: (success: bool, message: str, key: str)
        """
        # 1. Product must exist, be active, and be approved
        is_approved = (
            getattr(product, "is_approved", False)
            or getattr(product, "approval_status", "") == Product.APPROVAL_APPROVED
        ) and getattr(product, "approval_status", Product.APPROVAL_APPROVED) != Product.APPROVAL_REJECTED
        if not product or not product.is_active or not is_approved:
            return False, "This product is currently inactive, unavailable or pending review and cannot be added to cart.", None

        # 2. Variant validation
        if product.has_size_variants or product.has_variants:
            if not variant:
                return False, "Please select an available size/option before adding to cart.", None
            if not variant.is_active:
                return False, "The selected variant is inactive or unavailable.", None
            if variant.product_id != product.id:
                return False, "Invalid variant selected for this product.", None
        else:
            if variant is not None and (variant.product_id != product.id or not variant.is_active):
                return False, "Invalid variant option specified.", None

        # 3. Quantity validation
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            return False, "Please enter a valid positive quantity.", None

        if quantity < 1:
            return False, "Quantity must be at least 1 unit.", None

        # 4. Stock validation
        available_stock = variant.stock_quantity if variant else product.stock_quantity
        if available_stock <= 0:
            return False, f"'{product.name}' is currently out of stock.", None

        key = make_cart_key(product.id, variant.id if variant else None)
        current_qty = self.get_quantity(key)

        target_qty = quantity if override_quantity else (current_qty + quantity)

        if target_qty > available_stock:
            if current_qty > 0:
                return (
                    False,
                    f"Cannot add {quantity} more. You already have {current_qty} in your cart, and only {available_stock} are available.",
                    key,
                )
            return (
                False,
                f"Requested quantity ({quantity}) exceeds available stock ({available_stock}).",
                key,
            )

        # 5. Persist item
        if self.is_authenticated:
            with transaction.atomic():
                item, created = CartItemModel.objects.get_or_create(
                    cart=self.db_cart,
                    product=product,
                    variant=variant,
                    defaults={"quantity": target_qty},
                )
                if not created:
                    item.quantity = target_qty
                    item.save(update_fields=["quantity", "updated_at"])
                self.db_cart.save()  # update modified timestamp
        else:
            session_dict = self._get_session_dict()
            session_dict[key] = {
                "product_id": product.id,
                "variant_id": variant.id if variant else None,
                "quantity": target_qty,
            }
            self._save_session()

        var_text = f" ({variant.option_value})" if variant else ""
        return True, f"Added {quantity} × '{product.name}{var_text}' to your cart.", key

    def update_quantity(self, key, quantity):
        """
        Updates quantity for a specific cart line key.
        Returns: (success: bool, message: str)
        """
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            return False, "Please enter a valid quantity."

        if quantity < 1:
            return False, "Quantity must be at least 1. To remove this item, click the Remove button."

        p_id, v_id = parse_cart_key(key)

        try:
            product = Product.objects.get(pk=p_id)
        except Product.DoesNotExist:
            return False, "Product not found."

        variant = None
        if v_id:
            try:
                variant = ProductVariant.objects.get(pk=v_id, product_id=p_id)
            except ProductVariant.DoesNotExist:
                return False, "Variant not found."

        available_stock = variant.stock_quantity if variant else product.stock_quantity
        if quantity > available_stock:
            unit_str = "unit" if available_stock == 1 else "units"
            return False, f"Cannot update quantity to {quantity}. Only {available_stock} {unit_str} available in stock."

        if self.is_authenticated:
            item = CartItemModel.objects.filter(cart=self.db_cart, product_id=p_id, variant_id=v_id).first()
            if not item:
                return False, "Item not found in cart."
            item.quantity = quantity
            item.save(update_fields=["quantity", "updated_at"])
            self.db_cart.save()
        else:
            session_dict = self._get_session_dict()
            if key not in session_dict:
                return False, "Item not found in cart."
            session_dict[key]["quantity"] = quantity
            self._save_session()

        return True, "Cart quantity updated successfully."

    def remove(self, key):
        """
        Removes an item line from the cart.
        Returns: (success: bool, message: str)
        """
        p_id, v_id = parse_cart_key(key)

        if self.is_authenticated:
            deleted_count, _ = CartItemModel.objects.filter(
                cart=self.db_cart, product_id=p_id, variant_id=v_id
            ).delete()
            if deleted_count > 0:
                self.db_cart.save()
                return True, "Item removed from cart."
            return False, "Item was not found in cart."
        else:
            session_dict = self._get_session_dict()
            if key in session_dict:
                del session_dict[key]
                self._save_session()
                return True, "Item removed from cart."
            return False, "Item was not found in cart."

    def clear(self):
        """
        Clears all items from the cart.
        Returns: (success: bool, message: str)
        """
        if self.is_authenticated:
            CartItemModel.objects.filter(cart=self.db_cart).delete()
            self.db_cart.save()
        else:
            self.session[SESSION_CART_KEY] = {}
            self._save_session()

        return True, "Your shopping cart has been cleared."

    def get_lines(self):
        """
        Retrieves all cart lines re-fetched live from the database.
        Returns list of CartLine objects.
        """
        lines = []

        if self.is_authenticated:
            db_items = (
                CartItemModel.objects.filter(cart=self.db_cart)
                .select_related("product", "product__category", "variant")
                .order_by("created_at")
            )
            for item in db_items:
                key = make_cart_key(item.product_id, item.variant_id)
                lines.append(CartLine(key, item.product, item.variant, item.quantity))
        else:
            session_dict = self._get_session_dict()
            if not session_dict:
                return []

            # Gather all product and variant IDs for bulk querying
            product_ids = set()
            variant_ids = set()
            for key, data in session_dict.items():
                p_id = data.get("product_id")
                v_id = data.get("variant_id")
                if p_id:
                    product_ids.add(p_id)
                if v_id:
                    variant_ids.add(v_id)

            products_map = {
                p.id: p
                for p in Product.objects.filter(id__in=product_ids).select_related("category")
            }
            variants_map = {
                v.id: v
                for v in ProductVariant.objects.filter(id__in=variant_ids)
            }

            for key, data in list(session_dict.items()):
                p_id = data.get("product_id")
                v_id = data.get("variant_id")
                qty = data.get("quantity", 1)

                product = products_map.get(p_id)
                variant = variants_map.get(v_id) if v_id else None

                if not product:
                    # Clean up orphaned keys
                    continue

                lines.append(CartLine(key, product, variant, qty))

        return lines

    @property
    def lines(self):
        return self.get_lines()

    @property
    def total_units(self):
        """Total quantity of all items in cart."""
        return sum(line.quantity for line in self.get_lines())

    @property
    def subtotal(self):
        """Calculated selling price subtotal in INR (Decimal)."""
        return sum((line.line_subtotal for line in self.get_lines()), Decimal("0.00")).quantize(Decimal("0.01"))

    @property
    def original_subtotal(self):
        """Calculated compare-at/M.R.P. subtotal in INR (Decimal)."""
        return sum((line.line_compare_subtotal for line in self.get_lines()), Decimal("0.00")).quantize(Decimal("0.01"))

    @property
    def total_discount(self):
        """Total discount savings based on compare_at pricing rules in INR (Decimal)."""
        return sum((line.line_discount for line in self.get_lines()), Decimal("0.00")).quantize(Decimal("0.01"))

    @property
    def total_payable(self):
        """
        Total amount payable before shipping and taxes (Decimal).
        Shipping and taxes are calculated during checkout.
        """
        return self.subtotal

    @property
    def has_warnings(self):
        """True if any line item has stock or availability warnings."""
        return any(not line.is_available or line.has_stock_warning for line in self.get_lines())

    @property
    def is_empty(self):
        return len(self.get_lines()) == 0

    @property
    def is_checkout_ready(self):
        """True if cart has items and every item is in stock and available."""
        lines = self.get_lines()
        if not lines:
            return False
        return all(line.is_valid for line in lines)
