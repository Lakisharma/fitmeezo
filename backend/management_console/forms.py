"""
Forms and Form Factories for Management Console.
Provides robust validated forms for staff login, dynamic model management,
dedicated product catalog creation/updating, and seller approvals.
"""

from decimal import Decimal
import os
from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError
from django.forms import modelform_factory
from django.utils.text import slugify

from core.models import (
    Category,
    Order,
    Product,
    ProductImage,
    ProductVariant,
    Seller,
)

User = get_user_model()


class ConsoleLoginForm(forms.Form):
    """
    Staff authentication form for the Management Console.
    Verifies credentials and ensures staff or superuser authorization.
    """
    username_or_email = forms.CharField(
        label="Staff Username or Email",
        widget=forms.TextInput(
            attrs={
                "class": "console-input",
                "placeholder": "admin@fitmeezo.in or username",
                "autocomplete": "username",
                "autofocus": True,
                "required": True,
                "id": "consoleUsername",
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "console-input",
                "placeholder": "Enter your secure password",
                "autocomplete": "current-password",
                "required": True,
                "id": "consolePassword",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        username_or_email = cleaned_data.get("username_or_email", "").strip()
        password = cleaned_data.get("password", "")

        if not username_or_email or not password:
            return cleaned_data

        # Authenticate via username or email
        user = authenticate(username=username_or_email, password=password)
        if user is None and "@" in username_or_email:
            try:
                user_obj = User.objects.get(email__iexact=username_or_email)
                user = authenticate(username=user_obj.username, password=password)
            except (User.DoesNotExist, User.MultipleObjectsReturned):
                user = None

        if user is None:
            raise ValidationError("Invalid login credentials. Please verify your username and password.")

        if not user.is_active:
            raise ValidationError("This account is inactive. Please contact system administrator.")

        if not (user.is_staff or user.is_superuser):
            raise ValidationError("Access denied. Management Console requires an active Staff or Administrator account.")

        self.user = user
        return cleaned_data


class DynamicModelForm(forms.ModelForm):
    """
    Base dynamic ModelForm that automatically applies modern console styling to all fields
    and respects read-only field restrictions.
    """
    def __init__(self, *args, readonly_fields=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.console_readonly = set(readonly_fields or [])

        for field_name, field in self.fields.items():
            # Check read-only
            if field_name in self.console_readonly:
                field.disabled = True
                field.required = False

            # Styling classes
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs["class"] = "console-checkbox"
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs["class"] = "console-select"
            elif isinstance(widget, forms.Textarea):
                widget.attrs["class"] = "console-textarea"
                widget.attrs.setdefault("rows", 4)
            elif isinstance(widget, forms.FileInput):
                widget.attrs["class"] = "console-file-input"
            else:
                widget.attrs["class"] = "console-input"


def build_model_form(model, model_admin, request, obj=None):
    """
    Builds a dynamically generated ModelForm for any registered model,
    excluding uneditable and sensitive fields (e.g. password hash).
    """
    readonly = []
    if hasattr(model_admin, "get_readonly_fields"):
        try:
            readonly = list(model_admin.get_readonly_fields(request, obj) or [])
        except Exception:
            readonly = list(getattr(model_admin, "readonly_fields", []) or [])

    # Exclude non-editable or sensitive fields
    exclude = ["id", "pk"]
    if model._meta.model_name == "user":
        exclude.extend(["password"])  # Never expose raw password hashes
    if model._meta.model_name == "sellerloginotp":
        exclude.extend(["otp_hash"])

    # Determine fields
    fields = "__all__"
    if hasattr(model_admin, "fields") and model_admin.fields:
        fields = [f for f in model_admin.fields if f not in exclude]

    FormClass = modelform_factory(
        model,
        form=DynamicModelForm,
        fields=fields,
        exclude=exclude if fields == "__all__" else None,
    )

    class ConfiguredModelForm(FormClass):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("readonly_fields", readonly)
            super().__init__(*args, **kwargs)

    return ConfiguredModelForm, readonly


class ConsoleProductForm(forms.ModelForm):
    """
    Dedicated enterprise form for adding and modifying products.
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
            "is_active",
            "is_featured",
            "is_approved",
            "approval_status",
            "rejection_reason",
            "is_returnable",
            "return_window_days",
            "weight_grams",
            "length_cm",
            "breadth_cm",
            "height_cm",
            "is_dimensions_verified",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "console-input", "placeholder": "Product title"}),
            "slug": forms.TextInput(attrs={"class": "console-input", "placeholder": "auto-generated if blank"}),
            "sku": forms.TextInput(attrs={"class": "console-input", "placeholder": "e.g. FTM-SHIRT-001"}),
            "category": forms.Select(attrs={"class": "console-select"}),
            "seller": forms.Select(attrs={"class": "console-select"}),
            "short_description": forms.Textarea(attrs={"class": "console-textarea", "rows": 2}),
            "description": forms.Textarea(attrs={"class": "console-textarea", "rows": 5}),
            "price": forms.NumberInput(attrs={"class": "console-input", "step": "0.01", "min": "0"}),
            "compare_at_price": forms.NumberInput(attrs={"class": "console-input", "step": "0.01", "min": "0"}),
            "image": forms.FileInput(attrs={"class": "console-file-input"}),
            "stock_quantity": forms.NumberInput(attrs={"class": "console-input", "min": "0"}),
            "is_active": forms.CheckboxInput(attrs={"class": "console-checkbox"}),
            "is_featured": forms.CheckboxInput(attrs={"class": "console-checkbox"}),
            "is_approved": forms.CheckboxInput(attrs={"class": "console-checkbox"}),
            "approval_status": forms.Select(attrs={"class": "console-select"}),
            "rejection_reason": forms.Textarea(attrs={"class": "console-textarea", "rows": 2}),
            "is_returnable": forms.CheckboxInput(attrs={"class": "console-checkbox"}),
            "return_window_days": forms.NumberInput(attrs={"class": "console-input", "min": "0"}),
            "weight_grams": forms.NumberInput(attrs={"class": "console-input", "min": "0"}),
            "length_cm": forms.NumberInput(attrs={"class": "console-input", "step": "0.1", "min": "0"}),
            "breadth_cm": forms.NumberInput(attrs={"class": "console-input", "step": "0.1", "min": "0"}),
            "height_cm": forms.NumberInput(attrs={"class": "console-input", "step": "0.1", "min": "0"}),
            "is_dimensions_verified": forms.CheckboxInput(attrs={"class": "console-checkbox"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        self.fields["rejection_reason"].required = False
        self.fields["short_description"].required = False

    def clean(self):
        cleaned_data = super().clean()
        name = cleaned_data.get("name")
        slug = cleaned_data.get("slug")
        price = cleaned_data.get("price")
        compare_at = cleaned_data.get("compare_at_price")

        # Auto slug
        if name and not slug:
            base_slug = slugify(name)
            candidate = base_slug
            counter = 1
            qs = Product.objects.all()
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            while qs.filter(slug=candidate).exists():
                candidate = f"{base_slug}-{counter}"
                counter += 1
            cleaned_data["slug"] = candidate

        # Pricing validation
        if price is not None and price < 0:
            self.add_error("price", "Price cannot be negative.")

        if price is not None and compare_at is not None and compare_at > 0:
            if compare_at < price:
                self.add_error("compare_at_price", "Compare at price must be greater than or equal to current price.")

        return cleaned_data


class ConsoleSellerApprovalForm(forms.ModelForm):
    """
    Dedicated form for reviewing and updating seller status and commission rates.
    """
    class Meta:
        model = Seller
        fields = [
            "status",
            "is_active",
            "commission_rate",
            "admin_notes",
        ]
        widgets = {
            "status": forms.Select(attrs={"class": "console-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "console-checkbox"}),
            "commission_rate": forms.NumberInput(attrs={"class": "console-input", "step": "0.01", "min": "0", "max": "100"}),
            "admin_notes": forms.Textarea(attrs={"class": "console-textarea", "rows": 3}),
        }

    def clean_commission_rate(self):
        rate = self.cleaned_data.get("commission_rate")
        if rate is not None and (rate < 0 or rate > 100):
            raise ValidationError("Commission rate must be between 0% and 100%.")
        return rate
