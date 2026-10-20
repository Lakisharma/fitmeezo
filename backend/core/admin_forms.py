"""
Fitmeezo Custom Admin Dashboard - Forms & Validators.
Provides secure forms for staff authentication, product and variant management,
category administration, and validated order workflows.
"""

from decimal import Decimal
import json
import os
from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify

from .models import (
    Category,
    Product,
    ProductImage,
    ProductVariant,
    Order,
    Seller,
    SellerOrder,
    CODReceiptRecord,
    PayoutBatch,
    SellerLedgerEntry,
)

User = get_user_model()

ALLOWED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def validate_image_file(file):
    """
    Validates uploaded image file size and extension.
    """
    if not file:
        return file

    ext = os.path.splitext(file.name)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(
            f"Unsupported image format ({ext}). Allowed formats are JPG, JPEG, PNG, and WebP."
        )

    if file.size > MAX_IMAGE_SIZE_BYTES:
        max_mb = MAX_IMAGE_SIZE_BYTES // (1024 * 1024)
        raise ValidationError(f"Image file size cannot exceed {max_mb} MB.")

    return file


class AdminLoginForm(forms.Form):
    """
    Dedicated staff login form.
    Only allows active users with staff or superuser permissions.
    """

    username_or_email = forms.CharField(
        label="Staff Username or Email",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "support@fitmeezo.com or username",
                "autocomplete": "username",
                "autofocus": True,
                "required": True,
                "id": "adminUsername",
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter your password",
                "autocomplete": "current-password",
                "required": True,
                "id": "adminPassword",
            }
        ),
    )
    remember_me = forms.BooleanField(
        required=False,
        label="Remember this session",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "id": "adminRememberMe"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        identifier = cleaned_data.get("username_or_email", "").strip()
        password = cleaned_data.get("password")

        if not identifier or not password:
            return cleaned_data

        user = authenticate(username=identifier, password=password)
        if user is None:
            raise ValidationError("Invalid username/email or password.")

        if not user.is_active:
            raise ValidationError("This administrator account has been deactivated.")

        if not (user.is_staff or user.is_superuser):
            raise ValidationError(
                "Access denied: This account does not possess staff or administrative permissions."
            )

        cleaned_data["user"] = user
        return cleaned_data


class AdminProductForm(forms.ModelForm):
    """
    Form for adding and editing catalogue products.
    """

    class Meta:
        model = Product
        fields = [
            "name",
            "slug",
            "sku",
            "category",
            "seller",
            "short_description",
            "description",
            "price",
            "compare_at_price",
            "image",
            "stock_quantity",
            "weight_grams",
            "length_cm",
            "breadth_cm",
            "height_cm",
            "is_dimensions_verified",
            "is_returnable",
            "return_window_days",
            "approval_status",
            "is_approved",
            "is_active",
            "is_featured",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. FitPulse Pro Smartwatch", "required": True}
            ),
            "slug": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Leave empty to auto-generate from title",
                }
            ),
            "sku": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. EL-SMW-001",
                    "style": "text-transform: uppercase;",
                    "required": True,
                }
            ),
            "category": forms.Select(attrs={"class": "form-select", "required": True}),
            "short_description": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Concise summary for search previews and cards"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Detailed product description, specifications, warranty, etc.",
                }
            ),
            "price": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "2499.00", "required": True}
            ),
            "compare_at_price": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "2999.00"}
            ),
            "image": forms.FileInput(attrs={"class": "form-control", "accept": "image/*"}),
            "stock_quantity": forms.NumberInput(
                attrs={"class": "form-control", "min": "0", "placeholder": "50", "required": True}
            ),
            "weight_grams": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Weight in grams"}
            ),
            "length_cm": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Length in cm"}
            ),
            "breadth_cm": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Breadth in cm"}
            ),
            "height_cm": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Height in cm"}
            ),
            "seller": forms.Select(attrs={"class": "form-select"}),
            "approval_status": forms.Select(attrs={"class": "form-select"}),
            "is_approved": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_returnable": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "return_window_days": forms.NumberInput(
                attrs={"class": "form-control", "min": "0", "placeholder": "7"}
            ),
            "is_dimensions_verified": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_featured": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    # approval_status has a model-level default ('approved') but Django's ModelForm
    # makes CharField-with-choices required. Override to make it optional so admin
    # forms submitted without this field (e.g. via API or minimal test payloads) still
    # validate correctly and the model default is applied automatically.
    approval_status = forms.ChoiceField(
        choices=[("", "— Use default (approved) —")] + Product.APPROVAL_CHOICES,
        required=False,
        initial=Product.APPROVAL_APPROVED,
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Administrative moderation lifecycle status. Defaults to 'approved'.",
    )

    # Size Variants Configuration
    has_size_variants = forms.BooleanField(
        required=False,
        label="Enable Size Variants",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "id": "id_has_size_variants"}),
        help_text="Enable if this product comes in different sizes (e.g. S, M, L, XL, XXL) with per-size stock.",
    )
    available_sizes = forms.CharField(
        required=False,
        label="Available Sizes",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "e.g. S, M, L, XL, XXL",
                "id": "id_available_sizes",
            }
        ),
        help_text="Enter sizes separated by commas, or click the quick-select pills.",
    )
    size_stock_data = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"id": "id_size_stock_data"}),
        help_text="JSON payload storing stock count for each size.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            if "has_size_variants" not in self.initial:
                self.initial["has_size_variants"] = self.instance.has_size_variants
            if "available_sizes" not in self.initial:
                self.initial["available_sizes"] = ", ".join(self.instance.available_sizes_list)
            if "size_stock_data" not in self.initial:
                active_variants = self.instance.variants.filter(option_type__iexact="Size", is_active=True)
                stock_dict = {v.option_value: v.stock_quantity for v in active_variants}
                self.initial["size_stock_data"] = json.dumps(stock_dict)

    def clean_approval_status(self):
        value = self.cleaned_data.get("approval_status", "")
        if not value:
            return Product.APPROVAL_APPROVED
        return value

    def clean_image(self):
        img = self.cleaned_data.get("image")
        if img and hasattr(img, "size"):
            validate_image_file(img)
        return img

    def clean_sku(self):
        sku = self.cleaned_data.get("sku", "").strip().upper()
        if not sku:
            raise ValidationError("SKU cannot be empty.")

        # Check collision with existing products
        qs = Product.objects.filter(sku__iexact=sku)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(f"A product with SKU '{sku}' already exists.")

        # Check collision with existing variants
        if ProductVariant.objects.filter(sku__iexact=sku).exists():
            raise ValidationError(f"SKU '{sku}' is already assigned to a product variant.")

        return sku

    def clean_slug(self):
        slug = self.cleaned_data.get("slug", "").strip().lower()
        name = self.cleaned_data.get("name", "").strip()
        if not slug and name:
            slug = slugify(name)
        elif slug:
            slug = slugify(slug)

        qs = Product.objects.filter(slug=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(f"A product with slug '{slug}' already exists. Please customize the slug.")
        return slug

    def clean(self):
        cleaned_data = super().clean()
        price = cleaned_data.get("price")
        compare = cleaned_data.get("compare_at_price")
        stock = cleaned_data.get("stock_quantity")

        if price is not None and price < Decimal("0.01"):
            self.add_error("price", "Selling price must be at least ₹0.01.")

        if stock is not None and stock < 0:
            self.add_error("stock_quantity", "Stock quantity cannot be negative.")

        if compare is not None and price is not None:
            if compare < price:
                self.add_error(
                    "compare_at_price",
                    "Original (Compare-at) price cannot be lower than the selling price.",
                )

        # Size variant processing
        has_size_variants = cleaned_data.get("has_size_variants", False)
        raw_sizes = cleaned_data.get("available_sizes", "")
        raw_stock_data = self.cleaned_data.get("size_stock_data") or self.data.get("size_stock_data", "")

        parsed_stock = {}
        if raw_stock_data:
            try:
                if isinstance(raw_stock_data, str) and raw_stock_data.strip():
                    parsed_stock = json.loads(raw_stock_data)
                elif isinstance(raw_stock_data, dict):
                    parsed_stock = raw_stock_data
            except Exception:
                pass

        if has_size_variants:
            if isinstance(raw_sizes, list):
                raw_list = raw_sizes
            else:
                raw_list = [s.strip() for s in str(raw_sizes).split(",") if s.strip()]

            cleaned_sizes = []
            seen_upper = set()
            for s in raw_list:
                clean_s = str(s).strip()
                if clean_s and clean_s.upper() not in seen_upper:
                    seen_upper.add(clean_s.upper())
                    cleaned_sizes.append(clean_s)

            if not cleaned_sizes:
                self.add_error(
                    "available_sizes",
                    "Please specify at least one size variant when size variants are enabled (e.g. S, M, L).",
                )

            stock_map = {}
            default_stock = stock if stock is not None else 0

            for size_val in cleaned_sizes:
                key_upper = size_val.upper()
                # Priority:
                # 1. individual POST input: size_stock_<size> or size_stock_<KEY>
                val_from_post = self.data.get(f"size_stock_{size_val}", self.data.get(f"size_stock_{key_upper}"))
                # 2. JSON stock data: parsed_stock[size_val] or parsed_stock[key_upper]
                val_from_json = parsed_stock.get(size_val, parsed_stock.get(key_upper))

                qty_val = val_from_post if val_from_post is not None else val_from_json
                if qty_val is None or str(qty_val).strip() == "":
                    qty = max(0, int(default_stock))
                else:
                    try:
                        qty = int(qty_val)
                        if qty < 0:
                            self.add_error(
                                "available_sizes",
                                f"Stock quantity for size '{size_val}' cannot be negative.",
                            )
                            qty = 0
                    except (ValueError, TypeError):
                        self.add_error(
                            "available_sizes",
                            f"Invalid stock quantity '{qty_val}' for size '{size_val}'. Must be an integer.",
                        )
                        qty = 0

                stock_map[size_val] = qty
                stock_map[key_upper] = qty

            cleaned_data["available_sizes_list"] = cleaned_sizes
            cleaned_data["size_stock_map"] = stock_map

            # If no errors, update total stock_quantity to the sum of variant stocks
            if cleaned_sizes and not self.errors:
                total_size_stock = sum(stock_map.get(s, 0) for s in cleaned_sizes)
                cleaned_data["stock_quantity"] = total_size_stock
        else:
            cleaned_data["available_sizes_list"] = []
            cleaned_data["size_stock_map"] = {}

        return cleaned_data

    @transaction.atomic
    def save(self, commit=True):
        instance = super().save(commit=commit)

        has_size_variants = self.cleaned_data.get("has_size_variants", False)
        sizes_list = self.cleaned_data.get("available_sizes_list", [])
        stock_map = self.cleaned_data.get("size_stock_map", {})

        def sync_sizes():
            if has_size_variants and sizes_list:
                instance.sync_size_variants(
                    sizes_list=sizes_list,
                    stock_map=stock_map,
                    default_stock=instance.stock_quantity,
                )
            else:
                # If size variants are disabled or emptied, safely deactivate any active size variants
                # without deleting records (protecting order historical integrity)
                if instance.variants.filter(option_type__iexact="Size", is_active=True).exists():
                    instance.sync_size_variants(sizes_list=[])

        if commit:
            sync_sizes()
        else:
            old_save_m2m = getattr(self, "save_m2m", None)
            def save_m2m():
                if old_save_m2m:
                    old_save_m2m()
                sync_sizes()
            self.save_m2m = save_m2m

        return instance


class AdminProductVariantForm(forms.ModelForm):
    """
    Form for adding and editing product variants.
    """

    class Meta:
        model = ProductVariant
        fields = [
            "option_type",
            "option_value",
            "sku",
            "price",
            "compare_at_price",
            "stock_quantity",
            "weight_grams",
            "length_cm",
            "breadth_cm",
            "height_cm",
            "is_dimensions_verified",
            "is_active",
        ]
        widgets = {
            "option_type": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. Size, Color, Storage", "required": True}
            ),
            "option_value": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. M, Black, 128GB", "required": True}
            ),
            "sku": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. EL-SMW-001-BLK",
                    "style": "text-transform: uppercase;",
                    "required": True,
                }
            ),
            "price": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0.00",
                    "placeholder": "Leave blank to inherit product price",
                }
            ),
            "compare_at_price": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0.00",
                    "placeholder": "Optional M.R.P. override",
                }
            ),
            "stock_quantity": forms.NumberInput(
                attrs={"class": "form-control", "min": "0", "placeholder": "25", "required": True}
            ),
            "weight_grams": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Weight in grams"}
            ),
            "length_cm": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Length in cm"}
            ),
            "breadth_cm": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Breadth in cm"}
            ),
            "height_cm": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0.01", "placeholder": "Height in cm"}
            ),
            "is_dimensions_verified": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


    def __init__(self, *args, product=None, **kwargs):
        self.product = product
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and not self.product:
            self.product = self.instance.product

    def clean_sku(self):
        sku = self.cleaned_data.get("sku", "").strip().upper()
        if not sku:
            raise ValidationError("Variant SKU is required.")

        # Collisions with Products
        if Product.objects.filter(sku__iexact=sku).exists():
            raise ValidationError(f"SKU '{sku}' is already in use by a product.")

        # Collisions with other variants
        qs = ProductVariant.objects.filter(sku__iexact=sku)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(f"SKU '{sku}' is already in use by another variant.")

        return sku

    def clean(self):
        cleaned_data = super().clean()
        opt_type = cleaned_data.get("option_type", "").strip()
        opt_val = cleaned_data.get("option_value", "").strip()
        price = cleaned_data.get("price")
        compare = cleaned_data.get("compare_at_price")
        stock = cleaned_data.get("stock_quantity")

        if stock is not None and stock < 0:
            self.add_error("stock_quantity", "Stock quantity cannot be negative.")

        if price is not None and price < Decimal("0.00"):
            self.add_error("price", "Price cannot be negative.")

        effective_price = price
        if effective_price is None and self.product:
            effective_price = self.product.price

        if compare is not None and effective_price is not None:
            if compare < effective_price:
                self.add_error(
                    "compare_at_price",
                    "Compare-at price cannot be lower than the effective selling price.",
                )

        # Check unique option_type + option_value for product
        if self.product and opt_type and opt_val:
            dup = ProductVariant.objects.filter(
                product=self.product,
                option_type__iexact=opt_type,
                option_value__iexact=opt_val,
            )
            if self.instance and self.instance.pk:
                dup = dup.exclude(pk=self.instance.pk)
            if dup.exists():
                raise ValidationError(
                    f"A variant with '{opt_type}: {opt_val}' already exists for this product."
                )

        return cleaned_data


class AdminProductImageForm(forms.ModelForm):
    """
    Form for adding gallery images to a product.
    """

    class Meta:
        model = ProductImage
        fields = ["image", "alt_text", "display_order"]
        widgets = {
            "image": forms.FileInput(attrs={"class": "form-control", "accept": "image/*", "required": True}),
            "alt_text": forms.TextInput(attrs={"class": "form-control", "placeholder": "Image description for accessibility"}),
            "display_order": forms.NumberInput(attrs={"class": "form-control", "min": "0", "value": "0"}),
        }

    def clean_image(self):
        img = self.cleaned_data.get("image")
        if img and hasattr(img, "size"):
            validate_image_file(img)
        return img


class AdminCategoryForm(forms.ModelForm):
    """
    Form for adding and editing categories.
    """

    class Meta:
        model = Category
        fields = ["name", "slug", "description", "image", "display_order", "is_active"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. Home & Living", "required": True}
            ),
            "slug": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Leave empty to auto-generate from name"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Brief category summary for SEO and navigation",
                }
            ),
            "image": forms.FileInput(attrs={"class": "form-control", "accept": "image/*"}),
            "display_order": forms.NumberInput(attrs={"class": "form-control", "min": "0", "value": "0"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_image(self):
        img = self.cleaned_data.get("image")
        if img and hasattr(img, "size"):
            validate_image_file(img)
        return img

    def clean_slug(self):
        slug = self.cleaned_data.get("slug", "").strip().lower()
        name = self.cleaned_data.get("name", "").strip()
        if not slug and name:
            slug = slugify(name)
        elif slug:
            slug = slugify(slug)

        qs = Category.objects.filter(slug=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(f"A category with slug '{slug}' already exists.")
        return slug

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise ValidationError("Category name is required.")
        qs = Category.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(f"A category named '{name}' already exists.")
        return name


class AdminOrderStatusForm(forms.Form):
    """
    Form for updating order fulfillment status with state machine constraints.
    """

    ALLOWED_TRANSITIONS = {
        Order.STATUS_PENDING: [Order.STATUS_PROCESSING, Order.STATUS_CANCELLED],
        Order.STATUS_PROCESSING: [Order.STATUS_SHIPPED, Order.STATUS_CANCELLED],
        Order.STATUS_SHIPPED: [Order.STATUS_DELIVERED],
        Order.STATUS_DELIVERED: [],  # Terminal
        Order.STATUS_CANCELLED: [],  # Terminal
    }

    status = forms.ChoiceField(
        choices=Order.STATUS_CHOICES,
        widget=forms.Select(attrs={"class": "form-select", "required": True}),
    )

    def __init__(self, *args, order=None, **kwargs):
        self.order = order
        super().__init__(*args, **kwargs)
        if self.order:
            self.fields["status"].initial = self.order.status

    def clean_status(self):
        new_status = self.cleaned_data.get("status")
        if not self.order:
            return new_status

        current_status = self.order.status
        if new_status == current_status:
            return new_status  # No change

        allowed = self.ALLOWED_TRANSITIONS.get(current_status, [])
        if new_status not in allowed:
            current_display = dict(Order.STATUS_CHOICES).get(current_status, current_status)
            target_display = dict(Order.STATUS_CHOICES).get(new_status, new_status)
            raise ValidationError(
                f"Invalid order status transition from '{current_display}' to '{target_display}'. "
                f"Permitted next steps: {', '.join([dict(Order.STATUS_CHOICES).get(s, s) for s in allowed]) or 'None (Terminal state)'}."
            )

        return new_status


class AdminOrderPaymentStatusForm(forms.Form):
    """
    Form for updating order payment status.
    Guarantees online payments cannot be marked Paid without gateway verification.
    """

    payment_status = forms.ChoiceField(
        choices=Order.PAYMENT_STATUS_CHOICES,
        widget=forms.Select(attrs={"class": "form-select", "required": True}),
    )

    def __init__(self, *args, order=None, **kwargs):
        self.order = order
        super().__init__(*args, **kwargs)
        if self.order:
            self.fields["payment_status"].initial = self.order.payment_status

    def clean_payment_status(self):
        new_status = self.cleaned_data.get("payment_status")
        if not self.order:
            return new_status

        if new_status == Order.PAYMENT_STATUS_PAID:
            if (
                self.order.payment_method == Order.PAYMENT_METHOD_RAZORPAY
                and not self.order.razorpay_payment_id
            ):
                raise ValidationError(
                    "Online (Razorpay) orders cannot be marked as Paid without a verified Razorpay Payment ID from the gateway."
                )

        return new_status


class AdminCODReconcileForm(forms.Form):
    """
    Form for platform administrators to reconcile courier Cash-on-Delivery cash remittance.
    """

    remittance_reference = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. CRN-20261011-BLUEDART-8821", "required": True}
        ),
        help_text="Courier remittance reference number or bank UTR.",
    )
    collected_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "required": True}),
        help_text="Actual cash amount collected and remitted to platform.",
    )
    courier_name = forms.CharField(
        max_length=80,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Blue Dart / Delhivery"}),
        help_text="Delivering courier agency.",
    )
    remitted_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        help_text="Date on which funds were remitted by courier partner.",
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"class": "form-control", "rows": 3, "placeholder": "Optional reconciliation notes or invoice numbers"}
        ),
    )


class AdminManualAdjustmentForm(forms.Form):
    """
    Form for platform administrators to record approved manual credit or debit adjustments.
    """

    seller = forms.ModelChoiceField(
        queryset=Seller.objects.filter(is_active=True).order_by("store_name"),
        required=False,
        empty_label="Platform Store (Internal Fulfillment)",
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Select recipient seller, or leave empty for Platform Store.",
    )
    entry_type = forms.ChoiceField(
        choices=SellerLedgerEntry.ENTRY_TYPE_CHOICES,
        widget=forms.Select(attrs={"class": "form-select", "required": True}),
        help_text="Credit increases payable to seller; Debit reduces payable.",
    )
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "required": True}),
        help_text="Positive decimal transaction magnitude in INR.",
    )
    reason = forms.CharField(
        widget=forms.Textarea(
            attrs={"class": "form-control", "rows": 3, "placeholder": "Mandatory audit explanation for this adjustment", "required": True}
        ),
        help_text="Mandatory business reason for this financial adjustment.",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("A clear, non-empty explanation reason is required for manual adjustments.")
        return reason


class AdminPayoutProcessForm(forms.Form):
    """
    Form for executing simulated mock payout batch settlement.
    """

    simulate_failure = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="Check to simulate an automated banking transfer failure for audit testing.",
    )
    failure_reason = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. Beneficiary IFSC lookup failed (simulation)"}
        ),
        help_text="Reason to record if simulating a failure.",
    )


class AdminPayoutReverseForm(forms.Form):
    """
    Form for reversing an already COMPLETED payout batch.
    """

    reason = forms.CharField(
        widget=forms.Textarea(
            attrs={"class": "form-control", "rows": 3, "placeholder": "Mandatory explanation for reversing this payout batch", "required": True}
        ),
        help_text="Auditable explanation for why this payout batch is being reversed.",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("A mandatory reason is required to reverse a payout batch.")
        return reason


class AdminPayoutHoldForm(forms.Form):
    """
    Form for placing a payout batch on hold.
    """

    reason = forms.CharField(
        widget=forms.Textarea(
            attrs={"class": "form-control", "rows": 3, "placeholder": "Mandatory explanation for placing payout on hold", "required": True}
        ),
        help_text="Auditable explanation for why this payout batch is being placed on hold.",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("A mandatory explanation reason is required to hold a payout batch.")
        return reason


class AdminPayoutRejectForm(forms.Form):
    """
    Form for rejecting a payout batch with an auditable explanation.
    """

    reason = forms.CharField(
        widget=forms.Textarea(
            attrs={"class": "form-control", "rows": 3, "placeholder": "Mandatory explanation for rejecting this payout", "required": True}
        ),
        help_text="Auditable explanation for why this payout batch is being rejected.",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("A mandatory reason is required to reject a payout batch.")
        return reason


class AdminSellerActionForm(forms.Form):
    """
    Form for administrative seller approval, rejection, suspension, and reactivation.
    """
    ACTION_APPROVE = "approve"
    ACTION_REJECT = "reject"
    ACTION_SUSPEND = "suspend"
    ACTION_REACTIVATE = "reactivate"

    ACTION_CHOICES = [
        (ACTION_APPROVE, "Approve Application"),
        (ACTION_REJECT, "Reject Application"),
        (ACTION_SUSPEND, "Suspend Store"),
        (ACTION_REACTIVATE, "Reactivate Store"),
    ]

    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.HiddenInput())
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Provide a reason or internal administrative note...",
            }
        ),
        help_text="Required when rejecting or suspending a seller account.",
    )

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get("action")
        reason = (cleaned_data.get("reason") or "").strip()
        if action in (self.ACTION_REJECT, self.ACTION_SUSPEND) and not reason:
            raise ValidationError("A mandatory reason must be provided when rejecting or suspending a seller.")
        return cleaned_data


class AdminSellerCommissionForm(forms.Form):
    """
    Form for updating a seller's custom marketplace commission rate.
    """
    commission_rate = forms.DecimalField(
        label="Platform Commission Rate (%)",
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.00"),
        max_value=Decimal("100.00"),
        widget=forms.NumberInput(
            attrs={"class": "form-control", "step": "0.10", "placeholder": "10.00", "required": True}
        ),
        help_text="Standard percentage retained by the marketplace from each fulfilled item sale.",
    )


class AdminInventoryUpdateForm(forms.Form):
    """
    Form for updating inventory stock levels safely with validation.
    """
    stock_quantity = forms.IntegerField(
        label="New Stock Quantity",
        min_value=0,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": "0", "placeholder": "100", "required": True}
        ),
        help_text="Must be 0 or greater. Negative inventory quantities are strictly prevented.",
    )
    reason = forms.CharField(
        label="Adjustment Reason / Reference",
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. Physical count reconciliation, restock shipment"}
        ),
    )


class AdminStaffUserForm(forms.Form):
    """
    Form for creating a new administrative staff member.
    """
    from .admin_permissions import ALL_ROLES

    ROLE_CHOICES = [(r, r) for r in ALL_ROLES]

    username = forms.CharField(
        label="Username",
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "admin_john", "required": True}),
    )
    email = forms.EmailField(
        label="Email Address",
        widget=forms.EmailInput(attrs={"class": "form-control", "placeholder": "john@fitmeezo.com", "required": True}),
    )
    first_name = forms.CharField(
        label="First Name",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "John"}),
    )
    last_name = forms.CharField(
        label="Last Name",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Doe"}),
    )
    password = forms.CharField(
        label="Temporary Password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "At least 8 characters", "required": True}),
    )
    role = forms.ChoiceField(
        label="Administrative Role",
        choices=ROLE_CHOICES,
        widget=forms.Select(attrs={"class": "form-select", "required": True}),
    )
    is_active = forms.BooleanField(
        label="Account Active",
        initial=True,
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_password(self):
        pwd = self.cleaned_data.get("password")
        if len(pwd) < 8:
            raise ValidationError("Password must contain at least 8 characters.")
        return pwd


class AdminUserRoleForm(forms.Form):
    """
    Form for updating an existing staff member's administrative role.
    """
    from .admin_permissions import ALL_ROLES

    ROLE_CHOICES = [(r, r) for r in ALL_ROLES]

    role = forms.ChoiceField(
        label="Administrative Role",
        choices=ROLE_CHOICES,
        widget=forms.Select(attrs={"class": "form-select", "required": True}),
    )
    is_active = forms.BooleanField(
        label="Account Active",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class AdminReportFilterForm(forms.Form):
    """
    Form for date-range filtering of analytical reports.
    """
    PRESET_CHOICES = [
        ("all", "All Time"),
        ("today", "Today"),
        ("7d", "Last 7 Days"),
        ("30d", "Last 30 Days"),
        ("this_month", "This Month"),
        ("custom", "Custom Date Range"),
    ]

    preset = forms.ChoiceField(
        choices=PRESET_CHOICES,
        required=False,
        initial="all",
        widget=forms.Select(attrs={"class": "form-select", "id": "reportPresetSelect"}),
    )
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )



