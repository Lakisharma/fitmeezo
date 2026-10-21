"""
Fitmeezo Customer Authentication, Profile, and Address Forms.
Provides robust server-side validation, password strength verification,
input normalization, and accessible Bootstrap widget attributes.
"""

from decimal import Decimal
import json
import logging
import os
import re
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .otp_services import normalize_mobile_number
from .models import (
    Address,
    Category,
    CustomerProfile,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    Seller,
    SellerOrder,
    ContactInquiry,
    GSTIN_REGEX,
    PAN_REGEX,
)

logger = logging.getLogger(__name__)

User = get_user_model()

ALLOWED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def validate_image_file(file):
    """Validates uploaded image format and file size limit."""
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


class CustomerRegistrationForm(forms.Form):
    """
    Customer account registration form.
    Captures full name, mobile number, email, and password with confirmation and policy acceptance.
    """

    full_name = forms.CharField(
        max_length=100,
        label="Full Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Enter your full name (e.g. Aarav Patel)",
                "autocomplete": "name",
                "required": True,
            }
        ),
    )
    mobile_number = forms.CharField(
        max_length=15,
        label="Mobile Number",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Enter 10-digit mobile number",
                "autocomplete": "tel",
                "required": True,
                "inputmode": "numeric",
            }
        ),
    )
    email = forms.EmailField(
        label="Email Address",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "name@example.com",
                "autocomplete": "email",
                "required": True,
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control fm-input fm-password-input",
                "placeholder": "Create a secure password (min. 8 characters)",
                "autocomplete": "new-password",
                "required": True,
            }
        ),
    )
    confirm_password = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control fm-input fm-password-input",
                "placeholder": "Re-enter your password",
                "autocomplete": "new-password",
                "required": True,
            }
        ),
    )
    terms = forms.BooleanField(
        required=True,
        label="I agree to the Fitmeezo Terms of Service & Privacy Policy",
        error_messages={"required": "You must accept the terms and conditions to register."},
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_mobile_number(self):
        raw = self.cleaned_data.get("mobile_number", "").strip()
        if not raw:
            raise ValidationError("Mobile number is required.")
        clean_mobile = normalize_mobile_number(raw)
        if not clean_mobile:
            raise ValidationError(
                "Please enter a valid 10-digit Indian mobile number (e.g. 9876543210 or +91 9876543210)."
            )
        existing = CustomerProfile.objects.filter(
            Q(mobile_number=clean_mobile) | Q(phone_number__endswith=clean_mobile)
        )
        if existing.exists():
            raise ValidationError("An account with this mobile number already exists. Please sign in.")
        return clean_mobile

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            raise ValidationError("Email address is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email address already exists. Please sign in.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("confirm_password")
        email = cleaned_data.get("email")

        if password and confirm and password != confirm:
            self.add_error("confirm_password", "Passwords do not match.")

        if password and email:
            # Validate password strength using Django validators
            temp_user = User(username=email, email=email)
            try:
                validate_password(password, user=temp_user)
            except ValidationError as err:
                self.add_error("password", err)

        return cleaned_data

    def save(self):
        email = self.cleaned_data["email"]
        full_name = self.cleaned_data["full_name"].strip()
        mobile_number = self.cleaned_data["mobile_number"]
        password = self.cleaned_data["password"]

        parts = full_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        # Use email as primary username; if taken, generate unique username
        username = email
        if User.objects.filter(username__iexact=username).exists():
            base_user = email.split("@")[0]
            counter = 1
            while User.objects.filter(username__iexact=f"{base_user}_{counter}").exists():
                counter += 1
            username = f"{base_user}_{counter}"

        with transaction.atomic():
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
            )

            profile, _ = CustomerProfile.objects.get_or_create(user=user)
            profile.mobile_number = mobile_number
            profile.phone_number = mobile_number
            profile.save(update_fields=["mobile_number", "phone_number"])

        return user


class CustomerPasswordResetForm(PasswordResetForm):
    """
    Enhanced password reset form allowing customers to enter either
    their registered email address OR their registered mobile number.
    If a mobile number is entered, locates the linked customer's email address
    and securely dispatches the reset link to that email.
    """
    email_or_mobile = forms.CharField(
        label="Mobile Number or Email Address",
        max_length=254,
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Enter registered mobile number or email",
                "autocomplete": "username",
                "required": True,
                "autofocus": True,
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "email" in self.fields:
            del self.fields["email"]

    def clean_email_or_mobile(self):
        val = self.cleaned_data.get("email_or_mobile", "").strip()
        if not val:
            raise ValidationError("Please enter your registered email address or mobile number.")
        return val

    def get_users(self, email_or_mobile):
        UserModel = get_user_model()
        val = str(email_or_mobile).strip()
        if "@" in val:
            clean_email = val.lower()
            active_users = UserModel._default_manager.filter(
                Q(email__iexact=clean_email) | Q(username__iexact=clean_email),
                is_active=True,
            )
            return [u for u in active_users if u.has_usable_password() and u.email]
        else:
            clean_mobile = normalize_mobile_number(val)
            if clean_mobile:
                active_users = UserModel._default_manager.filter(
                    Q(profile__mobile_number=clean_mobile)
                    | Q(profile__phone_number=clean_mobile)
                    | Q(profile__phone_number__endswith=clean_mobile)
                    | Q(seller_profile__registered_mobile=clean_mobile),
                    is_active=True,
                ).distinct()
                return [u for u in active_users if u.has_usable_password() and u.email]
        return []

    def save(
        self,
        domain_override=None,
        subject_template_name="account/password_reset_subject.txt",
        email_template_name="account/password_reset_email.html",
        use_https=False,
        token_generator=default_token_generator,
        from_email=None,
        request=None,
        html_email_template_name=None,
        extra_email_context=None,
    ):
        email_or_mobile = self.cleaned_data["email_or_mobile"]
        from_email = from_email or getattr(settings, "DEFAULT_FROM_EMAIL", None)

        if not domain_override and request:
            domain_override = request.get_host()
            use_https = request.is_secure()

        users = self.get_users(email_or_mobile)
        for user in users:
            current_site = get_current_site(request) if request else None
            site_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
            domain = domain_override or (current_site.domain if current_site else "fitmeezo.com")
            context = {
                "email": user.email,
                "domain": domain,
                "site_name": site_name,
                "uid": urlsafe_base64_encode(force_bytes(user.pk)),
                "user": user,
                "token": token_generator.make_token(user),
                "protocol": "https" if use_https or not getattr(settings, "DEBUG", True) else "http",
                **(extra_email_context or {}),
            }
            try:
                self.send_mail(
                    subject_template_name,
                    email_template_name,
                    context,
                    from_email,
                    user.email,
                    html_email_template_name=html_email_template_name,
                )
            except Exception as e:
                logger.exception(f"Failed to dispatch password reset email to {user.email}: {e}")


class CustomerLoginForm(forms.Form):
    """
    Customer storefront login form.
    Supports authentication via registered mobile number or email address with mandatory password.
    """

    mobile_or_email = forms.CharField(
        label="Mobile Number or Email Address",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Enter your registered mobile number or email",
                "autocomplete": "username",
                "required": True,
                "autofocus": True,
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control fm-input fm-password-input",
                "placeholder": "Enter your password",
                "autocomplete": "current-password",
                "required": True,
            }
        ),
    )
    remember_me = forms.BooleanField(
        required=False,
        label="Keep me signed in",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Backward compatibility for legacy callers/tests passing 'username_or_email'
        if self.data and "username_or_email" in self.data and "mobile_or_email" not in self.data:
            self.data = self.data.copy()
            self.data["mobile_or_email"] = self.data["username_or_email"]

    def clean_mobile_or_email(self):
        val = self.cleaned_data.get("mobile_or_email", "").strip()
        if not val:
            raise ValidationError("Please enter your registered mobile number or email address.")
        return val

    def clean(self):
        cleaned_data = super().clean()
        if "mobile_or_email" in cleaned_data:
            cleaned_data["username_or_email"] = cleaned_data["mobile_or_email"]
        return cleaned_data


class CustomerProfileForm(forms.Form):
    """
    Customer profile details update form.
    """

    full_name = forms.CharField(
        max_length=100,
        label="Full Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Full Name",
                "autocomplete": "name",
                "required": True,
            }
        ),
    )
    email = forms.EmailField(
        label="Email Address",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Email Address",
                "autocomplete": "email",
                "required": True,
            }
        ),
    )
    phone_number = forms.CharField(
        max_length=20,
        required=False,
        label="Phone Number",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. +91 9876543210 (Optional)",
                "autocomplete": "tel",
            }
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if self.user:
            profile = getattr(self.user, "profile", None)
            name = f"{self.user.first_name} {self.user.last_name}".strip()
            self.fields["full_name"].initial = name or self.user.username
            self.fields["email"].initial = self.user.email
            if profile:
                self.fields["phone_number"].initial = profile.phone_number

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            raise ValidationError("Email address cannot be empty.")
        if self.user:
            duplicate = User.objects.filter(email__iexact=email).exclude(pk=self.user.pk)
            if duplicate.exists():
                raise ValidationError("This email address is already in use by another account.")
        return email

    def clean_phone_number(self):
        phone = self.cleaned_data.get("phone_number", "").strip()
        if not phone:
            return ""
        clean_mobile = normalize_mobile_number(phone)
        if not clean_mobile:
            raise ValidationError(
                "Please enter a valid 10-digit Indian mobile number (e.g. +91 9876543210 or 9876543210)."
            )
        existing = CustomerProfile.objects.filter(
            Q(mobile_number=clean_mobile) | Q(phone_number__endswith=clean_mobile)
        )
        if self.user:
            existing = existing.exclude(user=self.user)
        if existing.exists():
            raise ValidationError("This mobile number is already registered to another account.")
        return phone

    def save(self):
        if not self.user:
            return None

        email = self.cleaned_data["email"]
        full_name = self.cleaned_data["full_name"].strip()
        phone = self.cleaned_data.get("phone_number", "").strip()

        parts = full_name.split(" ", 1)
        self.user.first_name = parts[0]
        self.user.last_name = parts[1] if len(parts) > 1 else ""
        self.user.email = email
        self.user.save(update_fields=["first_name", "last_name", "email"])

        profile, _ = CustomerProfile.objects.get_or_create(user=self.user)
        profile.phone_number = phone
        profile.save(update_fields=["phone_number", "updated_at"])

        return self.user


class AddressForm(forms.ModelForm):
    """
    Customer delivery address management form.
    """

    class Meta:
        model = Address
        fields = [
            "full_name",
            "phone_number",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "landmark",
            "address_type",
            "is_default",
        ]
        widgets = {
            "full_name": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "Recipient Full Name"}
            ),
            "phone_number": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "10-digit mobile number"}
            ),
            "address_line1": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "House No., Flat, Building, Street",
                }
            ),
            "address_line2": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "Area, Sector, Locality (Optional)"}
            ),
            "city": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "City / Town"}
            ),
            "state": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "State / Province"}
            ),
            "postal_code": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "6-digit PIN code",
                }
            ),
            "landmark": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "E.g. Near Apollo Pharmacy (Optional)"}
            ),
            "address_type": forms.Select(attrs={"class": "form-select fm-input"}),
            "is_default": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        labels = {
            "full_name": "Full Name",
            "phone_number": "Contact Phone Number",
            "address_line1": "Address Line 1",
            "address_line2": "Address Line 2 (Optional)",
            "city": "City / District",
            "state": "State",
            "postal_code": "PIN / Postal Code",
            "landmark": "Landmark (Optional)",
            "address_type": "Address Type",
            "is_default": "Make this my default delivery address",
        }


class ProductReviewForm(forms.ModelForm):
    """
    Customer Product Review and Rating form.
    Captures rating (1-5), review title, and detailed feedback comments.
    """

    rating = forms.IntegerField(
        min_value=1,
        max_value=5,
        required=True,
        error_messages={
            "required": "Please select a star rating from 1 to 5.",
            "min_value": "Rating must be at least 1 star.",
            "max_value": "Rating cannot exceed 5 stars.",
        },
        widget=forms.HiddenInput(attrs={"id": "fmReviewRatingInput"}),
    )
    title = forms.CharField(
        max_length=200,
        required=True,
        label="Review Headline",
        error_messages={
            "required": "Please provide a headline for your review.",
            "max_length": "Review headline cannot exceed 200 characters.",
        },
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Summarize your experience (e.g. Excellent build quality & battery life!)",
                "maxlength": "200",
                "id": "fmReviewTitle",
                "required": True,
            }
        ),
    )
    comment = forms.CharField(
        required=True,
        label="Detailed Review",
        error_messages={
            "required": "Please share your detailed feedback in the review comments.",
        },
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 4,
                "placeholder": "What did you like or dislike? How does this product perform in everyday use?",
                "id": "fmReviewComment",
                "required": True,
            }
        ),
    )

    class Meta:
        model = ProductReview
        fields = ["rating", "title", "comment"]

    def clean_title(self):
        title = self.cleaned_data.get("title", "").strip()
        if not title:
            raise ValidationError("Please provide a headline for your review.")
        return title

    def clean_comment(self):
        comment = self.cleaned_data.get("comment", "").strip()
        if not comment:
            raise ValidationError("Please share your detailed feedback in the review comments.")
        return comment


class SellerRegistrationForm(forms.ModelForm):
    """
    Onboarding registration form for new third-party marketplace sellers.
    Captures store branding, legal identity, business address, pickup facility,
    and Indian tax identifiers (GSTIN/PAN).
    Administrative approval fields (status, commission, notes) are strictly excluded.
    """

    gstin = forms.CharField(
        max_length=15,
        min_length=15,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input text-uppercase",
                "placeholder": "15-digit GSTIN (e.g. 29ABCDE1234F1Z5)",
                "maxlength": "15",
                "required": True,
            }
        ),
        error_messages={
            "required": "GSTIN is mandatory for seller onboarding.",
            "min_length": "Invalid Indian GSTIN format. Expected 15 characters (e.g. 29ABCDE1234F1Z5).",
            "max_length": "Invalid Indian GSTIN format. Expected 15 characters (e.g. 29ABCDE1234F1Z5).",
        },
    )

    class Meta:
        model = Seller
        fields = [
            "store_name",
            "legal_business_name",
            "business_type",
            "store_description",
            "support_email",
            "support_phone",
            "registered_address",
            "city",
            "state",
            "postal_code",
            "pickup_address",
            "pickup_city",
            "pickup_state",
            "pickup_postal_code",
            "gstin",
            "pan_number",
        ]
        widgets = {
            "store_name": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "e.g. Apex Tech Store",
                    "autocomplete": "organization",
                    "required": True,
                }
            ),
            "legal_business_name": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "Registered legal entity name",
                    "required": True,
                }
            ),
            "business_type": forms.Select(
                attrs={
                    "class": "form-select fm-input",
                    "required": True,
                }
            ),
            "store_description": forms.Textarea(
                attrs={
                    "class": "form-control fm-input",
                    "rows": 3,
                    "placeholder": "Describe your store, merchandise specialization, and customer promise...",
                }
            ),
            "support_email": forms.EmailInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "support@yourstore.in",
                    "required": True,
                }
            ),
            "support_phone": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "+91 9876543210",
                    "required": True,
                }
            ),
            "registered_address": forms.Textarea(
                attrs={
                    "class": "form-control fm-input",
                    "rows": 2,
                    "placeholder": "Official registered business address",
                    "required": True,
                }
            ),
            "city": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "e.g. Bengaluru",
                    "required": True,
                }
            ),
            "state": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "e.g. Karnataka",
                    "required": True,
                }
            ),
            "postal_code": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "6-digit PIN code",
                    "maxlength": "10",
                    "required": True,
                }
            ),
            "pickup_address": forms.Textarea(
                attrs={
                    "class": "form-control fm-input",
                    "rows": 2,
                    "placeholder": "Warehouse address where couriers collect shipments",
                    "required": True,
                }
            ),
            "pickup_city": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "e.g. Bengaluru",
                    "required": True,
                }
            ),
            "pickup_state": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "e.g. Karnataka",
                    "required": True,
                }
            ),
            "pickup_postal_code": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "6-digit PIN code",
                    "maxlength": "10",
                    "required": True,
                }
            ),
            "pan_number": forms.TextInput(
                attrs={
                    "class": "form-control fm-input text-uppercase",
                    "placeholder": "10-digit PAN (e.g. ABCDE1234F)",
                    "maxlength": "10",
                }
            ),
        }

    def clean_store_name(self):
        store_name = self.cleaned_data.get("store_name", "").strip()
        if not store_name:
            raise ValidationError("Store name is required.")
        qs = Seller.objects.filter(store_name__iexact=store_name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("A seller with this store name is already registered.")
        return store_name

    def clean_gstin(self):
        gstin = self.cleaned_data.get("gstin", "").strip().upper()
        if not gstin:
            raise ValidationError("GSTIN is mandatory for seller onboarding.")
        if not re.match(GSTIN_REGEX, gstin):
            raise ValidationError("Invalid Indian GSTIN format. Expected 15 characters (e.g. 29ABCDE1234F1Z5).")
        qs = Seller.objects.filter(gstin__iexact=gstin)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("A seller with this GSTIN is already registered on the platform.")
        return gstin

    def clean_support_phone(self):
        raw = self.cleaned_data.get("support_phone", "").strip()
        if not raw:
            raise ValidationError("Customer support phone is required.")
        digits = re.sub(r"[^\d]", "", raw)
        if len(digits) < 10:
            raise ValidationError("Please enter a valid support phone number with at least 10 digits.")
        return raw.strip()

    def clean_postal_code(self):
        pc = self.cleaned_data.get("postal_code", "").strip()
        if not re.match(r"^\d{6}$", pc):
            raise ValidationError("Please enter a valid 6-digit PIN code.")
        return pc

    def clean_pickup_postal_code(self):
        ppc = self.cleaned_data.get("pickup_postal_code", "").strip()
        if ppc and not re.match(r"^\d{6}$", ppc):
            raise ValidationError("Please enter a valid 6-digit pickup PIN code.")
        return ppc

    def clean(self):
        cleaned_data = super().clean()
        gstin = cleaned_data.get("gstin")
        pan = cleaned_data.get("pan_number", "").strip().upper() if cleaned_data.get("pan_number") else ""

        if gstin and not pan:
            # Auto-extract PAN from valid GSTIN
            cleaned_data["pan_number"] = gstin[2:12]
        elif gstin and pan:
            pan_in_gstin = gstin[2:12]
            if pan != pan_in_gstin:
                self.add_error("pan_number", f"PAN ({pan}) does not match the PAN encoded in GSTIN ({pan_in_gstin}).")

        return cleaned_data


class PublicSellerRegistrationForm(forms.Form):
    """
    Public self-service seller registration form.
    Collects merchant login credentials along with complete business identity,
    addresses, and Indian tax compliance data in a single transactional onboarding step.
    Password hashing is strictly delegated to Django's authentication system.
    Created seller profiles are initialized with STATUS_PENDING awaiting admin approval.
    """

    # --- Account Credentials ---
    full_name = forms.CharField(
        max_length=150,
        required=True,
        label="Proprietor / Contact Person Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. Rajesh Sharma",
                "autocomplete": "name",
                "required": True,
            }
        ),
    )
    email = forms.EmailField(
        max_length=254,
        required=True,
        label="Account Email Address",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "merchant@business.in",
                "autocomplete": "email",
                "required": True,
            }
        ),
    )
    mobile = forms.CharField(
        max_length=15,
        required=True,
        label="Mobile Number",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "10-digit mobile (e.g. 9876543210)",
                "autocomplete": "tel",
                "required": True,
            }
        ),
    )
    username = forms.CharField(
        max_length=150,
        required=True,
        label="Merchant Username",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Choose unique username",
                "autocomplete": "username",
                "required": True,
            }
        ),
    )
    password = forms.CharField(
        required=True,
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Minimum 8 characters",
                "autocomplete": "new-password",
                "required": True,
            }
        ),
    )
    confirm_password = forms.CharField(
        required=True,
        label="Confirm Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Re-enter password",
                "autocomplete": "new-password",
                "required": True,
            }
        ),
    )

    # --- Store Identity & Legal Entity ---
    store_name = forms.CharField(
        max_length=200,
        required=True,
        label="Store Display Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. Apex Apparel Store",
                "required": True,
            }
        ),
    )
    legal_business_name = forms.CharField(
        max_length=255,
        required=True,
        label="Legal Business Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Registered entity name as on GST certificate",
                "required": True,
            }
        ),
    )
    business_type = forms.ChoiceField(
        choices=Seller.BUSINESS_TYPE_CHOICES,
        required=True,
        label="Business Entity Type",
        widget=forms.Select(attrs={"class": "form-select fm-input", "required": True}),
    )
    store_description = forms.CharField(
        required=False,
        label="Store Description",
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 3,
                "placeholder": "Describe your store, merchandise specialization, and customer promise...",
            }
        ),
    )
    support_email = forms.EmailField(
        required=True,
        label="Customer Support Email",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "support@yourstore.in",
                "required": True,
            }
        ),
    )
    support_phone = forms.CharField(
        max_length=20,
        required=True,
        label="Customer Support Phone",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "+91 9876543210",
                "required": True,
            }
        ),
    )

    # --- Registered Business Address ---
    registered_address = forms.CharField(
        required=True,
        label="Registered Business Address",
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 2,
                "placeholder": "Official registered business address",
                "required": True,
            }
        ),
    )
    city = forms.CharField(
        max_length=100,
        required=True,
        label="City",
        widget=forms.TextInput(attrs={"class": "form-control fm-input", "placeholder": "e.g. Bengaluru", "required": True}),
    )
    state = forms.CharField(
        max_length=100,
        required=True,
        label="State",
        widget=forms.TextInput(attrs={"class": "form-control fm-input", "placeholder": "e.g. Karnataka", "required": True}),
    )
    postal_code = forms.CharField(
        max_length=10,
        required=True,
        label="PIN Code",
        widget=forms.TextInput(attrs={"class": "form-control fm-input", "placeholder": "6-digit PIN code", "required": True}),
    )

    # --- Courier Dispatch & Pickup Location ---
    pickup_address = forms.CharField(
        required=False,
        label="Pickup Address",
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 2,
                "placeholder": "Warehouse address where couriers collect shipments (Leave blank to use registered address)",
            }
        ),
    )
    pickup_city = forms.CharField(
        max_length=100,
        required=False,
        label="Pickup City",
        widget=forms.TextInput(attrs={"class": "form-control fm-input", "placeholder": "e.g. Bengaluru"}),
    )
    pickup_state = forms.CharField(
        max_length=100,
        required=False,
        label="Pickup State",
        widget=forms.TextInput(attrs={"class": "form-control fm-input", "placeholder": "e.g. Karnataka"}),
    )
    pickup_postal_code = forms.CharField(
        max_length=10,
        required=False,
        label="Pickup PIN Code",
        widget=forms.TextInput(attrs={"class": "form-control fm-input", "placeholder": "6-digit PIN code"}),
    )

    # --- Indian Tax Identifiers ---
    gstin = forms.CharField(
        max_length=15,
        min_length=15,
        required=True,
        label="GSTIN",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input text-uppercase",
                "placeholder": "15-digit GSTIN (e.g. 29ABCDE1234F1Z5)",
                "maxlength": "15",
                "required": True,
            }
        ),
    )
    pan_number = forms.CharField(
        max_length=10,
        min_length=10,
        required=False,
        label="PAN (Permanent Account Number)",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input text-uppercase",
                "placeholder": "10-digit PAN (e.g. ABCDE1234F)",
                "maxlength": "10",
            }
        ),
    )
    terms = forms.BooleanField(
        required=True,
        label="I agree to the Fitmeezo Marketplace Seller Agreement & Terms of Service",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        error_messages={"required": "You must accept the seller terms and policies to proceed."},
    )

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        if not username:
            raise ValidationError("Username is required.")
        if not re.match(r"^[\w.@+-]+$", username):
            raise ValidationError("Username may contain letters, digits, and @/./+/-/_ only.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("This username is already taken. Please choose another username.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            raise ValidationError("Email address is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email address already exists. Please sign in.")
        try:
            from allauth.account.models import EmailAddress
            if EmailAddress.objects.filter(email__iexact=email).exists():
                raise ValidationError("An account with this email address already exists. Please sign in.")
        except Exception:
            pass
        return email

    def clean_mobile(self):
        raw = self.cleaned_data.get("mobile", "").strip()
        normalized = normalize_mobile_number(raw)
        if not normalized:
            raise ValidationError("Please enter a valid 10-digit Indian mobile number starting with 6, 7, 8, or 9.")
        if CustomerProfile.objects.filter(mobile_number=normalized).exists() or CustomerProfile.objects.filter(phone_number__endswith=normalized).exists():
            raise ValidationError("An account with this mobile number already exists. Please sign in or use a different mobile number.")
        if Seller.objects.filter(registered_mobile=normalized).exists():
            raise ValidationError("A seller store is already registered with this mobile number. Please sign in to Seller Hub or use a different mobile number.")
        return normalized

    def clean_support_phone(self):
        raw = self.cleaned_data.get("support_phone", "").strip()
        if not raw:
            raise ValidationError("Customer support phone is required.")
        digits = re.sub(r"[^\d]", "", raw)
        if len(digits) < 10:
            raise ValidationError("Please enter a valid support phone number with at least 10 digits.")
        return raw.strip()

    def clean_store_name(self):
        store_name = self.cleaned_data.get("store_name", "").strip()
        if not store_name:
            raise ValidationError("Store name is required.")
        if Seller.objects.filter(store_name__iexact=store_name).exists():
            raise ValidationError("A seller with this store name is already registered.")
        return store_name

    def clean_gstin(self):
        gstin = self.cleaned_data.get("gstin", "").strip().upper()
        if not gstin:
            raise ValidationError("GSTIN is mandatory for seller onboarding.")
        if not re.match(GSTIN_REGEX, gstin):
            raise ValidationError("Invalid Indian GSTIN format. Expected 15 characters (e.g. 29ABCDE1234F1Z5).")
        if Seller.objects.filter(gstin__iexact=gstin).exists():
            raise ValidationError("A seller with this GSTIN is already registered on the platform.")
        return gstin

    def clean_pan_number(self):
        pan = self.cleaned_data.get("pan_number", "").strip().upper()
        if pan and not re.match(PAN_REGEX, pan):
            raise ValidationError("Invalid PAN format. Expected 10 alphanumeric characters (e.g. ABCDE1234F).")
        return pan

    def clean_postal_code(self):
        pc = self.cleaned_data.get("postal_code", "").strip()
        if not re.match(r"^\d{6}$", pc):
            raise ValidationError("Please enter a valid 6-digit PIN code.")
        return pc

    def clean_pickup_postal_code(self):
        ppc = self.cleaned_data.get("pickup_postal_code", "").strip()
        if ppc and not re.match(r"^\d{6}$", ppc):
            raise ValidationError("Please enter a valid 6-digit pickup PIN code.")
        return ppc

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("confirm_password")
        email = cleaned_data.get("email")
        username = cleaned_data.get("username")

        if password and confirm and password != confirm:
            self.add_error("confirm_password", "Passwords do not match.")

        if password and (email or username):
            temp_user = User(username=username or email, email=email or "")
            try:
                validate_password(password, user=temp_user)
            except ValidationError as err:
                self.add_error("password", err)

        gstin = cleaned_data.get("gstin")
        pan = cleaned_data.get("pan_number")
        if gstin and not pan:
            cleaned_data["pan_number"] = gstin[2:12]
        elif gstin and pan and pan != gstin[2:12]:
            self.add_error("pan_number", f"PAN ({pan}) does not match the PAN encoded in GSTIN ({gstin[2:12]}).")

        return cleaned_data

    def save(self):
        """
        Creates the User account (with hashed password) and the associated Seller profile
        with STATUS_PENDING in a single atomic transaction.
        """
        with transaction.atomic():
            parts = self.cleaned_data["full_name"].strip().split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""

            user = User.objects.create_user(
                username=self.cleaned_data["username"],
                email=self.cleaned_data["email"],
                password=self.cleaned_data["password"],
                first_name=first_name,
                last_name=last_name,
                is_staff=False,
                is_superuser=False,
                is_active=True,
            )

            profile, _ = CustomerProfile.objects.get_or_create(user=user)
            profile.phone_number = self.cleaned_data["mobile"]
            profile.mobile_number = self.cleaned_data["mobile"]
            profile.save(update_fields=["phone_number", "mobile_number", "updated_at"])

            try:
                from allauth.account.models import EmailAddress
                EmailAddress.objects.get_or_create(
                    user=user,
                    email=user.email,
                    defaults={"primary": True, "verified": False},
                )
            except Exception:
                pass

            pickup_addr = self.cleaned_data.get("pickup_address") or self.cleaned_data["registered_address"]
            pickup_city = self.cleaned_data.get("pickup_city") or self.cleaned_data["city"]
            pickup_state = self.cleaned_data.get("pickup_state") or self.cleaned_data["state"]
            pickup_pin = self.cleaned_data.get("pickup_postal_code") or self.cleaned_data["postal_code"]

            seller = Seller.objects.create(
                user=user,
                store_name=self.cleaned_data["store_name"],
                legal_business_name=self.cleaned_data["legal_business_name"],
                business_type=self.cleaned_data["business_type"],
                store_description=self.cleaned_data.get("store_description", ""),
                support_email=self.cleaned_data["support_email"],
                support_phone=self.cleaned_data["support_phone"],
                registered_mobile=self.cleaned_data["mobile"],
                registered_address=self.cleaned_data["registered_address"],
                city=self.cleaned_data["city"],
                state=self.cleaned_data["state"],
                postal_code=self.cleaned_data["postal_code"],
                pickup_address=pickup_addr,
                pickup_city=pickup_city,
                pickup_state=pickup_state,
                pickup_postal_code=pickup_pin,
                gstin=self.cleaned_data["gstin"],
                pan_number=self.cleaned_data.get("pan_number", ""),
                status=Seller.STATUS_PENDING,
                is_active=True,
                commission_rate=Decimal("0.00"),
            )
            return user, seller


class SellerLoginForm(forms.Form):
    """
    Dedicated authentication form for the Fitmeezo Seller Hub login portal.
    """
    username_or_email = forms.CharField(
        max_length=254,
        required=True,
        label="Username or Email",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Enter merchant username or email",
                "autocomplete": "username",
                "required": True,
            }
        ),
    )
    password = forms.CharField(
        required=True,
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Enter your password",
                "autocomplete": "current-password",
                "required": True,
            }
        ),
    )
    remember_me = forms.BooleanField(
        required=False,
        initial=True,
        label="Remember me on this browser",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )


class SellerMobileOTPRequestForm(forms.Form):
    """
    Form validating the 10-digit Indian mobile number for requesting a Seller login OTP.
    """
    mobile = forms.CharField(
        max_length=15,
        required=True,
        label="Registered Mobile Number",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Enter 10-digit mobile number",
                "autocomplete": "tel",
                "maxlength": "10",
                "required": True,
            }
        ),
    )

    def clean_mobile(self):
        from core.otp_services import normalize_mobile_number
        raw = self.cleaned_data.get("mobile", "").strip()
        normalized = normalize_mobile_number(raw)
        if not normalized:
            raise ValidationError("Please enter a valid 10-digit Indian mobile number starting with 6, 7, 8, or 9.")
        return normalized


class SellerMobileOTPVerifyForm(forms.Form):
    """
    Form validating the 6-digit OTP and remember_me flag for Seller Hub login.
    """
    mobile = forms.CharField(
        max_length=15,
        required=True,
        widget=forms.HiddenInput(),
    )
    otp = forms.CharField(
        max_length=6,
        min_length=6,
        required=True,
        label="6-Digit Verification Code",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input text-center fw-bold fs-4",
                "placeholder": "Enter 6-digit OTP",
                "maxlength": "6",
                "autocomplete": "one-time-code",
                "required": True,
            }
        ),
    )
    remember_me = forms.BooleanField(
        required=False,
        initial=True,
        label="Remember me on this browser",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_mobile(self):
        from core.otp_services import normalize_mobile_number
        raw = self.cleaned_data.get("mobile", "").strip()
        normalized = normalize_mobile_number(raw)
        if not normalized:
            raise ValidationError("Invalid mobile number format.")
        return normalized

    def clean_otp(self):
        raw = self.cleaned_data.get("otp", "").strip()
        digits = re.sub(r"[^\d]", "", raw)
        if len(digits) != 6:
            raise ValidationError("Please enter a valid 6-digit verification code.")
        return digits


class SellerProfileUpdateForm(forms.ModelForm):
    """
    Profile update form for registered sellers to maintain operational details.
    Restricts access to administrative or legal tax fields to preserve audit integrity.
    """

    class Meta:
        model = Seller
        fields = [
            "store_name",
            "store_description",
            "support_email",
            "support_phone",
            "registered_address",
            "city",
            "state",
            "postal_code",
            "pickup_address",
            "pickup_city",
            "pickup_state",
            "pickup_postal_code",
        ]
        widgets = {
            "store_name": forms.TextInput(attrs={"class": "form-control fm-input"}),
            "store_description": forms.Textarea(attrs={"class": "form-control fm-input", "rows": 3}),
            "support_email": forms.EmailInput(attrs={"class": "form-control fm-input"}),
            "support_phone": forms.TextInput(attrs={"class": "form-control fm-input"}),
            "registered_address": forms.Textarea(attrs={"class": "form-control fm-input", "rows": 2}),
            "city": forms.TextInput(attrs={"class": "form-control fm-input"}),
            "state": forms.TextInput(attrs={"class": "form-control fm-input"}),
            "postal_code": forms.TextInput(attrs={"class": "form-control fm-input", "maxlength": "10"}),
            "pickup_address": forms.Textarea(attrs={"class": "form-control fm-input", "rows": 2}),
            "pickup_city": forms.TextInput(attrs={"class": "form-control fm-input"}),
            "pickup_state": forms.TextInput(attrs={"class": "form-control fm-input"}),
            "pickup_postal_code": forms.TextInput(attrs={"class": "form-control fm-input", "maxlength": "10"}),
        }

    def clean_store_name(self):
        store_name = self.cleaned_data.get("store_name", "").strip()
        if not store_name:
            raise ValidationError("Store name is required.")
        qs = Seller.objects.filter(store_name__iexact=store_name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("A seller with this store name is already registered.")
        return store_name


class SellerProductForm(forms.ModelForm):
    """
    Form for marketplace sellers to create and edit catalog merchandise.
    Strictly excludes platform-controlled fields: seller, is_approved, approval_status,
    rejection_reason, is_featured, and commission_rate.
    """

    class Meta:
        model = Product
        fields = [
            "name",
            "category",
            "sku",
            "price",
            "compare_at_price",
            "stock_quantity",
            "weight_grams",
            "length_cm",
            "breadth_cm",
            "height_cm",
            "short_description",
            "description",
            "image",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "e.g. Ergonomic Fitness Dumbbell Set", "required": True}
            ),
            "category": forms.Select(attrs={"class": "form-select fm-input", "required": True}),
            "sku": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "e.g. FIT-DB-005",
                    "style": "text-transform: uppercase;",
                    "required": True,
                }
            ),
            "price": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "1499.00", "required": True}
            ),
            "compare_at_price": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "1999.00"}
            ),
            "stock_quantity": forms.NumberInput(
                attrs={"class": "form-control fm-input", "min": "0", "placeholder": "25", "required": True}
            ),
            "weight_grams": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Weight in grams (e.g. 350.00)"}
            ),
            "length_cm": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Length in cm (e.g. 20.00)"}
            ),
            "breadth_cm": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Breadth in cm (e.g. 15.00)"}
            ),
            "height_cm": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Height in cm (e.g. 5.00)"}
            ),
            "short_description": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "Brief 1-line product summary"}
            ),
            "description": forms.Textarea(
                attrs={"class": "form-control fm-input", "rows": 4, "placeholder": "Comprehensive product details, materials, specs"}
            ),
            "image": forms.FileInput(attrs={"class": "form-control fm-input", "accept": "image/*"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    # Size Variants Configuration
    has_size_variants = forms.BooleanField(
        required=False,
        label="Enable Size Variants",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "id": "id_has_size_variants"}),
        help_text="Enable if this product is available in multiple clothing or footwear sizes with per-size inventory tracking.",
    )
    available_sizes = forms.CharField(
        required=False,
        label="Available Sizes",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. S, M, L, XL, XXL",
                "id": "id_available_sizes",
            }
        ),
        help_text="Enter sizes separated by commas, or click the quick-select pills below.",
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

    def clean_image(self):
        img = self.cleaned_data.get("image")
        if img and hasattr(img, "size"):
            validate_image_file(img)
        return img

    def clean_sku(self):
        sku = self.cleaned_data.get("sku", "").strip().upper()
        if not sku:
            raise ValidationError("SKU cannot be empty.")
        # Check collision with other products
        qs = Product.objects.filter(sku__iexact=sku)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(f"A product with SKU '{sku}' already exists.")
        # Check collision with variants
        if ProductVariant.objects.filter(sku__iexact=sku).exists():
            raise ValidationError(f"SKU '{sku}' is already in use by a product variant.")
        return sku

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
                    "Compare-at (M.R.P.) price cannot be lower than the selling price.",
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


class SellerProductVariantForm(forms.ModelForm):
    """
    Form for marketplace sellers to create or update variants belonging to their products.
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
            "is_active",
        ]
        widgets = {
            "option_type": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "e.g. Size, Color, Weight", "required": True}
            ),
            "option_value": forms.TextInput(
                attrs={"class": "form-control fm-input", "placeholder": "e.g. XL, Navy Blue, 10kg", "required": True}
            ),
            "sku": forms.TextInput(
                attrs={
                    "class": "form-control fm-input",
                    "placeholder": "e.g. FIT-DB-005-10KG",
                    "style": "text-transform: uppercase;",
                    "required": True,
                }
            ),
            "price": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.00", "placeholder": "Leave empty to use base price"}
            ),
            "compare_at_price": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.00", "placeholder": "Optional compare-at price"}
            ),
            "stock_quantity": forms.NumberInput(
                attrs={"class": "form-control fm-input", "min": "0", "placeholder": "15", "required": True}
            ),
            "weight_grams": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Leave empty to use base weight"}
            ),
            "length_cm": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Leave empty to use base length"}
            ),
            "breadth_cm": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Leave empty to use base breadth"}
            ),
            "height_cm": forms.NumberInput(
                attrs={"class": "form-control fm-input", "step": "0.01", "min": "0.01", "placeholder": "Leave empty to use base height"}
            ),
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
        if Product.objects.filter(sku__iexact=sku).exists():
            raise ValidationError(f"SKU '{sku}' is already in use by a product.")
        qs = ProductVariant.objects.filter(sku__iexact=sku)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(f"SKU '{sku}' is already in use by another variant.")
        return sku

    def clean(self):
        cleaned_data = super().clean()
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
                self.add_error("compare_at_price", "Compare-at price cannot be lower than the effective selling price.")

        for dim_f in ["weight_grams", "length_cm", "breadth_cm", "height_cm"]:
            val = cleaned_data.get(dim_f)
            if val is not None and val <= Decimal("0.00"):
                label = dim_f.replace("_", " ").capitalize()
                self.add_error(dim_f, f"{label} must be greater than zero.")

        return cleaned_data


class SellerProductImageForm(forms.ModelForm):
    """
    Form for uploading additional gallery showcase photos for a seller product.
    """

    class Meta:
        model = ProductImage
        fields = ["image", "alt_text", "display_order"]
        widgets = {
            "image": forms.FileInput(attrs={"class": "form-control fm-input", "accept": "image/*", "required": True}),
            "alt_text": forms.TextInput(attrs={"class": "form-control fm-input", "placeholder": "Image description for SEO"}),
            "display_order": forms.NumberInput(attrs={"class": "form-control fm-input", "min": "0"}),
        }

    def clean_image(self):
        img = self.cleaned_data.get("image")
        if img and hasattr(img, "size"):
            validate_image_file(img)
        return img


class SellerStandaloneInventoryForm(forms.Form):
    """
    Fast inventory stock adjustment form for standalone products.
    """
    stock_quantity = forms.IntegerField(
        min_value=0,
        label="Available Inventory Units",
        widget=forms.NumberInput(attrs={"class": "form-control fm-input", "min": "0", "required": True}),
    )


class SellerFulfillmentUpdateForm(forms.Form):
    """
    Form for validating controlled seller fulfillment status transitions
    and seller-entered logistics tracking details.
    """

    status = forms.CharField(
        max_length=30,
        required=True,
    )
    courier_name = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control fm-input", "placeholder": "e.g. Blue Dart, Delhivery, DTDC"}
        ),
    )
    tracking_number = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control fm-input", "placeholder": "e.g. AWB1234567890"}
        ),
    )

    ALLOWED_TRANSITIONS = {
        SellerOrder.STATUS_PENDING: [SellerOrder.STATUS_PROCESSING],
        SellerOrder.STATUS_PROCESSING: [SellerOrder.STATUS_READY_TO_SHIP],
        SellerOrder.STATUS_READY_TO_SHIP: [SellerOrder.STATUS_SHIPPED],
    }

    def __init__(self, *args, package=None, **kwargs):
        self.package = package
        super().__init__(*args, **kwargs)

    def clean_status(self):
        new_status = self.cleaned_data.get("status", "").strip().lower()
        if not self.package:
            raise ValidationError("Package reference is missing.")

        current_status = self.package.status
        allowed_next = self.ALLOWED_TRANSITIONS.get(current_status, [])

        if new_status not in allowed_next:
            if new_status == SellerOrder.STATUS_DELIVERED:
                raise ValidationError("Delivery confirmation cannot be performed by sellers. This is platform-managed.")
            elif current_status == SellerOrder.STATUS_CANCELLED:
                raise ValidationError("Cannot update a cancelled package.")
            elif current_status in [SellerOrder.STATUS_SHIPPED, SellerOrder.STATUS_DELIVERED]:
                raise ValidationError(f"Package is already {current_status}. No further fulfillment updates are permitted.")
            elif new_status == current_status:
                raise ValidationError(f"Package is already in status '{current_status}'.")
            else:
                allowed_display = ", ".join(allowed_next) if allowed_next else "None"
                raise ValidationError(
                    f"Invalid status transition from '{current_status}' to '{new_status}'. Allowed next status: {allowed_display}."
                )
        return new_status

    def clean(self):
        cleaned_data = super().clean()
        new_status = cleaned_data.get("status")
        courier_name = (cleaned_data.get("courier_name") or "").strip()
        tracking_number = (cleaned_data.get("tracking_number") or "").strip()

        if new_status == SellerOrder.STATUS_SHIPPED:
            if not courier_name:
                self.add_error("courier_name", "Courier/Carrier name is required when marking a package as Shipped.")
            elif len(courier_name) < 2:
                self.add_error("courier_name", "Courier name must be at least 2 characters.")
            elif len(courier_name) > 100:
                self.add_error("courier_name", "Courier name cannot exceed 100 characters.")

            if not tracking_number:
                self.add_error("tracking_number", "Tracking / AWB number is required when marking a package as Shipped.")
            elif len(tracking_number) < 3:
                self.add_error("tracking_number", "Tracking / AWB number must be at least 3 characters.")
            elif len(tracking_number) > 100:
                self.add_error("tracking_number", "Tracking / AWB number cannot exceed 100 characters.")

            cleaned_data["courier_name"] = courier_name
            cleaned_data["tracking_number"] = tracking_number

        return cleaned_data


class PackagePackedMeasurementsForm(forms.Form):
    """
    Dedicated form for authorized fulfillment staff or sellers to record
    actual measured packed parcel dimensions and dead weight (Tasks 2 & 6).
    """
    actual_weight_grams = forms.DecimalField(
        required=True,
        min_value=Decimal("0.01"),
        label="Actual Scale Weight (Grams)",
        widget=forms.NumberInput(attrs={"class": "form-control fm-input", "step": "0.01", "placeholder": "e.g. 850.00", "required": True}),
    )
    actual_length_cm = forms.DecimalField(
        required=True,
        min_value=Decimal("0.01"),
        label="Packed Carton Length (cm)",
        widget=forms.NumberInput(attrs={"class": "form-control fm-input", "step": "0.01", "placeholder": "e.g. 25.00", "required": True}),
    )
    actual_breadth_cm = forms.DecimalField(
        required=True,
        min_value=Decimal("0.01"),
        label="Packed Carton Breadth/Width (cm)",
        widget=forms.NumberInput(attrs={"class": "form-control fm-input", "step": "0.01", "placeholder": "e.g. 18.00", "required": True}),
    )
    actual_height_cm = forms.DecimalField(
        required=True,
        min_value=Decimal("0.01"),
        label="Packed Carton Height (cm)",
        widget=forms.NumberInput(attrs={"class": "form-control fm-input", "step": "0.01", "placeholder": "e.g. 10.00", "required": True}),
    )


# ==============================================================================
# PHASE 6: RETURNS, EXCHANGES & REFUND MANAGEMENT FORMS
# ==============================================================================

class ReturnRequestSubmissionForm(forms.Form):
    """
    Form for customers submitting a return or exchange request.
    """
    request_type = forms.ChoiceField(
        choices=[
            ("return", "Return & Refund"),
            ("exchange", "Exchange / Replacement"),
        ],
        label="Request Type",
        widget=forms.Select(attrs={"class": "form-select fm-input", "id": "id_request_type"}),
    )
    reason = forms.ChoiceField(
        choices=[
            ("defective", "Defective or Damaged Product"),
            ("wrong_item", "Wrong Item or Size Delivered"),
            ("size_fit", "Size or Fit Issue"),
            ("not_as_described", "Item Does Not Match Description"),
            ("quality_issue", "Quality / Material Disappointment"),
            ("other", "Other Reason"),
        ],
        label="Reason for Return",
        widget=forms.Select(attrs={"class": "form-select fm-input"}),
    )
    customer_notes = forms.CharField(
        required=False,
        label="Additional Details (Optional)",
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 3,
                "placeholder": "Describe any defects, issues, or details regarding the item...",
            }
        ),
    )
    photo_1 = forms.ImageField(
        required=False,
        label="Photo 1 (Defect / Item Condition)",
        widget=forms.FileInput(attrs={"class": "form-control fm-input", "accept": "image/*"}),
    )
    photo_2 = forms.ImageField(
        required=False,
        label="Photo 2 (Barcode / Brand Tag)",
        widget=forms.FileInput(attrs={"class": "form-control fm-input", "accept": "image/*"}),
    )
    photo_3 = forms.ImageField(
        required=False,
        label="Photo 3 (Outer Box / Packaging)",
        widget=forms.FileInput(attrs={"class": "form-control fm-input", "accept": "image/*"}),
    )

    def clean_photo_1(self):
        photo = self.cleaned_data.get("photo_1")
        if photo and photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Photo 1 file size cannot exceed 5MB.")
        return photo

    def clean_photo_2(self):
        photo = self.cleaned_data.get("photo_2")
        if photo and photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Photo 2 file size cannot exceed 5MB.")
        return photo

    def clean_photo_3(self):
        photo = self.cleaned_data.get("photo_3")
        if photo and photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Photo 3 file size cannot exceed 5MB.")
        return photo


class SellerReturnActionForm(forms.Form):
    """
    Form for seller or staff processing return requests:
    updating status, recording inspection results, or rejecting with reason.
    """
    status = forms.ChoiceField(
        choices=[],
        label="Update Status",
        widget=forms.Select(attrs={"class": "form-select fm-input"}),
    )
    notes = forms.CharField(
        required=False,
        label="Internal Remarks / Notes",
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 2,
                "placeholder": "Add remarks or inspection notes...",
            }
        ),
    )
    rejection_reason = forms.CharField(
        required=False,
        label="Rejection Reason (Required if Rejecting)",
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 2,
                "placeholder": "Provide specific reason why the return is rejected...",
            }
        ),
    )

    def __init__(self, *args, current_status="requested", allowed_transitions=None, **kwargs):
        super().__init__(*args, **kwargs)
        if allowed_transitions:
            self.fields["status"].choices = [
                (s, s.replace("_", " ").title()) for s in allowed_transitions
            ]
        else:
            self.fields["status"].choices = []

    def clean(self):
        cleaned_data = super().clean()
        new_status = cleaned_data.get("status")
        rej = (cleaned_data.get("rejection_reason") or "").strip()
        if new_status == "rejected" and not rej:
            self.add_error("rejection_reason", "Rejection reason is mandatory when rejecting a return request.")
        return cleaned_data


class ProcessRefundForm(forms.Form):
    """
    Form for staff/merchants initiating a simulated refund voucher.
    """
    approved_amount = forms.DecimalField(
        required=True,
        min_value=Decimal("0.00"),
        label="Approved Refund Amount (INR)",
        widget=forms.NumberInput(attrs={"class": "form-control fm-input", "step": "0.01"}),
    )
    reason = forms.CharField(
        required=False,
        label="Refund Reason / Notes",
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 2,
                "placeholder": "Reason for settlement...",
            }
        ),
    )


COMMON_INDIAN_BANKS = [
    "State Bank of India (SBI)",
    "HDFC Bank",
    "ICICI Bank",
    "Axis Bank",
    "Punjab National Bank",
    "Bank of Baroda",
    "Canara Bank",
    "Union Bank of India",
    "Bank of India",
    "Kotak Mahindra Bank",
    "Indian Bank",
    "IndusInd Bank",
    "Yes Bank",
    "IDBI Bank",
    "Federal Bank",
    "Central Bank of India",
    "Indian Overseas Bank",
    "UCO Bank",
    "Punjab & Sind Bank",
    "Bank of Maharashtra",
]

BANK_CHOICES = [
    ("", "-- Select or search your bank --"),
] + [(b, b) for b in COMMON_INDIAN_BANKS] + [
    ("OTHER", "Other (Bank not listed above)"),
]


class SellerBankDetailsForm(forms.Form):
    """
    Secure seller-facing bank details registration and update form.
    Captures beneficiary name, bank name (searchable dropdown + Other manual fallback),
    branch, account type, account number with mandatory confirmation, and 11-digit Indian IFSC code.

    Security & Privacy Rules:
    - Never exposes complete bank account numbers in unmasked templates or logs.
    - Requires staff/finance admin manual review before payout eligibility.
    - Updating bank details automatically resets verification status to 'pending'.
    - Selecting a bank name does not claim automated account ownership verification.
    """

    bank_account_holder_name = forms.CharField(
        max_length=150,
        required=True,
        label="Beneficiary / Account Holder Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "Exact name as registered with the bank (e.g. Acme Retail Pvt Ltd)",
                "autocomplete": "off",
            }
        ),
        help_text="Name must match your business entity or authorized proprietor's bank records.",
    )
    bank_name_select = forms.ChoiceField(
        choices=BANK_CHOICES,
        required=False,
        label="Bank Name",
        widget=forms.Select(attrs={"class": "form-select fm-input", "id": "id_bank_name_select"}),
        help_text="Select your bank from the predefined list or choose 'Other' to specify manually.",
    )
    other_bank_name = forms.CharField(
        max_length=100,
        required=False,
        label="Specify Bank Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "id": "id_other_bank_name",
                "placeholder": "Enter full bank name if 'Other' selected",
                "autocomplete": "off",
            }
        ),
        help_text="Provide full legal bank name if you selected 'Other'.",
    )
    bank_name = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.HiddenInput(attrs={"id": "id_bank_name"}),
    )
    bank_branch = forms.CharField(
        max_length=150,
        required=True,
        label="Branch Name / Locality",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. Indiranagar Branch, Bengaluru",
            }
        ),
    )
    bank_account_type = forms.ChoiceField(
        choices=[
            ("current", "Current Account (Recommended for Businesses)"),
            ("savings", "Savings Account"),
        ],
        required=True,
        label="Account Type",
        widget=forms.Select(attrs={"class": "form-select fm-input"}),
    )
    bank_ifsc_code = forms.CharField(
        max_length=11,
        min_length=11,
        required=True,
        label="IFSC Code",
        widget=forms.TextInput(
            attrs={
                "class": "form-control fm-input font-monospace text-uppercase",
                "placeholder": "11-character IFSC (e.g. HDFC0001234)",
                "maxlength": "11",
                "autocomplete": "off",
            }
        ),
        help_text="11-character alphanumeric code printed on your bank cheque or passbook.",
    )
    bank_account_number = forms.CharField(
        max_length=18,
        min_length=9,
        required=True,
        label="Bank Account Number",
        widget=forms.PasswordInput(
            render_value=False,
            attrs={
                "class": "form-control fm-input font-monospace",
                "placeholder": "Enter full bank account number (9-18 digits)",
                "maxlength": "18",
                "autocomplete": "new-password",
            }
        ),
    )
    confirm_account_number = forms.CharField(
        max_length=18,
        min_length=9,
        required=True,
        label="Confirm Bank Account Number",
        widget=forms.PasswordInput(
            render_value=False,
            attrs={
                "class": "form-control fm-input font-monospace",
                "placeholder": "Re-enter bank account number to confirm",
                "maxlength": "18",
                "autocomplete": "new-password",
            }
        ),
    )

    def __init__(self, *args, seller: Seller | None = None, **kwargs):
        self.seller = seller
        super().__init__(*args, **kwargs)
        if seller and not self.is_bound:
            self.fields["bank_account_holder_name"].initial = seller.bank_account_holder_name
            self.fields["bank_name"].initial = seller.bank_name
            self.fields["bank_branch"].initial = seller.bank_branch
            self.fields["bank_account_type"].initial = seller.bank_account_type
            self.fields["bank_ifsc_code"].initial = seller.bank_ifsc_code

            # Pre-populate bank_name_select & other_bank_name from existing seller.bank_name
            existing_bank = (seller.bank_name or "").strip()
            if existing_bank:
                matched_bank = None
                for b in COMMON_INDIAN_BANKS:
                    if b.lower() == existing_bank.lower() or existing_bank.lower() in b.lower():
                        matched_bank = b
                        break
                if matched_bank:
                    self.fields["bank_name_select"].initial = matched_bank
                else:
                    self.fields["bank_name_select"].initial = "OTHER"
                    self.fields["other_bank_name"].initial = existing_bank

    def clean_bank_ifsc_code(self):
        code = self.cleaned_data.get("bank_ifsc_code", "").strip().upper()
        ifsc_regex = r"^[A-Z]{4}0[A-Z0-9]{6}$"
        if not re.match(ifsc_regex, code):
            raise ValidationError(
                "Invalid Indian IFSC format. Must be 11 characters (e.g. HDFC0001234, SBIN0000456)."
            )
        return code

    def clean_bank_account_number(self):
        num = self.cleaned_data.get("bank_account_number", "").strip()
        if not num.isdigit():
            raise ValidationError("Bank account number must contain digits only.")
        if len(num) < 9 or len(num) > 18:
            raise ValidationError("Indian bank account numbers must be between 9 and 18 digits.")
        return num

    def clean_confirm_account_number(self):
        confirm_num = self.cleaned_data.get("confirm_account_number", "").strip()
        if not confirm_num.isdigit():
            raise ValidationError("Confirmation account number must contain digits only.")
        return confirm_num

    def clean(self):
        cleaned_data = super().clean()
        acc = cleaned_data.get("bank_account_number")
        confirm = cleaned_data.get("confirm_account_number")
        if acc and confirm and acc != confirm:
            self.add_error("confirm_account_number", "Bank account numbers do not match. Please verify carefully.")

        # Resolve bank_name from bank_name_select, other_bank_name, or legacy bank_name
        b_select = (cleaned_data.get("bank_name_select") or "").strip()
        b_other = (cleaned_data.get("other_bank_name") or "").strip()
        b_direct = (cleaned_data.get("bank_name") or "").strip()

        resolved_bank_name = ""
        if b_select == "OTHER":
            if not b_other:
                self.add_error("other_bank_name", "Please specify your bank name since 'Other' was selected.")
            else:
                resolved_bank_name = b_other
        elif b_select:
            resolved_bank_name = b_select
        elif b_other:
            resolved_bank_name = b_other
        elif b_direct:
            resolved_bank_name = b_direct
        else:
            self.add_error("bank_name_select", "Please select your bank or choose 'Other' to enter manually.")

        cleaned_data["bank_name"] = resolved_bank_name
        return cleaned_data

    def save(self, seller: Seller, actor=None) -> Seller:
        """
        Securely persists bank details to the Seller model.
        Resets verification status to pending and clears previous verification / rejection stamps.
        """
        seller.bank_account_holder_name = self.cleaned_data["bank_account_holder_name"].strip()
        seller.bank_account_number = self.cleaned_data["bank_account_number"].strip()
        seller.bank_ifsc_code = self.cleaned_data["bank_ifsc_code"].strip().upper()
        seller.bank_name = self.cleaned_data["bank_name"].strip()
        seller.bank_branch = self.cleaned_data["bank_branch"].strip()
        seller.bank_account_type = self.cleaned_data["bank_account_type"]
        seller.bank_verification_status = "pending"
        seller.bank_rejection_reason = ""
        seller.bank_verified_at = None
        seller.bank_verified_by = None
        seller.save(
            update_fields=[
                "bank_account_holder_name",
                "bank_account_number",
                "bank_ifsc_code",
                "bank_name",
                "bank_branch",
                "bank_account_type",
                "bank_verification_status",
                "bank_rejection_reason",
                "bank_verified_at",
                "bank_verified_by",
                "updated_at",
            ]
        )
        return seller


class AdminBankVerificationForm(forms.Form):
    """
    Platform administrator review form to approve or reject a seller's submitted bank details.
    """

    action = forms.ChoiceField(
        choices=[
            ("approve", "Verify & Approve Bank Account"),
            ("reject", "Reject Bank Details (Requires Reason)"),
        ],
        required=True,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )
    rejection_reason = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control fm-input",
                "rows": 3,
                "placeholder": "Provide specific reason if rejecting (e.g. Beneficiary name mismatch with GST certificate)...",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get("action")
        reason = (cleaned_data.get("rejection_reason") or "").strip()
        if action == "reject" and not reason:
            self.add_error("rejection_reason", "Rejection reason is mandatory when rejecting bank details.")
        return cleaned_data


class ContactForm(forms.ModelForm):
    """
    Customer & Merchant Contact Inquiry Submission Form.
    Provides rigorous client-side and server-side validation, clean error messages,
    accessible attributes, and sanitized field inputs.
    """

    class Meta:
        model = ContactInquiry
        fields = ["full_name", "email", "phone_number", "subject", "order_number", "message"]
        widgets = {
            "full_name": forms.TextInput(attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. Ramesh Kumar",
                "required": True,
                "autocomplete": "name",
                "id": "contact_full_name",
            }),
            "email": forms.EmailInput(attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. ramesh@example.com",
                "required": True,
                "autocomplete": "email",
                "id": "contact_email",
            }),
            "phone_number": forms.TextInput(attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. +91 98765 43210 (Optional)",
                "autocomplete": "tel",
                "id": "contact_phone_number",
            }),
            "subject": forms.Select(attrs={
                "class": "form-select fm-input",
                "required": True,
                "id": "contact_subject",
            }),
            "order_number": forms.TextInput(attrs={
                "class": "form-control fm-input",
                "placeholder": "e.g. FM-20261010-AB12 (Optional)",
                "id": "contact_order_number",
            }),
            "message": forms.Textarea(attrs={
                "class": "form-control fm-input",
                "rows": 5,
                "placeholder": "Please describe your inquiry or issue in detail...",
                "required": True,
                "id": "contact_message",
            }),
        }

    def clean_full_name(self):
        name = (self.cleaned_data.get("full_name") or "").strip()
        if len(name) < 2:
            raise ValidationError("Please enter your full name (at least 2 characters).")
        return name

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise ValidationError("Please provide a valid email address.")
        return email

    def clean_message(self):
        message = (self.cleaned_data.get("message") or "").strip()
        if len(message) < 10:
            raise ValidationError("Your message is too short. Please provide at least 10 characters.")
        if len(message) > 5000:
            raise ValidationError("Your message exceeds the maximum allowed length of 5000 characters.")
        return message

    def clean_order_number(self):
        ord_num = (self.cleaned_data.get("order_number") or "").strip().upper()
        if ord_num.startswith("#"):
            ord_num = ord_num[1:].strip()
        return ord_num


class OrderTrackingForm(forms.Form):
    """
    Customer order tracking search form with dual-factor verification
    (order number + customer phone or email) to protect privacy.
    """
    order_number = forms.CharField(
        max_length=40,
        required=True,
        widget=forms.TextInput(attrs={
            "class": "form-control fm-input form-control-lg",
            "placeholder": "e.g. FM-20261010-AB12",
            "required": True,
            "autocomplete": "off",
            "id": "fmTrackOrderNumber",
        }),
        label="Order Number",
    )
    verification = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(attrs={
            "class": "form-control fm-input form-control-lg",
            "placeholder": "Email address or 10-digit mobile number used at checkout",
            "required": True,
            "autocomplete": "off",
            "id": "fmTrackVerification",
        }),
        label="Email or Mobile Number",
        help_text="Enter the email address or phone number provided during checkout.",
    )

    def clean_order_number(self):
        ord_num = (self.cleaned_data.get("order_number") or "").strip().upper()
        if ord_num.startswith("#"):
            ord_num = ord_num[1:].strip()
        if not ord_num:
            raise ValidationError("Please enter your Fitmeezo order number.")
        return ord_num

    def clean_verification(self):
        v = (self.cleaned_data.get("verification") or "").strip().lower()
        if not v:
            raise ValidationError("Please enter your verification email or phone number.")
        return v



