from decimal import Decimal
import re
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction, IntegrityError
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


class Category(models.Model):
    """
    Fitmeezo Product Category model.
    Organizes merchandise into department stores with support for custom ordering,
    status toggles, SEO slugs, and optional department imagery.
    """

    SLUG_ICON_MAP = {
        "electronics": "bi-laptop",
        "fashion": "bi-bag",
        "home-living": "bi-house-door",
        "beauty": "bi-stars",
        "sports": "bi-bicycle",
        "baby-kids": "bi-emoji-smile",
    }

    SLUG_BADGE_MAP = {
        "electronics": "Hot",
        "fashion": "New Season",
        "beauty": "Trending",
    }

    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Primary display name for the category (e.g. 'Electronics').",
    )
    slug = models.SlugField(
        max_length=120,
        unique=True,
        blank=True,
        help_text="URL-friendly identifier (e.g. 'home-living'). Auto-generated from name if left empty.",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Brief descriptive summary for department landing pages and SEO.",
    )
    image = models.ImageField(
        upload_to="categories/",
        blank=True,
        null=True,
        help_text="Optional department promotional or banner image.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Controls whether this category is visible across customer storefront navigation.",
    )
    display_order = models.PositiveIntegerField(
        default=0,
        db_index=True,
        help_text="Numerical weight for ordering in navigation bars and grids (lower values appear first).",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when category was created.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when category was last modified.",
    )

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name = "Category"
        verbose_name_plural = "Categories"
        indexes = [
            models.Index(fields=["slug", "is_active"], name="cat_slug_active_idx"),
            models.Index(fields=["display_order", "name"], name="cat_order_name_idx"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.name:
            self.name = self.name.strip()
        if self.slug:
            self.slug = self.slug.strip().lower()

    def save(self, *args, **kwargs):
        if not self.slug and self.name:
            self.slug = slugify(self.name)
        elif self.slug:
            self.slug = slugify(self.slug)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("core:category_detail", kwargs={"slug": self.slug})

    @property
    def icon_class(self):
        """Returns Bootstrap Icon class according to slug or default generic tag."""
        return self.SLUG_ICON_MAP.get(self.slug, "bi-tag")

    @property
    def icon(self):
        """Alias for icon_class."""
        return self.icon_class

    @property
    def desc(self):
        """Alias for description."""
        return self.description

    @property
    def badge(self):
        """Returns promotional badge text if applicable."""
        return self.SLUG_BADGE_MAP.get(self.slug, "")


GSTIN_REGEX = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
PAN_REGEX = r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$"
IFSC_REGEX = r"^[A-Z]{4}0[A-Z0-9]{6}$"


class Seller(models.Model):
    """
    Fitmeezo Third-Party Seller / Vendor profile.
    Encapsulates vendor legal identity, store branding, business address,
    warehouse pickup location, Indian tax identifiers (GSTIN/PAN),
    commission rates, and platform administrative approval status.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_SUSPENDED = "suspended"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Approval"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_SUSPENDED, "Suspended"),
    ]

    BUSINESS_TYPE_INDIVIDUAL = "individual"
    BUSINESS_TYPE_PROPRIETORSHIP = "sole_proprietorship"
    BUSINESS_TYPE_PARTNERSHIP = "partnership"
    BUSINESS_TYPE_PVT_LTD = "private_limited"
    BUSINESS_TYPE_LLP = "llp"

    BUSINESS_TYPE_CHOICES = [
        (BUSINESS_TYPE_INDIVIDUAL, "Individual / Proprietor"),
        (BUSINESS_TYPE_PROPRIETORSHIP, "Sole Proprietorship"),
        (BUSINESS_TYPE_PARTNERSHIP, "Partnership Firm"),
        (BUSINESS_TYPE_PVT_LTD, "Private Limited Company (Pvt Ltd)"),
        (BUSINESS_TYPE_LLP, "Limited Liability Partnership (LLP)"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="seller_profile",
        help_text="User account associated with this seller. Must not have staff/superuser access.",
    )
    store_name = models.CharField(
        max_length=150,
        unique=True,
        db_index=True,
        help_text="Public store/brand name displayed on storefront and product pages.",
    )
    slug = models.SlugField(
        max_length=170,
        unique=True,
        blank=True,
        help_text="URL-friendly identifier for seller store landing page.",
    )
    legal_business_name = models.CharField(
        max_length=200,
        help_text="Registered legal name of the entity or proprietor.",
    )
    business_type = models.CharField(
        max_length=50,
        choices=BUSINESS_TYPE_CHOICES,
        default=BUSINESS_TYPE_INDIVIDUAL,
        help_text="Legal constitution of the business entity.",
    )
    store_description = models.TextField(
        blank=True,
        default="",
        help_text="Store biography, merchandising summary, or brand background.",
    )

    # Support & Contact
    support_email = models.EmailField(
        help_text="Primary support email for customer communications.",
    )
    support_phone = models.CharField(
        max_length=20,
        help_text="Operational phone number for seller coordination.",
    )
    registered_mobile = models.CharField(
        max_length=15,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique verified 10-digit mobile number for merchant OTP authentication.",
    )

    # Registered Address
    registered_address = models.TextField(
        help_text="Official registered business address for invoicing and legal records.",
    )
    city = models.CharField(
        max_length=100,
        help_text="City of registered office.",
    )
    state = models.CharField(
        max_length=100,
        help_text="State of registered office.",
    )
    postal_code = models.CharField(
        max_length=10,
        help_text="PIN / Postal code of registered office.",
    )

    # Dispatch / Pickup Location
    pickup_address = models.TextField(
        help_text="Warehouse or facility address where couriers collect outgoing shipments.",
    )
    pickup_city = models.CharField(
        max_length=100,
        help_text="City of courier pickup facility.",
    )
    pickup_state = models.CharField(
        max_length=100,
        help_text="State of courier pickup facility.",
    )
    pickup_postal_code = models.CharField(
        max_length=10,
        help_text="PIN / Postal code of pickup facility.",
    )
    pickup_location_nickname = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Optional nickname for pickup location in provider system (e.g. 'Primary', 'Warehouse-1').",
    )
    pickup_location_verified = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if warehouse pickup facility has been verified by platform staff or courier onboarding.",
    )
    pickup_verification_notes = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Internal notes on pickup facility verification or courier accessibility.",
    )

    # Indian Tax Identifiers
    gstin = models.CharField(
        max_length=15,
        blank=True,
        default="",
        db_index=True,
        help_text="Goods and Services Tax Identification Number (15 characters). Optional for exempt categories.",
    )
    pan_number = models.CharField(
        max_length=10,
        blank=True,
        default="",
        db_index=True,
        help_text="Permanent Account Number of the entity or proprietor (10 characters).",
    )

    # Bank Account & Payout Settlement Details
    bank_account_holder_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Beneficiary account holder name as registered in bank records.",
    )
    bank_account_number = models.CharField(
        max_length=34,
        blank=True,
        default="",
        help_text="Bank account number (stored securely, displayed masked).",
    )
    bank_ifsc_code = models.CharField(
        max_length=11,
        blank=True,
        default="",
        help_text="11-character Indian Financial System Code (IFSC).",
    )
    bank_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Name of the banking institution (e.g. HDFC Bank, State Bank of India).",
    )
    bank_branch = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Bank branch name or locality.",
    )
    bank_account_type = models.CharField(
        max_length=20,
        choices=[
            ("current", "Current Account"),
            ("savings", "Savings Account"),
        ],
        default="current",
        help_text="Type of business bank account.",
    )
    BANK_STATUS_UNVERIFIED = "unverified"
    BANK_STATUS_PENDING = "pending"
    BANK_STATUS_VERIFIED = "verified"
    BANK_STATUS_REJECTED = "rejected"

    BANK_STATUS_CHOICES = [
        (BANK_STATUS_UNVERIFIED, "Not Submitted / Unverified"),
        (BANK_STATUS_PENDING, "Pending Verification"),
        (BANK_STATUS_VERIFIED, "Verified & Approved"),
        (BANK_STATUS_REJECTED, "Verification Rejected"),
    ]

    bank_verification_status = models.CharField(
        max_length=20,
        choices=BANK_STATUS_CHOICES,
        default=BANK_STATUS_UNVERIFIED,
        db_index=True,
        help_text="Administrative bank verification status.",
    )
    bank_rejection_reason = models.TextField(
        blank=True,
        default="",
        help_text="Reason for rejection if bank details verification failed.",
    )
    bank_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when bank details were verified by platform finance administrator.",
    )
    bank_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="verified_seller_banks",
        help_text="Platform staff member who audited and verified bank details.",
    )

    # Governance & Commission
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="Administrative onboarding approval status.",
    )
    admin_notes = models.TextField(
        blank=True,
        default="",
        help_text="Internal notes from platform administrator regarding this seller.",
    )
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Marketplace commission percentage retained by Fitmeezo (e.g. 10.00%).",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Controls whether this seller's products are visible on the customer storefront.",
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when administrator approved this seller.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when application was submitted.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when seller profile was last updated.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Seller"
        verbose_name_plural = "Sellers"
        indexes = [
            models.Index(fields=["status", "is_active"], name="seller_status_active_idx"),
            models.Index(fields=["slug", "status"], name="seller_slug_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["gstin"],
                condition=~models.Q(gstin=""),
                name="unique_seller_gstin_when_present",
            ),
        ]

    def __str__(self):
        return f"{self.store_name} ({self.get_status_display()})"

    @property
    def has_valid_gstin(self):
        """Validates that GSTIN is present and adheres to Indian GSTIN format."""
        if not self.gstin:
            return False
        clean_val = self.gstin.strip().upper()
        return bool(re.match(GSTIN_REGEX, clean_val))

    @property
    def is_approved(self):
        return self.status == self.STATUS_APPROVED and self.is_active

    @property
    def can_sell(self):
        return self.is_approved and self.has_valid_gstin

    def get_masked_account_number(self) -> str:
        """
        Returns masked bank account number (e.g. '••••••••1234')
        preventing full account number exposure in UI displays, templates, and logs.
        """
        if not self.bank_account_number:
            return "Not Provided"
        num = self.bank_account_number.strip()
        if len(num) >= 4:
            return "•" * (len(num) - 4) + num[-4:]
        return "••••••••"

    @property
    def is_bank_payout_eligible(self) -> bool:
        """
        Returns True strictly if seller is approved, bank verification status is verified,
        and essential bank fields are provided.
        Never treats seller-entered details alone as proof of bank ownership.
        """
        return (
            self.status == self.STATUS_APPROVED
            and self.bank_verification_status == "verified"
            and bool(self.bank_account_number)
            and bool(self.bank_ifsc_code)
        )

    def clean(self):
        super().clean()
        user = None
        try:
            if hasattr(self, "user"):
                user = self.user
        except ObjectDoesNotExist:
            user = None

        if user:
            if not user.is_active:
                raise ValidationError({
                    "user": "Inactive users cannot be assigned as sellers."
                })
            if user.is_staff or user.is_superuser:
                raise ValidationError({
                    "user": "Staff members and superusers cannot be assigned as sellers. Seller accounts must be standard customer accounts."
                })
            dup_seller = Seller.objects.filter(user=user)
            if self.pk:
                dup_seller = dup_seller.exclude(pk=self.pk)
            if dup_seller.exists():
                raise ValidationError({
                    "user": "This user is already linked to another seller store. Each user can only have one seller profile."
                })

        # Inherit courier pickup facility from registered business address if omitted
        if not (self.pickup_address or "").strip() and (self.registered_address or "").strip():
            self.pickup_address = self.registered_address.strip()
        if not (self.pickup_city or "").strip() and (self.city or "").strip():
            self.pickup_city = self.city.strip()
        if not (self.pickup_state or "").strip() and (self.state or "").strip():
            self.pickup_state = self.state.strip()
        if not (self.pickup_postal_code or "").strip() and (self.postal_code or "").strip():
            self.pickup_postal_code = self.postal_code.strip()

        if self.store_name:
            self.store_name = self.store_name.strip()
        if self.legal_business_name:
            self.legal_business_name = self.legal_business_name.strip()
        if self.slug:
            self.slug = self.slug.strip().lower()

        # Normalization and Validation of GSTIN
        if self.gstin:
            self.gstin = self.gstin.strip().upper()
            if not re.match(GSTIN_REGEX, self.gstin):
                raise ValidationError({
                    "gstin": "Invalid Indian GSTIN format. Expected 15 characters (e.g. 29ABCDE1234F1Z5)."
                })
            dup_qs = Seller.objects.filter(gstin__iexact=self.gstin)
            if self.pk:
                dup_qs = dup_qs.exclude(pk=self.pk)
            if dup_qs.exists():
                raise ValidationError({
                    "gstin": "A seller with this GSTIN is already registered on the platform."
                })

        # A seller cannot be approved without a valid GSTIN
        if self.status == self.STATUS_APPROVED and not self.has_valid_gstin:
            raise ValidationError({
                "gstin": "A seller cannot be approved without a valid 15-character GSTIN. Please provide a valid GSTIN or set status to Pending."
            })

        # Commission rate validation
        if self.commission_rate is not None:
            if self.commission_rate < Decimal("0.00") or self.commission_rate > Decimal("100.00"):
                raise ValidationError({
                    "commission_rate": "Commission rate must be between 0.00% and 100.00%."
                })

        # Normalization and Validation of PAN
        if self.pan_number:
            self.pan_number = self.pan_number.strip().upper()
            if not re.match(PAN_REGEX, self.pan_number):
                raise ValidationError({
                    "pan_number": "Invalid Indian PAN format. Expected 10 alphanumeric characters (e.g. ABCDE1234F)."
                })

        # Cross-validation: PAN embedded inside GSTIN
        if self.gstin and self.pan_number:
            pan_in_gstin = self.gstin[2:12]
            if pan_in_gstin != self.pan_number:
                raise ValidationError({
                    "pan_number": f"PAN ({self.pan_number}) does not match the PAN encoded in GSTIN ({pan_in_gstin})."
                })
        elif self.gstin and not self.pan_number:
            # Auto-extract PAN from valid GSTIN
            self.pan_number = self.gstin[2:12]

        # Normalization and Validation of registered_mobile
        if self.registered_mobile:
            clean_digits = re.sub(r"[^\d]", "", str(self.registered_mobile))
            if len(clean_digits) >= 10:
                clean_digits = clean_digits[-10:]
            if len(clean_digits) != 10 or clean_digits[0] not in "6789":
                raise ValidationError({
                    "registered_mobile": "Please enter a valid 10-digit Indian mobile number starting with 6, 7, 8, or 9."
                })
            self.registered_mobile = clean_digits
            dup_mobile = Seller.objects.filter(registered_mobile=self.registered_mobile)
            if self.pk:
                dup_mobile = dup_mobile.exclude(pk=self.pk)
            if dup_mobile.exists():
                raise ValidationError({
                    "registered_mobile": "This mobile number is already linked to another registered seller store."
                })

    def save(self, *args, **kwargs):
        if not self.registered_mobile and self.support_phone:
            clean_digits = re.sub(r"[^\d]", "", str(self.support_phone))
            if len(clean_digits) >= 10:
                candidate = clean_digits[-10:]
                if candidate[0] in "6789":
                    dup = Seller.objects.filter(registered_mobile=candidate)
                    if self.pk:
                        dup = dup.exclude(pk=self.pk)
                    if not dup.exists():
                        self.registered_mobile = candidate

        if not self.slug and self.store_name:
            base_slug = slugify(self.store_name)
            slug = base_slug
            counter = 1
            while Seller.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        elif self.slug:
            self.slug = slugify(self.slug)

        super().save(*args, **kwargs)

    def approve(self, admin_user=None, commit=True):
        """Transitions status to APPROVED, updates approved_at timestamp."""
        if not self.has_valid_gstin:
            raise ValidationError("Cannot approve seller without a valid submitted GSTIN.")
        self.status = self.STATUS_APPROVED
        self.approved_at = timezone.now()
        self.is_active = True
        if commit:
            self.save(update_fields=["status", "approved_at", "is_active", "updated_at"])

    def reject(self, reason="", admin_user=None, commit=True):
        """Transitions status to REJECTED."""
        self.status = self.STATUS_REJECTED
        self.is_active = False
        if reason:
            self.admin_notes = f"{self.admin_notes}\n[Rejected]: {reason}".strip()
        if commit:
            self.save(update_fields=["status", "is_active", "admin_notes", "updated_at"])

    def suspend(self, reason="", admin_user=None, commit=True):
        """Transitions status to SUSPENDED."""
        self.status = self.STATUS_SUSPENDED
        self.is_active = False
        if reason:
            self.admin_notes = f"{self.admin_notes}\n[Suspended]: {reason}".strip()
        if commit:
            self.save(update_fields=["status", "is_active", "admin_notes", "updated_at"])

    def validate_pickup_location(self) -> dict:
        """
        Server-side validation checking whether the seller's pickup facility
        is sufficiently complete before future courier booking (Task 4).
        Returns:
            dict: {
                "is_complete": bool,
                "is_verified": bool,
                "status": "incomplete" | "unverified" | "verified",
                "errors": list[str],
                "details": dict,
            }
        """
        from core.shipping_dimensions import PIN_CODE_PATTERN, PHONE_PATTERN

        errors = []
        clean_addr = (self.pickup_address or "").strip()
        if not clean_addr or len(clean_addr) < 5:
            errors.append("Pickup warehouse address is required (minimum 5 characters).")

        clean_city = (self.pickup_city or "").strip()
        if not clean_city or len(clean_city) < 2:
            errors.append("Pickup city is required.")

        clean_state = (self.pickup_state or "").strip()
        if not clean_state or len(clean_state) < 2:
            errors.append("Pickup state is required.")

        clean_pin = (self.pickup_postal_code or "").strip()
        if not clean_pin or not PIN_CODE_PATTERN.match(clean_pin):
            errors.append("Valid 6-digit Indian PIN code is required for pickup facility (e.g. 560001).")

        clean_phone = (self.support_phone or "").strip()
        if not clean_phone or not PHONE_PATTERN.match(clean_phone):
            errors.append("Valid dispatch contact phone number is required (10-15 digits).")

        is_complete = len(errors) == 0
        is_verified = bool(self.pickup_location_verified) and is_complete

        if not is_complete:
            status = "incomplete"
        elif is_verified:
            status = "verified"
        else:
            status = "unverified"

        return {
            "source": "seller",
            "name": self.store_name,
            "pickup_location_name": self.legal_business_name or self.store_name,
            "is_complete": is_complete,
            "is_verified": is_verified,
            "status": status,
            "verification_status": status,
            "address": self.pickup_address,
            "city": self.pickup_city,
            "state": self.pickup_state,
            "postal_code": self.pickup_postal_code,
            "phone": self.support_phone,
            "errors": errors,
            "validation_errors": errors,
            "details": {
                "source": "seller",
                "store_name": self.store_name,
                "contact_name": self.legal_business_name or self.store_name,
                "phone": self.support_phone,
                "email": self.support_email,
                "address": self.pickup_address,
                "city": self.pickup_city,
                "state": self.pickup_state,
                "postal_code": self.pickup_postal_code,
                "nickname": self.pickup_location_nickname or "Primary Warehouse",
                "notes": self.pickup_verification_notes,
            },
        }

class SellerLoginOTP(models.Model):
    """
    Cryptographically hashed, single-use One-Time Password for Seller Mobile Login.
    OTPs are never stored or logged in plain text. Expire strictly after 5 minutes.
    """
    seller = models.ForeignKey(
        "Seller",
        on_delete=models.CASCADE,
        related_name="login_otps",
        help_text="Seller account requesting login authentication.",
    )
    mobile = models.CharField(
        max_length=15,
        db_index=True,
        help_text="10-digit mobile number to which OTP was dispatched.",
    )
    otp_hash = models.CharField(
        max_length=255,
        help_text="PBKDF2/SHA256 salted hash of the single-use OTP.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when OTP was generated and dispatched.",
    )
    expires_at = models.DateTimeField(
        db_index=True,
        help_text="Expiration timestamp (strictly 5 minutes from creation).",
    )
    is_used = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether this OTP has already been consumed for session login.",
    )
    used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when OTP was consumed.",
    )
    attempts_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of verification attempts made against this OTP (max 5).",
    )
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="Client IP address from which OTP was requested.",
    )
    user_agent = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Client User-Agent header snippet.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Seller Login OTP"
        verbose_name_plural = "Seller Login OTPs"
        indexes = [
            models.Index(fields=["mobile", "-created_at"], name="seller_otp_mobile_created_idx"),
            models.Index(fields=["seller", "is_used"], name="seller_otp_seller_used_idx"),
        ]

    def __str__(self):
        from core.sms_services import mask_phone_number
        status = "Used" if self.is_used else "Active"
        return f"OTP for {self.seller.store_name} ({mask_phone_number(self.mobile)}) - {status}"

    def set_otp(self, raw_otp: str):
        from django.contrib.auth.hashers import make_password
        self.otp_hash = make_password(raw_otp)

    def verify_otp(self, raw_otp: str) -> tuple[bool, str]:
        from django.contrib.auth.hashers import check_password
        from django.utils import timezone

        if self.is_used:
            return False, "This OTP has already been used. Please request a new OTP."

        if timezone.now() > self.expires_at:
            return False, "This OTP has expired. OTPs are valid for 5 minutes. Please request a new OTP."

        if self.attempts_count >= 5:
            return False, "Too many failed verification attempts. This OTP has been invalidated. Please request a new OTP."

        self.attempts_count += 1

        if check_password(raw_otp, self.otp_hash):
            self.is_used = True
            self.used_at = timezone.now()
            self.save(update_fields=["is_used", "used_at", "attempts_count"])
            return True, "Verification successful."
        else:
            self.save(update_fields=["attempts_count"])
            remaining = max(0, 5 - self.attempts_count)
            if remaining > 0:
                return False, f"Invalid OTP. {remaining} attempt(s) remaining."
            return False, "Invalid OTP. Maximum attempts exceeded. This OTP has been invalidated."


class Product(models.Model):
    """
    Fitmeezo Product model.
    Represents an individual physical item available for purchase,
    with exact decimal pricing in INR, unique SKU tracking, stock level constraints,
    dynamic discount calculation, and foreign key linkage to Category.
    """

    APPROVAL_PENDING = "pending"
    APPROVAL_APPROVED = "approved"
    APPROVAL_REJECTED = "rejected"

    APPROVAL_CHOICES = [
        (APPROVAL_PENDING, "Pending Approval"),
        (APPROVAL_APPROVED, "Approved"),
        (APPROVAL_REJECTED, "Rejected"),
    ]

    name = models.CharField(
        max_length=200,
        db_index=True,
        help_text="Full product title displayed to customers (e.g. 'FitPulse Pro Max Smartwatch').",
    )
    slug = models.SlugField(
        max_length=220,
        unique=True,
        blank=True,
        help_text="SEO-friendly URL identifier. Auto-generated from name if left empty.",
    )
    sku = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Stock Keeping Unit (SKU) - unique inventory identifier (e.g. 'EL-SMW-001').",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="products",
        help_text="Department category to which this product belongs.",
    )
    seller = models.ForeignKey(
        "Seller",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        help_text="Associated vendor or seller. Null indicates Fitmeezo direct merchandise.",
    )
    short_description = models.CharField(
        max_length=300,
        blank=True,
        default="",
        help_text="Concise summary for search previews and product cards.",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Full product specifications, materials, warranty, and package contents.",
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Current customer selling price in INR (e.g. 2499.00).",
    )
    compare_at_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Optional original/M.R.P. price in INR for calculating discount percentage.",
    )
    image = models.ImageField(
        upload_to="products/",
        null=True,
        blank=True,
        help_text="Primary product photograph or showcase image.",
    )
    stock_quantity = models.PositiveIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Available inventory units in warehouse (0 marks out of stock).",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Controls whether this product is publicly discoverable and purchasable.",
    )
    is_featured = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether this product is featured on the homepage showcase section.",
    )
    is_approved = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Platform administrative moderation status. Required for public storefront listing.",
    )
    approval_status = models.CharField(
        max_length=20,
        choices=APPROVAL_CHOICES,
        default=APPROVAL_APPROVED,
        db_index=True,
        help_text="Administrative moderation lifecycle status.",
    )
    rejection_reason = models.TextField(
        blank=True,
        default="",
        help_text="Administrative explanation if product was rejected.",
    )

    # Return & Policy Configuration
    is_returnable = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Controls whether this product is eligible for return/exchange under policy.",
    )
    return_window_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Product-specific return window in days. If null, global FITMEEZO_RETURN_WINDOW_DAYS applies.",
    )

    # Physical Dimensions & Logistics (Phase 5 Task 1)
    weight_grams = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Physical item dead weight in grams (e.g. 250.00). Null indicates legacy/unknown.",
    )
    length_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Physical package length in cm (e.g. 20.00). Null indicates legacy/unknown.",
    )
    breadth_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Physical package breadth/width in cm (e.g. 15.00). Null indicates legacy/unknown.",
    )
    height_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Physical package height in cm (e.g. 5.00). Null indicates legacy/unknown.",
    )
    is_dimensions_verified = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if dimensions have been verified by vendor or platform QA. False indicates unverified or estimated.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when product was added to catalogue.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when product details were last updated.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Product"
        verbose_name_plural = "Products"
        indexes = [
            models.Index(fields=["slug", "is_active"], name="prod_slug_active_idx"),
            models.Index(fields=["category", "is_active"], name="prod_cat_active_idx"),
            models.Index(fields=["seller", "is_active"], name="prod_seller_active_idx"),
            models.Index(fields=["is_featured", "is_active"], name="prod_feat_active_idx"),
            models.Index(fields=["created_at", "is_active"], name="prod_created_active_idx"),
            models.Index(fields=["is_approved", "is_active"], name="prod_appr_active_idx"),
            models.Index(fields=["is_dimensions_verified"], name="prod_dim_verified_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price__gte=0),
                name="product_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(stock_quantity__gte=0),
                name="product_stock_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(compare_at_price__isnull=True) | models.Q(compare_at_price__gte=models.F("price")),
                name="product_compare_at_price_gte_price",
            ),
            models.CheckConstraint(
                condition=models.Q(weight_grams__isnull=True) | models.Q(weight_grams__gt=0),
                name="product_weight_grams_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(length_cm__isnull=True) | models.Q(length_cm__gt=0),
                name="product_length_cm_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(breadth_cm__isnull=True) | models.Q(breadth_cm__gt=0),
                name="product_breadth_cm_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(height_cm__isnull=True) | models.Q(height_cm__gt=0),
                name="product_height_cm_gt_zero",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    def clean(self):
        super().clean()
        if self.price is not None and self.price < Decimal("0.00"):
            raise ValidationError({"price": "Price cannot be negative."})
        if self.stock_quantity is not None and self.stock_quantity < 0:
            raise ValidationError({"stock_quantity": "Stock quantity cannot be negative."})
        if self.compare_at_price is not None and self.price is not None:
            if self.compare_at_price < self.price:
                raise ValidationError({
                    "compare_at_price": "Original price (compare-at price) cannot be lower than the selling price."
                })
        for dim_field in ["weight_grams", "length_cm", "breadth_cm", "height_cm"]:
            val = getattr(self, dim_field)
            if val is not None and val <= Decimal("0.00"):
                label = dim_field.replace("_", " ").capitalize()
                raise ValidationError({dim_field: f"{label} must be greater than zero."})
        if self.sku:
            if "ProductVariant" in globals() or "ProductVariant" in locals():
                if ProductVariant.objects.filter(sku__iexact=self.sku.strip()).exists():
                    raise ValidationError({"sku": f"SKU '{self.sku}' is already in use by a product variant."})


    def generate_unique_slug(self, base_slug=None):
        """
        Generates a unique slug for this product.
        If a collision occurs with another product, appends -2, -3, etc.
        Respects the max_length=220 constraint of the slug field.
        """
        raw_source = base_slug or self.slug or self.name or "product"
        base = slugify(raw_source)
        if not base:
            base = "product"

        # Truncate base so that suffix like -9999 fits within 220 chars
        max_base_len = 200
        base = base[:max_base_len].rstrip("-")
        if not base:
            base = "product"

        candidate = base
        counter = 2

        # Fast lookup of existing slugs sharing this prefix
        qs = Product.objects.filter(slug__startswith=base)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        existing_slugs = set(qs.values_list("slug", flat=True))

        while candidate in existing_slugs:
            candidate = f"{base}-{counter}"
            counter += 1

        return candidate

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "slug" not in update_fields and self.slug:
            if self.sku:
                self.sku = self.sku.strip().upper()
            self.full_clean()
            super().save(*args, **kwargs)
            return

        if self.sku:
            self.sku = self.sku.strip().upper()

        is_new = self.pk is None
        if not self.slug:
            self.slug = self.generate_unique_slug()
        elif is_new:
            clean = slugify(self.slug)
            if not clean or Product.objects.filter(slug=clean).exists():
                self.slug = self.generate_unique_slug(base_slug=clean or self.slug)
            else:
                self.slug = clean
        else:
            clean = slugify(self.slug)
            if not clean:
                self.slug = self.generate_unique_slug()
            elif Product.objects.filter(slug=clean).exclude(pk=self.pk).exists():
                self.slug = self.generate_unique_slug(base_slug=clean)
            else:
                self.slug = clean

        self.full_clean()

        max_retries = 5
        for attempt in range(max_retries):
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                break
            except IntegrityError as exc:
                err_str = str(exc).lower()
                if "slug" in err_str and attempt < max_retries - 1:
                    self.slug = self.generate_unique_slug(base_slug=self.slug)
                    self.full_clean()
                    continue
                raise

    def get_absolute_url(self):
        return reverse("core:product_detail", kwargs={"slug": self.slug})

    def approve(self, admin_user=None, commit=True):
        """Approves product for public storefront catalog display."""
        self.is_approved = True
        self.approval_status = self.APPROVAL_APPROVED
        self.rejection_reason = ""
        if commit:
            self.save(update_fields=["is_approved", "approval_status", "rejection_reason", "updated_at"])

    def reject(self, reason="", admin_user=None, commit=True):
        """Rejects product from public storefront catalog display."""
        self.is_approved = False
        self.approval_status = self.APPROVAL_REJECTED
        if reason:
            self.rejection_reason = reason.strip()
        if commit:
            self.save(update_fields=["is_approved", "approval_status", "rejection_reason", "updated_at"])

    @property
    def has_variants(self):
        """Indicates whether product has at least one active variant."""
        return self.variants.filter(is_active=True).exists()

    @property
    def active_variants(self):
        """Returns active variants queryset ordered by option type and value."""
        return self.variants.filter(is_active=True).order_by("option_type", "option_value", "id")

    @property
    def available_inventory(self):
        """
        Calculates total available inventory without double-counting.
        If product has variants, returns sum of active variant stock.
        Otherwise returns product's own stock_quantity.
        """
        if self.variants.exists():
            return sum(v.stock_quantity for v in self.variants.filter(is_active=True))
        return self.stock_quantity

    @property
    def is_in_stock(self):
        """
        Indicates whether product is available for order.
        If product has variants, returns True if any active variant has stock > 0.
        Otherwise returns True if stock_quantity > 0.
        """
        if self.variants.exists():
            return any(v.stock_quantity > 0 for v in self.variants.filter(is_active=True))
        return self.stock_quantity > 0

    @property
    def min_price(self):
        """
        Returns lowest effective price among active variants, or base price if none.
        """
        active = list(self.variants.filter(is_active=True))
        if active:
            return min(v.effective_price for v in active)
        return self.price

    @property
    def max_price(self):
        """
        Returns highest effective price among active variants, or base price if none.
        """
        active = list(self.variants.filter(is_active=True))
        if active:
            return max(v.effective_price for v in active)
        return self.price

    @property
    def starting_price(self):
        """Alias for min_price to provide starting price for variant products."""
        return self.min_price

    @property
    def has_variable_pricing(self):
        """True if active variants have varying effective prices."""
        active = list(self.variants.filter(is_active=True))
        if len(active) > 1:
            prices = {v.effective_price for v in active}
            return len(prices) > 1
        return False

    @property
    def discount_percentage(self):
        """
        Calculates savings percentage based on compare_at_price and selling price.
        Uses starting_price if active variants exist.
        Returns 0 if compare_at_price is not set or not higher than selling price.
        """
        selling = self.starting_price if self.has_variants else self.price
        compare = self.compare_at_price
        if compare and compare > selling and compare > Decimal("0.00"):
            diff = compare - selling
            return round((diff / compare) * 100)
        return 0

    @property
    def discount_percent(self):
        """Alias for discount_percentage for template compatibility."""
        return self.discount_percentage

    @property
    def title(self):
        """Alias for name for template compatibility."""
        return self.name

    @property
    def average_rating(self):
        """
        Calculates average customer rating from approved reviews only.
        Returns float rounded to 1 decimal place, or 0.0 if no approved reviews exist.
        """
        res = self.reviews.filter(status="approved").aggregate(avg=models.Avg("rating"))["avg"]
        return round(float(res), 1) if res is not None else 0.0

    @property
    def review_count(self):
        """Total count of approved customer reviews."""
        return self.reviews.filter(status="approved").count()

    def get_rating_breakdown(self):
        """
        Returns star distribution summary for approved reviews:
        {
            'average_rating': 4.5,
            'review_count': 12,
            'stars': {
                5: {'count': 8, 'percentage': 67},
                4: {'count': 3, 'percentage': 25},
                3: {'count': 1, 'percentage': 8},
                2: {'count': 0, 'percentage': 0},
                1: {'count': 0, 'percentage': 0},
            }
        }
        """
        approved = self.reviews.filter(status="approved")
        total = approved.count()
        if total == 0:
            return {
                "average_rating": 0.0,
                "review_count": 0,
                "stars": {i: {"count": 0, "percentage": 0} for i in range(5, 0, -1)},
            }
        avg = approved.aggregate(avg=models.Avg("rating"))["avg"] or 0.0
        counts = approved.values("rating").annotate(c=models.Count("id"))
        counts_map = {item["rating"]: item["c"] for item in counts}
        stars = {}
        for i in range(5, 0, -1):
            c = counts_map.get(i, 0)
            stars[i] = {
                "count": c,
                "percentage": round((c / total) * 100) if total else 0,
            }
        return {
            "average_rating": round(float(avg), 1),
            "review_count": total,
            "stars": stars,
        }

    @property
    def seller_name(self):
        """Returns the public brand/store name of the seller, or Fitmeezo for direct items."""
        if self.seller:
            return self.seller.store_name
        return getattr(settings, "BRAND_NAME", "Fitmeezo")

    @property
    def has_complete_dimensions(self) -> bool:
        """True if all four physical measurements are populated and strictly positive."""
        return all(
            getattr(self, f) is not None and getattr(self, f) > Decimal("0.00")
            for f in ["weight_grams", "length_cm", "breadth_cm", "height_cm"]
        )

    @property
    def volumetric_weight_kg(self) -> Decimal | None:
        """Calculates estimated volumetric weight in kg using configurable divisor."""
        from core.shipping_dimensions import calculate_volumetric_weight_kg
        return calculate_volumetric_weight_kg(self.length_cm, self.breadth_cm, self.height_cm)

    @property
    def dimensions_display(self) -> str:
        """Formatted human-readable string of physical measurements."""
        if not self.has_complete_dimensions:
            return "Unknown / Unspecified"
        status_suffix = " (Verified)" if self.is_dimensions_verified else " (Estimated)"
        return f"{self.length_cm} × {self.breadth_cm} × {self.height_cm} cm, {self.weight_grams} g{status_suffix}"




class ProductImage(models.Model):
    """
    Additional showcase gallery images for a product.
    Allows rich multi-angle photography while keeping primary image on the product.
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="images",
        help_text="Product to which this gallery image belongs.",
    )
    image = models.ImageField(
        upload_to="products/gallery/",
        help_text="Product gallery photograph.",
    )
    alt_text = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Descriptive alternate text for accessibility and SEO.",
    )
    display_order = models.PositiveIntegerField(
        default=0,
        db_index=True,
        help_text="Order in which this image appears in product gallery (lowest first).",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when gallery image was uploaded.",
    )

    class Meta:
        ordering = ["display_order", "id"]
        verbose_name = "Product Image"
        verbose_name_plural = "Product Images"

    def __str__(self):
        return f"{self.product.name} Image #{self.id}"


class ProductVariant(models.Model):
    """
    Fitmeezo Product Variant model.
    Represents an individual purchasable option (Size, Color, Storage, Pack, etc.)
    linked to a parent Product, with its own normalized SKU, inventory stock,
    and optional price/compare-at price overrides.
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="variants",
        help_text="Parent product to which this variant belongs.",
    )
    option_type = models.CharField(
        max_length=50,
        help_text="Option type or variant name (e.g. 'Size', 'Color', 'Storage', 'Pack').",
    )
    option_value = models.CharField(
        max_length=100,
        help_text="Option value (e.g. 'M', 'Black', '128GB', 'Pack of 2').",
    )
    sku = models.CharField(
        max_length=60,
        unique=True,
        db_index=True,
        help_text="Stock Keeping Unit (SKU) - unique inventory identifier, normalized to uppercase.",
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional price override in INR. If left blank, parent product price is used.",
    )
    compare_at_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Optional compare-at (M.R.P.) price override in INR. If blank, parent compare-at price is used.",
    )
    stock_quantity = models.PositiveIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Available inventory units for this variant in warehouse (0 marks out of stock).",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Controls whether this variant is active and discoverable/purchasable by customers.",
    )

    # Physical Dimensions & Logistics Overrides (Phase 5 Task 1)
    weight_grams = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Variant-specific dead weight in grams. If null, inherits from parent product.",
    )
    length_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Variant-specific length in cm. If null, inherits from parent product.",
    )
    breadth_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Variant-specific breadth/width in cm. If null, inherits from parent product.",
    )
    height_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Variant-specific height in cm. If null, inherits from parent product.",
    )
    is_dimensions_verified = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if variant dimensions have been verified. False indicates unverified or estimated.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when variant was created.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when variant was last updated.",
    )

    class Meta:
        ordering = ["option_type", "option_value", "id"]
        verbose_name = "Product Variant"
        verbose_name_plural = "Product Variants"
        indexes = [
            models.Index(fields=["product", "is_active"], name="var_prod_active_idx"),
            models.Index(fields=["sku"], name="var_sku_idx"),
            models.Index(fields=["option_type"], name="var_opt_type_idx"),
            models.Index(fields=["created_at"], name="var_created_idx"),
            models.Index(fields=["is_dimensions_verified"], name="var_dim_verified_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "option_type", "option_value"],
                name="unique_product_variant_option",
            ),
            models.CheckConstraint(
                condition=models.Q(price__isnull=True) | models.Q(price__gte=0),
                name="variant_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(compare_at_price__isnull=True) | models.Q(compare_at_price__gte=0),
                name="variant_compare_at_price_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(stock_quantity__gte=0),
                name="variant_stock_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(weight_grams__isnull=True) | models.Q(weight_grams__gt=0),
                name="variant_weight_grams_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(length_cm__isnull=True) | models.Q(length_cm__gt=0),
                name="variant_length_cm_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(breadth_cm__isnull=True) | models.Q(breadth_cm__gt=0),
                name="variant_breadth_cm_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(height_cm__isnull=True) | models.Q(height_cm__gt=0),
                name="variant_height_cm_gt_zero",
            ),
        ]


    def __init__(self, *args, **kwargs):
        # Support 'variant_name' as alias for 'option_type'
        if "variant_name" in kwargs and "option_type" not in kwargs:
            kwargs["option_type"] = kwargs.pop("variant_name")
        super().__init__(*args, **kwargs)

    def __str__(self):
        return f"{self.product.name} - {self.option_type}: {self.option_value} ({self.sku})"

    @property
    def variant_name(self):
        """Alias for option_type."""
        return self.option_type

    @variant_name.setter
    def variant_name(self, value):
        self.option_type = value

    @property
    def effective_price(self):
        """Returns variant price override if set, otherwise parent product price."""
        if self.price is not None:
            return self.price
        return self.product.price

    @property
    def effective_compare_at_price(self):
        """Returns variant compare_at_price override if set, otherwise parent product compare_at_price."""
        if self.compare_at_price is not None:
            return self.compare_at_price
        if self.product_id and hasattr(self, "product") and self.product.compare_at_price:
            # If variant price exceeds parent compare-at price, parent compare-at does not apply
            if self.effective_price and self.product.compare_at_price < self.effective_price:
                return None
            return self.product.compare_at_price
        return None

    @property
    def discount_percentage(self):
        """
        Calculates savings percentage based on effective compare_at_price and effective selling price.
        Returns 0 if effective compare_at_price is not set or not higher than effective selling price.
        """
        eff_compare = self.effective_compare_at_price
        eff_price = self.effective_price
        if eff_compare and eff_price and eff_compare > eff_price and eff_compare > Decimal("0.00"):
            diff = eff_compare - eff_price
            return round((diff / eff_compare) * 100)
        return 0

    @property
    def discount_percent(self):
        """Alias for discount_percentage for template compatibility."""
        return self.discount_percentage

    @property
    def is_in_stock(self):
        """Indicates whether this variant is active and has available inventory units."""
        return self.is_active and self.stock_quantity > 0

    def clean(self):
        super().clean()
        if self.sku:
            self.sku = self.sku.strip().upper()
        if self.option_type:
            self.option_type = self.option_type.strip()
        if self.option_value:
            self.option_value = self.option_value.strip()

        # Validate non-negative values
        if self.price is not None and self.price < Decimal("0.00"):
            raise ValidationError({"price": "Price override cannot be negative."})
        if self.compare_at_price is not None and self.compare_at_price < Decimal("0.00"):
            raise ValidationError({"compare_at_price": "Compare-at price override cannot be negative."})
        if self.stock_quantity is not None and self.stock_quantity < 0:
            raise ValidationError({"stock_quantity": "Stock quantity cannot be negative."})

        # Validate compare-at price against selling price when override is provided
        eff_price = self.effective_price if hasattr(self, "product") and self.product_id else self.price
        if self.compare_at_price is not None and eff_price is not None and self.compare_at_price < eff_price:
            raise ValidationError({
                "compare_at_price": "Compare-at price cannot be lower than the selling price."
            })

        # Avoid duplicate option combinations for the same product
        if self.product_id and self.option_type and self.option_value:
            dup_qs = ProductVariant.objects.filter(
                product_id=self.product_id,
                option_type__iexact=self.option_type,
                option_value__iexact=self.option_value,
            )
            if self.pk:
                dup_qs = dup_qs.exclude(pk=self.pk)
            if dup_qs.exists():
                raise ValidationError(
                    f"A variant with option '{self.option_type}: {self.option_value}' already exists for this product."
                )

        # Validate dimension overrides if provided
        for dim_field in ["weight_grams", "length_cm", "breadth_cm", "height_cm"]:
            val = getattr(self, dim_field)
            if val is not None and val <= Decimal("0.00"):
                label = dim_field.replace("_", " ").capitalize()
                raise ValidationError({dim_field: f"{label} must be greater than zero."})

        # Ensure variant SKU does not collide with a parent product SKU
        if self.sku:
            if Product.objects.filter(sku__iexact=self.sku).exists():
                raise ValidationError({"sku": f"SKU '{self.sku}' is already in use by a product."})

    @property
    def effective_weight_grams(self) -> Decimal | None:
        """Returns variant weight override if set, otherwise parent product weight."""
        if self.weight_grams is not None:
            return self.weight_grams
        if self.product_id and hasattr(self, "product"):
            return self.product.weight_grams
        return None

    @property
    def effective_length_cm(self) -> Decimal | None:
        """Returns variant length override if set, otherwise parent product length."""
        if self.length_cm is not None:
            return self.length_cm
        if self.product_id and hasattr(self, "product"):
            return self.product.length_cm
        return None

    @property
    def effective_breadth_cm(self) -> Decimal | None:
        """Returns variant breadth override if set, otherwise parent product breadth."""
        if self.breadth_cm is not None:
            return self.breadth_cm
        if self.product_id and hasattr(self, "product"):
            return self.product.breadth_cm
        return None

    @property
    def effective_height_cm(self) -> Decimal | None:
        """Returns variant height override if set, otherwise parent product height."""
        if self.height_cm is not None:
            return self.height_cm
        if self.product_id and hasattr(self, "product"):
            return self.product.height_cm
        return None

    @property
    def effective_is_dimensions_verified(self) -> bool:
        """Returns True if variant's own or inherited parent measurements are verified."""
        if any(d is not None for d in [self.weight_grams, self.length_cm, self.breadth_cm, self.height_cm]):
            return self.is_dimensions_verified
        if self.product_id and hasattr(self, "product"):
            return self.product.is_dimensions_verified
        return False

    @property
    def has_complete_dimensions(self) -> bool:
        """True if all four effective physical measurements are present and strictly positive."""
        effs = [self.effective_weight_grams, self.effective_length_cm, self.effective_breadth_cm, self.effective_height_cm]
        return all(d is not None and d > Decimal("0.00") for d in effs)

    @property
    def volumetric_weight_kg(self) -> Decimal | None:
        """Calculates estimated volumetric weight in kg from effective dimensions."""
        from core.shipping_dimensions import calculate_volumetric_weight_kg
        return calculate_volumetric_weight_kg(
            self.effective_length_cm,
            self.effective_breadth_cm,
            self.effective_height_cm,
        )

    @property
    def dimensions_display(self) -> str:
        """Formatted human-readable string of effective physical measurements."""
        if not self.has_complete_dimensions:
            return "Unknown / Unspecified"
        status_suffix = " (Verified)" if self.effective_is_dimensions_verified else " (Estimated)"
        return f"{self.effective_length_cm} × {self.effective_breadth_cm} × {self.effective_height_cm} cm, {self.effective_weight_grams} g{status_suffix}"


    def save(self, *args, **kwargs):
        if self.sku:
            self.sku = self.sku.strip().upper()
        if self.option_type:
            self.option_type = self.option_type.strip()
        if self.option_value:
            self.option_value = self.option_value.strip()
        self.full_clean()
        super().save(*args, **kwargs)


class Cart(models.Model):
    """
    Fitmeezo Persistent Shopping Cart model.
    Holds persistent cart state for authenticated customer accounts,
    while anonymous carts utilize the Django session store.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="carts",
        help_text="Registered customer account, if authenticated.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when the cart was initiated.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when the cart contents were last modified.",
    )

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Shopping Cart"
        verbose_name_plural = "Shopping Carts"

    def __str__(self):
        if self.user:
            return f"Cart #{self.pk} ({self.user.username})"
        return f"Cart #{self.pk} (Guest)"


class CartItem(models.Model):
    """
    Individual item line in a persistent Shopping Cart.
    Links to a product and an optional product variant with a guaranteed positive quantity.
    """

    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="items",
        help_text="Parent shopping cart.",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="cart_items",
        help_text="Selected catalogue product.",
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="cart_items",
        help_text="Selected product variant, if applicable.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Purchasable units (must be at least 1).",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when item was placed in cart.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when item quantity was last updated.",
    )

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Cart Item"
        verbose_name_plural = "Cart Items"
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product", "variant"],
                name="unique_cart_product_variant",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="cartitem_quantity_gte_1",
            ),
        ]

    def __str__(self):
        var_text = f" - {self.variant.option_value}" if self.variant else ""
        return f"{self.product.name}{var_text} (x{self.quantity})"

    def clean(self):
        super().clean()
        if self.quantity < 1:
            raise ValidationError({"quantity": "Cart item quantity must be at least 1."})
        if self.variant and self.product_id and self.variant.product_id != self.product_id:
            raise ValidationError({"variant": "Selected variant does not belong to the selected product."})


class CustomerProfile(models.Model):
    """
    Profile extension for registered Fitmeezo customers.
    Stores optional contact phone number and customer metadata
    without replacing Django's standard User model.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        help_text="Linked Django user account.",
    )
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Primary customer contact phone number.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when customer profile was created.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when customer profile was last updated.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Customer Profile"
        verbose_name_plural = "Customer Profiles"

    def __str__(self):
        return f"Profile for {self.user.username}"

    @property
    def full_name(self):
        name = f"{self.user.first_name} {self.user.last_name}".strip()
        return name if name else self.user.username


class Address(models.Model):
    """
    Saved customer shipping and delivery addresses.
    Prepares address management for checkout with default address uniqueness.
    """
    ADDRESS_TYPE_CHOICES = [
        ("home", "Home"),
        ("work", "Work"),
        ("other", "Other"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="addresses",
        help_text="Customer owning this address.",
    )
    full_name = models.CharField(
        max_length=100,
        help_text="Recipient's full name for courier delivery.",
    )
    phone_number = models.CharField(
        max_length=20,
        help_text="Recipient's 10-digit mobile number for delivery updates.",
    )
    address_line1 = models.CharField(
        max_length=255,
        help_text="Flat, House no., Building, Company, Apartment.",
    )
    address_line2 = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Area, Colony, Street, Sector, Village.",
    )
    city = models.CharField(
        max_length=100,
        help_text="Town / City.",
    )
    state = models.CharField(
        max_length=100,
        help_text="State / Union Territory.",
    )
    postal_code = models.CharField(
        max_length=20,
        help_text="PIN code / Postal code (e.g. 6-digit Indian PIN code).",
    )
    landmark = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Optional landmark (e.g. Near Metro Station).",
    )
    address_type = models.CharField(
        max_length=20,
        choices=ADDRESS_TYPE_CHOICES,
        default="home",
        help_text="Category label for address.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Indicates whether this is the customer's primary default address.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when address was added.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when address was last modified.",
    )

    class Meta:
        ordering = ["-is_default", "-updated_at"]
        verbose_name = "Saved Address"
        verbose_name_plural = "Saved Addresses"

    def __str__(self):
        return f"{self.full_name} ({self.city}, {self.postal_code})"

    def clean(self):
        super().clean()
        if self.full_name:
            self.full_name = self.full_name.strip()
        if self.phone_number:
            self.phone_number = self.phone_number.strip()
        if self.address_line1:
            self.address_line1 = self.address_line1.strip()
        if self.city:
            self.city = self.city.strip()
        if self.state:
            self.state = self.state.strip()
        if self.postal_code:
            code = self.postal_code.strip()
            self.postal_code = code
            # Validation:
            # If purely digits of length 6: validate Indian PIN code (cannot start with 0)
            if code.isdigit() and len(code) == 6:
                if code.startswith("0"):
                    raise ValidationError({"postal_code": "Indian PIN codes cannot start with 0."})
            elif not re.match(r"^[A-Za-z0-9\s\-]{3,12}$", code):
                raise ValidationError({"postal_code": "Please enter a valid postal code (e.g. 6-digit Indian PIN code)."})

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.is_default:
            # Unset is_default on other addresses belonging to this user
            Address.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        else:
            # If this is the user's only address, automatically make it default
            if not Address.objects.filter(user=self.user).exclude(pk=self.pk).exists():
                self.is_default = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        was_default = self.is_default
        user = self.user
        super().delete(*args, **kwargs)
        if was_default:
            # Safely promote the most recently updated remaining address to default
            next_addr = Address.objects.filter(user=user).order_by("-updated_at").first()
            if next_addr:
                next_addr.is_default = True
                next_addr.save(update_fields=["is_default"])


class ProductReview(models.Model):
    """
    Customer Product Review and Rating model.
    Stores verified 1-5 star ratings, feedback title, comment,
    moderation status, and enforces single-review-per-customer-per-product.
    """
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Moderation"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="reviews",
        help_text="Product being reviewed.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="product_reviews",
        help_text="Customer who authored this review.",
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Rating score from 1 to 5 stars.",
    )
    title = models.CharField(
        max_length=200,
        help_text="Review headline or summary.",
    )
    comment = models.TextField(
        help_text="Detailed customer review comments.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_APPROVED,
        db_index=True,
        help_text="Moderation status (approved reviews are visible publicly).",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when review was submitted.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when review was last edited.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Product Review"
        verbose_name_plural = "Product Reviews"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "user"],
                name="unique_product_user_review",
            ),
            models.CheckConstraint(
                condition=models.Q(rating__gte=1, rating__lte=5),
                name="review_rating_1_to_5",
            ),
        ]
        indexes = [
            models.Index(fields=["product", "status"], name="review_prod_status_idx"),
            models.Index(fields=["created_at"], name="review_created_idx"),
            models.Index(fields=["user"], name="review_user_idx"),
        ]

    def __str__(self):
        return f"{self.rating}★ review by {self.customer_display_name} for {self.product.name}"

    def clean(self):
        super().clean()
        if self.rating is not None and (self.rating < 1 or self.rating > 5):
            raise ValidationError({"rating": "Rating must be between 1 and 5 stars."})
        if self.title:
            self.title = self.title.strip()
        if self.comment:
            self.comment = self.comment.strip()

    @property
    def customer_display_name(self):
        """
        Returns a privacy-conscious display name for customer privacy.
        Examples:
        - If user has first_name 'Rahul' and last_name 'Sharma': 'Rahul S.'
        - If user has first_name 'Rahul' only: 'Rahul'
        - If user has only username 'user123': 'user123'
        - If username contains email '@': 't***t' or 'Customer'
        """
        fn = (self.user.first_name or "").strip()
        ln = (self.user.last_name or "").strip()
        if fn and ln:
            return f"{fn} {ln[0].upper()}."
        if fn:
            return fn
        u = (self.user.username or "").strip()
        if "@" in u:
            prefix = u.split("@")[0]
            if len(prefix) > 2:
                return f"{prefix[0]}***{prefix[-1]}"
            return "Customer"
        return u

    @property
    def is_approved(self):
        return self.status == self.STATUS_APPROVED


class WishlistItem(models.Model):
    """
    Customer Wishlist model.
    Tracks products bookmarked/favorited by an authenticated customer.
    Enforces user-product uniqueness at the database level.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wishlist_items",
        help_text="Customer who saved this item.",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="wishlist_items",
        help_text="Product saved to customer's wishlist.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when item was added to wishlist.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Wishlist Item"
        verbose_name_plural = "Wishlist Items"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "product"],
                name="unique_user_product_wishlist",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "created_at"], name="wishlist_user_created_idx"),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.product.name}"


class Order(models.Model):
    """
    Fitmeezo Customer Order model.
    Encapsulates final customer purchase records with immutable address snapshots,
    financial totals (subtotal, shipping, discount, final amount), order status,
    and payment tracking (e.g. Cash on Delivery - unpaid until collected).
    """

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_SHIPPED = "shipped"
    STATUS_DELIVERED = "delivered"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_SHIPPED, "Shipped"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    PAYMENT_METHOD_COD = "cod"
    PAYMENT_METHOD_RAZORPAY = "razorpay"

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_METHOD_COD, "Cash on Delivery (COD)"),
        (PAYMENT_METHOD_RAZORPAY, "Online Payment (Razorpay)"),
    ]

    PAYMENT_STATUS_UNPAID = "unpaid"
    PAYMENT_STATUS_PAID = "paid"
    PAYMENT_STATUS_FAILED = "failed"
    PAYMENT_STATUS_REFUNDED = "refunded"

    PAYMENT_STATUS_CHOICES = [
        (PAYMENT_STATUS_UNPAID, "Unpaid"),
        (PAYMENT_STATUS_PAID, "Paid"),
        (PAYMENT_STATUS_FAILED, "Failed"),
        (PAYMENT_STATUS_REFUNDED, "Refunded"),
    ]

    REFUND_STATUS_NOT_APPLICABLE = "not_applicable"
    REFUND_STATUS_PENDING = "pending"
    REFUND_STATUS_PROCESSED = "processed"
    REFUND_STATUS_FAILED = "failed"

    REFUND_STATUS_CHOICES = [
        (REFUND_STATUS_NOT_APPLICABLE, "Not Applicable"),
        (REFUND_STATUS_PENDING, "Refund Required (Pending)"),
        (REFUND_STATUS_PROCESSED, "Refund Processed"),
        (REFUND_STATUS_FAILED, "Refund Failed"),
    ]

    order_number = models.CharField(
        max_length=32,
        unique=True,
        db_index=True,
        help_text="Unique customer-facing order identifier (e.g. FM-20261010-AB12).",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="orders",
        help_text="Customer who placed this order.",
    )

    # Address snapshot fields (immutable copy of delivery destination at purchase time)
    shipping_full_name = models.CharField(
        max_length=100,
        help_text="Recipient's full name snapshot.",
    )
    shipping_phone_number = models.CharField(
        max_length=20,
        help_text="Recipient's contact phone number snapshot.",
    )
    shipping_address_line1 = models.CharField(
        max_length=255,
        help_text="Primary street / building address snapshot.",
    )
    shipping_address_line2 = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Secondary locality / landmark snapshot.",
    )
    shipping_city = models.CharField(
        max_length=100,
        help_text="City snapshot.",
    )
    shipping_state = models.CharField(
        max_length=100,
        help_text="State / Territory snapshot.",
    )
    shipping_postal_code = models.CharField(
        max_length=20,
        help_text="PIN / Postal code snapshot.",
    )
    shipping_landmark = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Delivery landmark snapshot.",
    )
    shipping_address_type = models.CharField(
        max_length=20,
        default="home",
        help_text="Address type snapshot (e.g. home, work).",
    )

    # Financial breakdown
    subtotal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Sum of line item totals in INR.",
    )
    shipping_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Shipping charge in INR (Free for orders >= ₹499).",
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Total applied discounts in INR.",
    )
    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Final payable total in INR (subtotal + shipping_fee - discount).",
    )
    currency = models.CharField(
        max_length=10,
        default="INR",
        help_text="Currency code (e.g. INR).",
    )

    # Order & Payment state
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="Fulfillment status.",
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default=PAYMENT_METHOD_COD,
        help_text="Payment method selected.",
    )
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default=PAYMENT_STATUS_UNPAID,
        db_index=True,
        help_text="Payment state (unpaid until collected at delivery for COD).",
    )

    # Razorpay Online Payment tracking
    razorpay_order_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Razorpay Order ID created on server (e.g. order_xyz123).",
    )
    razorpay_payment_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Razorpay Payment ID returned upon verified settlement (e.g. pay_abc456).",
    )
    razorpay_signature = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Cryptographic payment signature verified on server.",
    )
    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when payment was confirmed as paid.",
    )

    # Deduplication & Notes
    idempotency_key = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
        help_text="Form submission token to prevent duplicate order placement.",
    )
    customer_notes = models.TextField(
        blank=True,
        default="",
        help_text="Optional delivery instructions or notes from customer.",
    )

    # Order Cancellation & Stock Tracking
    inventory_restored = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Tracks whether product stock was restored to catalogue inventory upon cancellation.",
    )
    refund_status = models.CharField(
        max_length=20,
        choices=REFUND_STATUS_CHOICES,
        default=REFUND_STATUS_NOT_APPLICABLE,
        db_index=True,
        help_text="Refund fulfillment state for cancelled paid orders.",
    )
    cancelled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the order was cancelled.",
    )
    cancellation_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Customer or administrative reason provided for cancellation.",
    )
    cancelled_by = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Entity who initiated cancellation ('customer' or 'staff').",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when order was placed.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when order was last updated.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Order"
        verbose_name_plural = "Orders"
        indexes = [
            models.Index(fields=["user", "-created_at"], name="order_user_created_idx"),
            models.Index(fields=["status", "-created_at"], name="order_status_created_idx"),
            models.Index(fields=["payment_status"], name="order_payment_status_idx"),
            models.Index(fields=["razorpay_order_id"], name="order_rp_order_idx"),
            models.Index(fields=["razorpay_payment_id"], name="order_rp_payment_idx"),
        ]

    def __str__(self):
        return f"Order #{self.order_number} ({self.user.username}) - ₹{self.total_amount}"

    def get_absolute_url(self):
        return reverse("core:order_detail", kwargs={"order_number": self.order_number})

    def clean(self):
        super().clean()
        if self.payment_status == self.PAYMENT_STATUS_PAID:
            if self.payment_method == self.PAYMENT_METHOD_RAZORPAY and not self.razorpay_payment_id:
                raise ValidationError({
                    "payment_status": "Online (Razorpay) orders cannot be marked as Paid without a verified Razorpay Payment ID."
                })

    def mark_as_paid(self, payment_id=None, signature=None):
        """
        Safely transitions order payment status to paid with verified payment attributes.
        """
        self.payment_status = self.PAYMENT_STATUS_PAID
        self.paid_at = timezone.now()
        if payment_id:
            self.razorpay_payment_id = payment_id
        if signature:
            self.razorpay_signature = signature
        if self.status == self.STATUS_PENDING:
            self.status = self.STATUS_PROCESSING
        self.save(update_fields=[
            "payment_status", "paid_at", "razorpay_payment_id",
            "razorpay_signature", "status", "updated_at"
        ])

    @property
    def total_items_count(self):
        return sum(item.quantity for item in self.items.all())

    @property
    def shipping_address_formatted(self):
        parts = [self.shipping_address_line1]
        if self.shipping_address_line2:
            parts.append(self.shipping_address_line2)
        if self.shipping_landmark:
            parts.append(f"Landmark: {self.shipping_landmark}")
        parts.append(f"{self.shipping_city}, {self.shipping_state} - {self.shipping_postal_code}")
        return ", ".join(parts)

    @property
    def is_cancellable(self):
        """
        Indicates whether the order is currently eligible for customer or administrative cancellation.
        Allowed in Pending and Processing states; disallowed once Shipped, Delivered, or already Cancelled.
        """
        return self.status in [self.STATUS_PENDING, self.STATUS_PROCESSING]

    @property
    def requires_refund(self):
        """
        Indicates whether a cancelled order was paid online and requires manual or gateway refund settlement.
        """
        return (
            self.status == self.STATUS_CANCELLED
            and self.payment_status == self.PAYMENT_STATUS_PAID
            and self.refund_status == self.REFUND_STATUS_PENDING
        )

    @property
    def has_return_eligible_items(self) -> bool:
        """True if at least one delivered package in this order has return-eligible items."""
        return any(pkg.is_returnable for pkg in self.seller_orders.all())

    @property
    def return_requests_list(self):
        """Returns all return requests associated with this order."""
        return self.return_requests.select_related("seller_order", "seller").all().order_by("-created_at")


class SellerOrder(models.Model):
    """
    Fitmeezo Seller Sub-Order / Package model.
    Represents an independent fulfillment package within a parent customer Order.
    Enables independent seller dispatch workflows, parcel tracking, and financial snapshots.
    """

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_READY_TO_SHIP = "ready_to_ship"
    STATUS_SHIPPED = "shipped"
    STATUS_DELIVERED = "delivered"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Acknowledgment"),
        (STATUS_PROCESSING, "Processing / Packing"),
        (STATUS_READY_TO_SHIP, "Ready for Dispatch"),
        (STATUS_SHIPPED, "Shipped"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    # Linkages
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="seller_orders",
        help_text="Parent customer order.",
    )
    seller = models.ForeignKey(
        "Seller",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="seller_orders",
        help_text="Fulfilling merchant. Null indicates platform-owned/direct merchandise.",
    )
    is_platform_fulfilled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if this package contains direct platform merchandise (seller=None).",
    )
    sub_order_number = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Distinct sub-order / package identifier (e.g. FM-20261011-XXXX-S1 or FM-20261011-XXXX-PLT).",
    )

    # Fulfillment Status (Independent of parent Order.payment_status)
    status = models.CharField(
        max_length=25,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="Package dispatch and delivery status.",
    )

    # Merchandise Financials (Snapshotted via exact Decimal arithmetic)
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Gross merchandise value of items belonging to this package in INR.",
    )
    total_commission = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Total marketplace commission retained by Fitmeezo for this package in INR.",
    )
    net_seller_payable = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Gross subtotal minus total commission (before logistics adjustments) in INR.",
    )

    # Separate Shipping Concepts (Phase 5 Approved Business Rules)
    customer_shipping_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Allocated pro-rata share of customer-paid shipping fee (Order.shipping_fee) in INR, weighted by merchandise subtotal.",
    )
    actual_shipping_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Actual logistics expense billed by courier/Shiprocket for this package in INR (reconciled post-dispatch; ₹0.00 until verified).",
    )
    shipping_allocated_to_seller = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Portion of actual courier cost allocated to/deducted from seller payout in INR. Represents seller logistics liability, NOT customer revenue.",
    )
    shipping_margin = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Platform logistics variance in INR: customer_shipping_charge - actual_shipping_cost (gross shipping cost variance; not net platform profit).",
    )

    # Courier and Tracking
    courier_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Logistics carrier name (e.g. Blue Dart, Delhivery).",
    )
    tracking_number = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Shipment tracking / AWB number.",
    )
    tracking_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text="Public carrier or Shiprocket tracking URL for the package.",
    )

    # Shiprocket Provider Identifiers (Storage only; populated on future provider booking)
    shiprocket_order_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Shiprocket internal order identifier.",
    )
    shiprocket_shipment_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Shiprocket internal shipment identifier.",
    )
    shiprocket_awb_code = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Air Waybill (AWB) tracking number assigned by courier via Shiprocket.",
    )

    # Package Physical Dimensions & Verification (Phase 5 Tasks 2 & 3)
    PACKAGE_DIMENSION_UNKNOWN = "unknown"
    PACKAGE_DIMENSION_ESTIMATED = "estimated"
    PACKAGE_DIMENSION_VERIFIED = "verified"

    PACKAGE_DIMENSION_STATUS_CHOICES = [
        (PACKAGE_DIMENSION_UNKNOWN, "Unknown / Missing Measurements"),
        (PACKAGE_DIMENSION_ESTIMATED, "Estimated (Cart Aggregate / Bounding Box)"),
        (PACKAGE_DIMENSION_VERIFIED, "Verified (Staff Measured at Packing)"),
    ]

    package_dimension_status = models.CharField(
        max_length=20,
        choices=PACKAGE_DIMENSION_STATUS_CHOICES,
        default=PACKAGE_DIMENSION_UNKNOWN,
        db_index=True,
        help_text="Verification tier of parcel physical measurements.",
    )

    # Estimated / Checkout Snapshot Dimensions
    estimated_weight_grams = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Estimated dead weight of parcel in grams from item snapshots.",
    )
    estimated_length_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Estimated bounding box length in cm.",
    )
    estimated_breadth_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Estimated bounding box breadth in cm.",
    )
    estimated_height_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Estimated stacked height in cm.",
    )

    # Actual / Verified Packed Measurements (Recorded by authorized warehouse staff)
    actual_weight_grams = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Verified actual physical scale weight in grams recorded at packing.",
    )
    actual_length_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Verified packed carton length in cm.",
    )
    actual_breadth_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Verified packed carton breadth in cm.",
    )
    actual_height_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Verified packed carton height in cm.",
    )
    dimensions_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when actual packed parcel measurements were verified.",
    )
    dimensions_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="verified_seller_orders",
        help_text="Staff or seller user who measured and verified packed parcel.",
    )

    # Operational Timestamps
    shipped_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when parcel was dispatched.")
    delivered_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when parcel was delivered.")
    cancelled_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when package was cancelled.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Seller Sub-Order"
        verbose_name_plural = "Seller Sub-Orders"
        indexes = [
            models.Index(fields=["seller", "status"], name="seller_order_status_idx"),
            models.Index(fields=["order", "seller"], name="order_seller_suborder_idx"),
            models.Index(fields=["package_dimension_status"], name="seller_order_dim_status_idx"),
        ]

        constraints = [
            models.UniqueConstraint(
                fields=["order", "seller"],
                condition=models.Q(seller__isnull=False),
                name="unique_order_seller_suborder",
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(is_platform_fulfilled=True),
                name="unique_order_platform_suborder",
            ),
        ]

    def __str__(self):
        vendor = self.seller.store_name if self.seller else "Platform Store"
        return f"{self.sub_order_number} ({vendor}) - {self.get_status_display()}"

    @property
    def awb_code(self):
        """Returns provider AWB code or falls back to tracking number."""
        return self.shiprocket_awb_code or self.tracking_number

    @awb_code.setter
    def awb_code(self, value):
        self.shiprocket_awb_code = value or ""

    @property
    def refundable_shipping_amount(self) -> Decimal:
        """
        The maximum allocated customer shipping charge eligible for refund
        if this package is cancelled due to seller fault, subject to customer refund policy.
        """
        return self.customer_shipping_charge

    @property
    def total_amount(self) -> Decimal:
        """Alias for subtotal, aligning with financial report and ledger interfaces."""
        return self.subtotal

    @property
    def commission_amount(self) -> Decimal:
        """Alias for total_commission, aligning with ledger / commission analytics."""
        return self.total_commission

    @property
    def seller_payout_amount(self) -> Decimal:
        """Alias for net_seller_payable."""
        return self.net_seller_payable

    def calculate_estimated_seller_payable(self) -> Decimal:
        """
        Accounting formula for seller payable separation:
            Eligible Payable = Merchandise Subtotal - Platform Commission - Reconciled Courier Expense Allocated to Seller

        Safety & Accounting Principles:
        - Commission is computed strictly on merchandise subtotal snapshots, NEVER on customer shipping revenue.
        - Actual courier expense (shipping_allocated_to_seller) is deducted ONLY after it is verified and reconciled.
          If courier cost is unknown (₹0.00), no courier expense is deducted.
        - Courier expense is never deducted twice.
        - Cancelled packages have an eligible payable of ₹0.00.
        - This provides a financial calculation snapshot; it does not represent a finalized or disbursed payout ledger.
        """
        if self.status == self.STATUS_CANCELLED:
            return Decimal("0.00")

        base_payable = self.subtotal - self.total_commission
        if self.shipping_allocated_to_seller and self.shipping_allocated_to_seller > Decimal("0.00"):
            base_payable -= self.shipping_allocated_to_seller

        return max(Decimal("0.00"), base_payable).quantize(Decimal("0.01"))

    def get_effective_measurements(self) -> dict:
        """
        Returns active physical measurements for this parcel.
        Prioritizes verified actual packed measurements when recorded;
        otherwise falls back to estimated bounding-box calculations.
        """
        if (
            self.package_dimension_status == self.PACKAGE_DIMENSION_VERIFIED
            and self.actual_weight_grams is not None
            and self.actual_length_cm is not None
            and self.actual_breadth_cm is not None
            and self.actual_height_cm is not None
        ):
            return {
                "weight_grams": self.actual_weight_grams,
                "length_cm": self.actual_length_cm,
                "breadth_cm": self.actual_breadth_cm,
                "height_cm": self.actual_height_cm,
                "is_verified": True,
                "status": self.PACKAGE_DIMENSION_VERIFIED,
            }
        return {
            "weight_grams": self.estimated_weight_grams,
            "length_cm": self.estimated_length_cm,
            "breadth_cm": self.estimated_breadth_cm,
            "height_cm": self.estimated_height_cm,
            "is_verified": False,
            "status": self.package_dimension_status,
        }

    def record_verified_measurements(
        self,
        weight_grams: Decimal | str | float,
        length_cm: Decimal | str | float,
        breadth_cm: Decimal | str | float,
        height_cm: Decimal | str | float,
        user=None,
        commit: bool = True,
    ):
        """
        Safe method for authorized staff or sellers to record actual packed measurements
        at fulfillment time (Tasks 2 & 6).
        """
        try:
            w = Decimal(str(weight_grams))
            l = Decimal(str(length_cm))
            b = Decimal(str(breadth_cm))
            h = Decimal(str(height_cm))
        except Exception as exc:
            raise ValidationError(f"Invalid decimal format in packed measurements: {exc}")

        for name, val in [("weight_grams", w), ("length_cm", l), ("breadth_cm", b), ("height_cm", h)]:
            if val <= Decimal("0.00"):
                label = name.replace("_", " ").capitalize()
                raise ValidationError({name: f"{label} must be greater than zero."})

        self.actual_weight_grams = w
        self.actual_length_cm = l
        self.actual_breadth_cm = b
        self.actual_height_cm = h
        self.package_dimension_status = self.PACKAGE_DIMENSION_VERIFIED
        self.dimensions_verified_at = timezone.now()
        if user and user.is_authenticated:
            self.dimensions_verified_by = user

        if commit:
            self.save(update_fields=[
                "actual_weight_grams",
                "actual_length_cm",
                "actual_breadth_cm",
                "actual_height_cm",
                "package_dimension_status",
                "dimensions_verified_at",
                "dimensions_verified_by",
                "updated_at",
            ])

    def get_dead_weight_kg(self) -> Decimal | None:
        """Dead weight in kg derived from effective parcel measurements."""
        from core.shipping_dimensions import grams_to_kg
        eff = self.get_effective_measurements()
        return grams_to_kg(eff.get("weight_grams"))

    def get_volumetric_weight_kg(self, divisor: Decimal | None = None) -> Decimal | None:
        """
        Volumetric weight in kg derived from effective parcel measurements
        using the configured or provided provider divisor (Task 3).
        """
        from core.shipping_dimensions import calculate_volumetric_weight_kg
        eff = self.get_effective_measurements()
        return calculate_volumetric_weight_kg(
            eff.get("length_cm"),
            eff.get("breadth_cm"),
            eff.get("height_cm"),
            divisor=divisor,
        )

    def get_chargeable_weight_kg(self, divisor: Decimal | None = None) -> Decimal | None:
        """
        Estimated chargeable weight in kg: max(dead_weight_kg, volumetric_weight_kg).
        Returns None if dimensions or weights are missing/unknown.
        """
        from core.shipping_dimensions import calculate_chargeable_weight_kg
        return calculate_chargeable_weight_kg(
            self.get_dead_weight_kg(),
            self.get_volumetric_weight_kg(divisor=divisor),
        )

    def get_pickup_location_details(self) -> dict:
        """
        Returns registered warehouse or pickup location for this package (Approved Rule 3).
        - Third-party sellers use their own registered pickup location.
        - Platform-owned merchandise uses the platform's configured warehouse.
        """
        if not self.is_platform_fulfilled and self.seller:
            return self.seller.validate_pickup_location()
        from core.shipping_dimensions import validate_platform_pickup_location
        return validate_platform_pickup_location()

    def clean(self):
        super().clean()
        if self.is_platform_fulfilled and self.seller is not None:
            raise ValidationError({
                "seller": "Platform-fulfilled sub-orders cannot have an assigned third-party seller."
            })
        if not self.is_platform_fulfilled and self.seller is None:
            raise ValidationError({
                "seller": "Seller must be specified when is_platform_fulfilled is False."
            })
        if self.subtotal is not None and self.subtotal < Decimal("0.00"):
            raise ValidationError({"subtotal": "Subtotal cannot be negative."})
        if self.total_commission is not None and self.total_commission < Decimal("0.00"):
            raise ValidationError({"total_commission": "Total commission cannot be negative."})
        if self.net_seller_payable is not None and self.net_seller_payable < Decimal("0.00"):
            raise ValidationError({"net_seller_payable": "Net seller payable cannot be negative."})
        if self.customer_shipping_charge is not None and self.customer_shipping_charge < Decimal("0.00"):
            raise ValidationError({"customer_shipping_charge": "Customer shipping charge cannot be negative."})
        if self.actual_shipping_cost is not None and self.actual_shipping_cost < Decimal("0.00"):
            raise ValidationError({"actual_shipping_cost": "Actual shipping cost cannot be negative."})
        if self.shipping_allocated_to_seller is not None and self.shipping_allocated_to_seller < Decimal("0.00"):
            raise ValidationError({"shipping_allocated_to_seller": "Shipping allocated to seller cannot be negative."})

        # Validate estimated dimensions if specified
        for dim_field in ["estimated_weight_grams", "estimated_length_cm", "estimated_breadth_cm", "estimated_height_cm"]:
            val = getattr(self, dim_field)
            if val is not None and val <= Decimal("0.00"):
                label = dim_field.replace("_", " ").capitalize()
                raise ValidationError({dim_field: f"{label} must be greater than zero."})

        # Validate actual dimensions if specified
        for dim_field in ["actual_weight_grams", "actual_length_cm", "actual_breadth_cm", "actual_height_cm"]:
            val = getattr(self, dim_field)
            if val is not None and val <= Decimal("0.00"):
                label = dim_field.replace("_", " ").capitalize()
                raise ValidationError({dim_field: f"{label} must be greater than zero."})


        # Validate duplicate prevention in clean()
        if self.order_id and self.seller_id and not self.is_platform_fulfilled:
            qs = SellerOrder.objects.filter(order=self.order, seller=self.seller)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("A sub-order for this seller already exists in this parent order.")

        if self.order_id and self.is_platform_fulfilled:
            qs = SellerOrder.objects.filter(order=self.order, is_platform_fulfilled=True)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("A platform-fulfilled sub-order already exists in this parent order.")

    def mark_shipped(
        self,
        courier_name="",
        tracking_number="",
        tracking_url="",
        shiprocket_awb_code="",
        shiprocket_order_id="",
        shiprocket_shipment_id="",
        commit=True,
    ):
        self.status = self.STATUS_SHIPPED
        self.shipped_at = timezone.now()
        if courier_name:
            self.courier_name = courier_name
        if tracking_number:
            self.tracking_number = tracking_number
        if tracking_url:
            self.tracking_url = tracking_url
        if shiprocket_awb_code:
            self.shiprocket_awb_code = shiprocket_awb_code
            if not self.tracking_number:
                self.tracking_number = shiprocket_awb_code
        if shiprocket_order_id:
            self.shiprocket_order_id = shiprocket_order_id
        if shiprocket_shipment_id:
            self.shiprocket_shipment_id = shiprocket_shipment_id
        if commit:
            self.save(update_fields=[
                "status", "shipped_at", "courier_name", "tracking_number",
                "tracking_url", "shiprocket_awb_code", "shiprocket_order_id",
                "shiprocket_shipment_id", "updated_at",
            ])

    def mark_delivered(self, commit=True):
        self.status = self.STATUS_DELIVERED
        self.delivered_at = timezone.now()
        if commit:
            self.save(update_fields=["status", "delivered_at", "updated_at"])

    def cancel_package(self, commit=True):
        self.status = self.STATUS_CANCELLED
        self.cancelled_at = timezone.now()
        if commit:
            self.save(update_fields=["status", "cancelled_at", "updated_at"])

    @property
    def is_booked(self) -> bool:
        """True if this package has a recorded mock or live shipment booking."""
        try:
            return bool(self.shipment_booking)
        except Exception:
            return False

    @property
    def is_returnable(self) -> bool:
        """True if delivered, has a delivery timestamp, and has at least one return-eligible item."""
        if self.status != self.STATUS_DELIVERED:
            return False
        delivered_date = self.delivered_at or self.updated_at
        if not delivered_date:
            return False
        return any(item.is_return_eligible for item in self.items.all())



class OrderItem(models.Model):
    """
    Individual merchandise line within a finalized Order.
    Stores immutable snapshots of product name, variant option label, SKU,
    and effective unit selling price at the moment of order placement.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
        help_text="Parent customer order.",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_items",
        help_text="Linked catalogue product (may be null if product is later removed).",
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_items",
        help_text="Linked product variant option, if applicable.",
    )

    # Line item snapshots
    product_name = models.CharField(
        max_length=200,
        help_text="Snapshot of product title.",
    )
    variant_label = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Snapshot of variant attributes (e.g. 'Size: M').",
    )
    sku = models.CharField(
        max_length=60,
        blank=True,
        default="",
        help_text="Snapshot of inventory SKU.",
    )
    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Price charged per unit in INR at the time of purchase.",
    )
    compare_at_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Snapshot of compare-at / M.R.P. price in INR, if applicable.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Units purchased (minimum 1).",
    )
    line_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Total for this line item in INR (unit_price * quantity).",
    )

    # Seller & Commission snapshots (Phase 1 reliable snapshot mechanism)
    seller = models.ForeignKey(
        "Seller",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_items",
        help_text="Snapshot of the seller/vendor associated with this order line item.",
    )
    seller_order = models.ForeignKey(
        "SellerOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
        help_text="Associated seller sub-order package.",
    )
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        help_text="Snapshot of marketplace commission percentage at purchase time.",
    )
    commission_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Calculated marketplace commission in INR (line_total * commission_rate / 100).",
    )

    # Physical Dimension Snapshots (Phase 5 Task 2 Immutable Order-Time Snapshots)
    weight_grams = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Snapshot of unit dead weight in grams at checkout time. Null for legacy orders.",
    )
    length_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Snapshot of unit length in cm at checkout time. Null for legacy orders.",
    )
    breadth_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Snapshot of unit breadth/width in cm at checkout time. Null for legacy orders.",
    )
    height_cm = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Snapshot of unit height in cm at checkout time. Null for legacy orders.",
    )
    is_dimensions_verified = models.BooleanField(
        default=False,
        help_text="Snapshot of whether dimensions were verified at checkout time.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="Timestamp when order line item was created.",
    )

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Order Item"
        verbose_name_plural = "Order Items"
        indexes = [
            models.Index(fields=["seller", "created_at"], name="order_item_seller_created_idx"),
            models.Index(fields=["seller_order", "created_at"], name="order_item_suborder_idx"),
        ]

    def __str__(self):
        var_suffix = f" ({self.variant_label})" if self.variant_label else ""
        return f"{self.product_name}{var_suffix} x{self.quantity}"

    @property
    def has_complete_dimensions(self) -> bool:
        """True if all four physical snapshot measurements are populated and positive."""
        return all(
            getattr(self, f) is not None and getattr(self, f) > Decimal("0.00")
            for f in ["weight_grams", "length_cm", "breadth_cm", "height_cm"]
        )

    @property
    def total_weight_grams(self) -> Decimal | None:
        """Total weight for this line item in grams (weight_grams * quantity)."""
        if self.weight_grams is not None:
            return (self.weight_grams * Decimal(str(self.quantity))).quantize(Decimal("0.01"))
        return None

    @property
    def returned_quantity(self) -> int:
        """
        Sum of units in active or completed return/exchange requests.
        Excludes rejected and cancelled return requests.
        """
        from django.db.models import Sum
        active_statuses = [
            "requested", "approved", "pickup_pending",
            "item_received", "inspection", "processing", "completed",
        ]
        res = self.return_request_items.filter(
            return_request__status__in=active_statuses
        ).aggregate(total=Sum("quantity"))["total"]
        return res or 0

    @property
    def remaining_returnable_quantity(self) -> int:
        """Units that can still be requested for return/exchange."""
        return max(0, self.quantity - self.returned_quantity)

    @property
    def return_window_days(self) -> int:
        """Returns product-specific return window or falls back to global setting."""
        if self.product and getattr(self.product, "return_window_days", None) is not None:
            return self.product.return_window_days
        return getattr(settings, "FITMEEZO_RETURN_WINDOW_DAYS", 7)

    @property
    def is_return_eligible(self) -> bool:
        """
        Checks if item is part of a delivered package, product is returnable under policy,
        within product-specific return window, and has remaining returnable units.
        """
        if self.remaining_returnable_quantity <= 0:
            return False
        if self.product and not getattr(self.product, "is_returnable", True):
            return False
        if not self.seller_order or self.seller_order.status != SellerOrder.STATUS_DELIVERED:
            return False
        delivered_date = self.seller_order.delivered_at or self.seller_order.updated_at
        if not delivered_date:
            return False
        from datetime import timedelta
        return timezone.now() <= (delivered_date + timedelta(days=self.return_window_days))

    @property
    def active_return_item(self):
        """Returns the most recent active ReturnRequestItem for this order item if any."""
        active_statuses = [
            "requested", "approved", "pickup_pending",
            "item_received", "inspection", "processing", "completed",
        ]
        return self.return_request_items.filter(
            return_request__status__in=active_statuses
        ).select_related("return_request").order_by("-return_request__created_at", "-id").first()

    def clean(self):
        super().clean()
        if self.quantity < 1:
            raise ValidationError({"quantity": "Quantity must be at least 1."})
        if self.unit_price is not None and self.unit_price < Decimal("0.00"):
            raise ValidationError({"unit_price": "Unit price cannot be negative."})
        if self.commission_rate is not None and (self.commission_rate < Decimal("0.00") or self.commission_rate > Decimal("100.00")):
            raise ValidationError({"commission_rate": "Commission rate must be between 0.00% and 100.00%."})
        if self.commission_amount is not None and self.commission_amount < Decimal("0.00"):
            raise ValidationError({"commission_amount": "Commission amount cannot be negative."})
        for dim_field in ["weight_grams", "length_cm", "breadth_cm", "height_cm"]:
            val = getattr(self, dim_field)
            if val is not None and val <= Decimal("0.00"):
                label = dim_field.replace("_", " ").capitalize()
                raise ValidationError({dim_field: f"{label} must be greater than zero."})




class ShipmentBooking(models.Model):
    """
    Durable record of a courier booking for a SellerOrder package.
    Ensures strict 1-to-1 package idempotency, auditability, and clear separation
    between simulated (mock) bookings and live carrier-issued records.
    """
    package = models.OneToOneField(
        SellerOrder,
        on_delete=models.CASCADE,
        related_name="shipment_booking",
        help_text="Seller sub-order package associated with this booking.",
    )
    idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Cryptographic/unique request token preventing duplicate booking executions.",
    )
    is_mock = models.BooleanField(
        default=True,
        db_index=True,
        help_text="True if this is a simulated/mock booking (no live carrier contacted).",
    )
    status = models.CharField(
        max_length=30,
        default="booked",
        help_text="Simulated or provider booking status.",
    )
    shipment_id = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Simulated or provider shipment identifier (e.g. MOCK-SHP-...).",
    )
    awb_code = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Simulated or provider AWB tracking code (e.g. MOCK-AWB-...).",
    )
    courier_name = models.CharField(
        max_length=100,
        help_text="Courier partner name assigned to this shipment.",
    )
    courier_company_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="Provider courier company identifier.",
    )
    chargeable_weight_kg = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=Decimal("0.000"),
        help_text="Chargeable weight in kg used for this booking.",
    )
    estimated_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Estimated shipping rate in INR at booking time.",
    )
    pickup_nickname = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Origin warehouse / pickup facility nickname.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when booking was created.",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Sanitized response payload or simulation details.",
    )

    class Meta:
        verbose_name = "Shipment Booking"
        verbose_name_plural = "Shipment Bookings"
        ordering = ["-created_at"]


# ==============================================================================
# PHASE 6: RETURNS, EXCHANGES & REFUND MANAGEMENT MODELS
# ==============================================================================

class ReturnRequest(models.Model):
    """
    Auditable return or exchange request initiated by a customer
    for items within a specific delivered SellerOrder package.
    """
    TYPE_RETURN = "return"
    TYPE_EXCHANGE = "exchange"
    TYPE_CHOICES = [
        (TYPE_RETURN, "Return & Refund"),
        (TYPE_EXCHANGE, "Exchange / Replacement"),
    ]

    STATUS_REQUESTED = "requested"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_PICKUP_PENDING = "pickup_pending"
    STATUS_ITEM_RECEIVED = "item_received"
    STATUS_INSPECTION = "inspection"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Pending Review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_PICKUP_PENDING, "Pickup Scheduled"),
        (STATUS_ITEM_RECEIVED, "Item Received"),
        (STATUS_INSPECTION, "Under Inspection"),
        (STATUS_PROCESSING, "Refund Processing"),
        (STATUS_COMPLETED, "Refunded / Completed"),
        (STATUS_CANCELLED, "Closed / Cancelled"),
    ]

    REASON_DEFECTIVE = "defective"
    REASON_WRONG_ITEM = "wrong_item"
    REASON_SIZE_FIT = "size_fit"
    REASON_NOT_AS_DESCRIBED = "not_as_described"
    REASON_QUALITY_ISSUE = "quality_issue"
    REASON_OTHER = "other"

    REASON_CHOICES = [
        (REASON_DEFECTIVE, "Defective or Damaged Product"),
        (REASON_WRONG_ITEM, "Wrong Item or Size Delivered"),
        (REASON_SIZE_FIT, "Size or Fit Issue"),
        (REASON_NOT_AS_DESCRIBED, "Item Does Not Match Description"),
        (REASON_QUALITY_ISSUE, "Quality / Material Disappointment"),
        (REASON_OTHER, "Other Reason"),
    ]

    request_number = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Unique return request reference code (e.g. RET-20261011-XXXXXX).",
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="return_requests",
        help_text="Customer who requested the return.",
    )
    order = models.ForeignKey(
        "Order",
        on_delete=models.CASCADE,
        related_name="return_requests",
        help_text="Parent order containing returned merchandise.",
    )
    seller_order = models.ForeignKey(
        "SellerOrder",
        on_delete=models.CASCADE,
        related_name="return_requests",
        help_text="Seller sub-order package being returned.",
    )
    seller = models.ForeignKey(
        "Seller",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="return_requests",
        help_text="Merchant fulfilling the sub-order. Null for platform items.",
    )
    is_platform_fulfilled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if returning platform-fulfilled merchandise.",
    )
    request_type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default=TYPE_RETURN,
        db_index=True,
        help_text="Return for refund or exchange for replacement variant.",
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_REQUESTED,
        db_index=True,
        help_text="Lifecycle state of the return request.",
    )
    reason = models.CharField(
        max_length=40,
        choices=REASON_CHOICES,
        default=REASON_OTHER,
        help_text="Customer stated reason category for return.",
    )
    customer_notes = models.TextField(
        blank=True,
        default="",
        help_text="Customer's explanation or additional details.",
    )
    photo_1 = models.ImageField(
        upload_to="returns/%Y/%m/",
        null=True,
        blank=True,
        help_text="Optional customer condition photo 1.",
    )
    photo_2 = models.ImageField(
        upload_to="returns/%Y/%m/",
        null=True,
        blank=True,
        help_text="Optional customer condition photo 2.",
    )
    photo_3 = models.ImageField(
        upload_to="returns/%Y/%m/",
        null=True,
        blank=True,
        help_text="Optional customer condition photo 3.",
    )
    seller_notes = models.TextField(
        blank=True,
        default="",
        help_text="Internal notes added by seller or platform staff.",
    )
    rejection_reason = models.TextField(
        blank=True,
        default="",
        help_text="Reason for rejection provided to customer if rejected.",
    )
    inventory_restored = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Tracks whether restockable returned units were restored to catalog inventory.",
    )
    is_mock = models.BooleanField(
        default=True,
        help_text="Indicates return/refund logistics were handled via internal simulation.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when return was requested.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when return request was last modified.",
    )
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when return was completed, rejected, or cancelled.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Return Request"
        verbose_name_plural = "Return Requests"
        indexes = [
            models.Index(fields=["customer", "-created_at"], name="ret_cust_created_idx"),
            models.Index(fields=["seller_order", "status"], name="ret_pkg_status_idx"),
            models.Index(fields=["seller", "status"], name="ret_seller_status_idx"),
            models.Index(fields=["is_platform_fulfilled", "status"], name="ret_plt_status_idx"),
        ]

    def __str__(self):
        return f"{self.request_number} ({self.get_request_type_display()} - {self.get_status_display()})"

    @property
    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items.all())

    @property
    def total_refund_amount(self) -> Decimal:
        return sum((item.refund_eligible_amount for item in self.items.all()), Decimal("0.00"))

    @property
    def is_active(self) -> bool:
        """True if not rejected, cancelled, or already completed."""
        return self.status not in [self.STATUS_REJECTED, self.STATUS_CANCELLED, self.STATUS_COMPLETED]

    @property
    def can_customer_cancel(self) -> bool:
        """Customer can cancel before pickup is scheduled."""
        return self.status in [self.STATUS_REQUESTED, self.STATUS_APPROVED]


class ReturnRequestItem(models.Model):
    """
    Individual item line in a ReturnRequest.
    Tracks item quantity, optional exchange replacement variant,
    inspection decision, and refund amount eligibility.
    """
    INSPECTION_PENDING = "pending"
    INSPECTION_PASSED = "passed"
    INSPECTION_FAILED = "failed"
    INSPECTION_CHOICES = [
        (INSPECTION_PENDING, "Pending Inspection"),
        (INSPECTION_PASSED, "Passed (Good / Restockable)"),
        (INSPECTION_FAILED, "Failed (Damaged / Unrestockable)"),
    ]

    return_request = models.ForeignKey(
        ReturnRequest,
        on_delete=models.CASCADE,
        related_name="items",
        help_text="Parent return request.",
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.CASCADE,
        related_name="return_request_items",
        help_text="The purchased order item being returned.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Number of units requested for return/exchange.",
    )
    exchange_variant = models.ForeignKey(
        ProductVariant,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="exchange_request_items",
        help_text="Target replacement variant requested for an exchange.",
    )
    item_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Snapshot of unit purchase price from OrderItem.",
    )
    refund_eligible_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Merchandise amount eligible for refund (item_unit_price * quantity).",
    )
    restock_inventory = models.BooleanField(
        default=False,
        help_text="True if inspected goods are deemed restockable into active catalogue inventory.",
    )
    inspection_status = models.CharField(
        max_length=20,
        choices=INSPECTION_CHOICES,
        default=INSPECTION_PENDING,
        help_text="Quality inspection result.",
    )
    inspection_notes = models.TextField(
        blank=True,
        default="",
        help_text="Staff or seller remarks from merchandise physical inspection.",
    )

    class Meta:
        verbose_name = "Return Request Item"
        verbose_name_plural = "Return Request Items"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1),
                name="return_item_quantity_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(refund_eligible_amount__gte=Decimal("0.00")),
                name="return_item_refund_amount_gte_0",
            ),
        ]

    def __str__(self):
        return f"{self.return_request.request_number} - {self.order_item.product_name} (x{self.quantity})"


class RefundRecord(models.Model):
    """
    Durable, auditable ledger entry for refunds.
    Never alters historical Order or OrderItem totals.
    Supports safe mock/simulated workflow with zero live external calls.
    """
    STATUS_PENDING = "pending"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Approval / Processing"),
        (STATUS_PROCESSED, "Processed (Settled)"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    refund_reference = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Internal unique refund voucher reference (e.g. REF-20261011-XXXXXX).",
    )
    external_reference = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Simulated or provider refund transaction reference (e.g. MOCK-REF-...).",
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="refund_records",
        help_text="Parent order associated with this refund.",
    )
    return_request = models.ForeignKey(
        ReturnRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="refund_records",
        help_text="Return request triggering this refund, if applicable.",
    )
    seller_order = models.ForeignKey(
        SellerOrder,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="refund_records",
        help_text="Specific seller package associated with refunded items.",
    )
    requested_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Amount requested for refund in INR.",
    )
    approved_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Amount approved and settled for refund in INR.",
    )
    commission_reversal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Marketplace commission reversed as an auditable adjustment.",
    )
    seller_payable_deduction = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Net seller payable deduction for this refund.",
    )
    currency = models.CharField(
        max_length=10,
        default="INR",
        help_text="Three-letter ISO currency code.",
    )
    payment_method = models.CharField(
        max_length=50,
        help_text="Original payment channel or refund destination (e.g. razorpay, cod, store_credit, mock).",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
        help_text="Settlement status of this refund voucher.",
    )
    is_mock = models.BooleanField(
        default=True,
        db_index=True,
        help_text="True if this refund was settled via internal simulation (no real banking payout).",
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="processed_refunds",
        help_text="User/staff member who authorized or processed the refund.",
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when refund was processed/settled.",
    )
    reason = models.TextField(
        blank=True,
        default="",
        help_text="Official refund justification.",
    )
    audit_note = models.TextField(
        blank=True,
        default="",
        help_text="Auditable metadata, payment snapshot, or system notes.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when refund record was generated.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when refund record was last updated.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Refund Record"
        verbose_name_plural = "Refund Records"
        indexes = [
            models.Index(fields=["order", "status"], name="ref_order_status_idx"),
            models.Index(fields=["external_reference"], name="ref_ext_ref_idx"),
        ]

    def __str__(self):
        mock_tag = "[MOCK] " if self.is_mock else ""
        return f"{mock_tag}{self.refund_reference} - ₹{self.approved_amount} ({self.get_status_display()})"


class ReturnAuditLog(models.Model):
    """
    Append-only timeline record tracking state transitions, inspection outcomes,
    and administrative decisions for a ReturnRequest.
    """
    return_request = models.ForeignKey(
        ReturnRequest,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        help_text="Associated return request.",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="return_audit_logs",
        help_text="User who initiated this action, or null if system-driven.",
    )
    actor_role = models.CharField(
        max_length=30,
        default="customer",
        help_text="Entity role at action time: 'customer', 'seller', 'platform_admin', or 'system'.",
    )
    from_status = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="Previous return status.",
    )
    to_status = models.CharField(
        max_length=30,
        help_text="New return status.",
    )
    action = models.CharField(
        max_length=60,
        help_text="Event classification (e.g. status_transition, inspection_logged, refund_issued).",
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text="Descriptive notes or audit reasoning.",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured contextual metadata.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp of this audit log entry.",
    )

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Return Audit Log"
        verbose_name_plural = "Return Audit Logs"

    def __str__(self):
        return f"[{self.created_at.strftime('%Y-%m-%d %H:%M')}] {self.return_request.request_number}: {self.from_status} -> {self.to_status} by {self.actor_role}"


class PayoutBatch(models.Model):
    """
    Simulated internal seller payout batch record (Phase 7).
    Aggregates eligible posted ledger entries for a merchant disbursement run.
    Zero external banking or live payment gateway API calls.
    """

    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_PAID = "completed"  # Alias for completed payouts
    STATUS_FAILED = "failed"
    STATUS_REVERSED = "reversed"
    STATUS_ON_HOLD = "on_hold"

    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft / Prepared"),
        (STATUS_PENDING, "Pending Review"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed / Paid"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REVERSED, "Reversed"),
        (STATUS_ON_HOLD, "On Hold"),
    )

    batch_reference = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Unique internal payout identifier (e.g. PAY-20261011-A8F2B1).",
    )
    seller = models.ForeignKey(
        "Seller",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payout_batches",
        help_text="Recipient seller. Null for platform-fulfilled store account.",
    )
    is_platform_fulfilled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if representing platform central fulfillment internal disbursements.",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Total calculated net payable amount for this payout batch.",
    )
    currency = models.CharField(
        max_length=10,
        default="INR",
        help_text="ISO currency code.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
        help_text="Current state of payout disbursement.",
    )
    payout_method = models.CharField(
        max_length=40,
        default="mock_bank_transfer",
        help_text="Disbursement rail (e.g. mock_bank_transfer, mock_upi).",
    )
    external_reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Simulated external transaction reference (e.g. MOCK-PAY-XXXX).",
    )
    bank_account_masked = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="Snapshot of masked seller bank account at time of batch creation.",
    )
    bank_ifsc_code = models.CharField(
        max_length=11,
        blank=True,
        default="",
        help_text="Snapshot of seller bank IFSC code at time of batch creation.",
    )
    bank_beneficiary_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Snapshot of beneficiary name at time of batch creation.",
    )
    eligible_entries_count = models.PositiveIntegerField(
        default=0,
        help_text="Count of ledger entries bundled into this batch.",
    )
    period_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Earliest ledger entry timestamp covered.",
    )
    period_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Latest ledger entry timestamp covered.",
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when mock payout batch reached completed/failed terminal state.",
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_payouts",
        help_text="Platform staff user who approved or executed the batch.",
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text="Operational or audit notes.",
    )
    failure_reason = models.TextField(
        blank=True,
        default="",
        help_text="Detailed explanation in the event of disbursement failure or reversal.",
    )
    payout_cycle = models.CharField(
        max_length=20,
        blank=True,
        default="",
        db_index=True,
        help_text="Monthly payout cycle in YYYY-MM format (e.g. '2026-10').",
    )
    hold_reason = models.TextField(
        blank=True,
        default="",
        help_text="Reason why this batch was placed on hold.",
    )
    held_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when batch was placed on hold.",
    )
    held_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="held_payout_batches",
        help_text="Staff user who placed this batch on hold.",
    )
    released_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when hold was released.",
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="released_payout_batches",
        help_text="Staff user who released the hold.",
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when batch was approved for payout.",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_payout_batches",
        help_text="Staff user who approved the batch.",
    )
    rejection_reason = models.TextField(
        blank=True,
        default="",
        help_text="Reason why payout was rejected.",
    )
    rejected_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when payout was rejected.",
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_payout_batches",
        help_text="Staff user who rejected the payout.",
    )
    provider_status = models.CharField(
        max_length=50,
        blank=True,
        default="not_configured",
        help_text="Disbursement status returned by payout provider rail.",
    )
    retry_count = models.PositiveIntegerField(
        default=0,
        help_text="Count of retry attempts for this batch.",
    )
    is_mock = models.BooleanField(
        default=True,
        help_text="Always True. Simulated internal payout record without live bank or Razorpay Route API transfers.",
    )
    audit_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Financial breakdown snapshots, locking metadata, and verification stamps.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Payout Batch"
        verbose_name_plural = "Payout Batches"
        constraints = [
            models.UniqueConstraint(
                fields=["seller", "payout_cycle"],
                condition=~models.Q(payout_cycle="") & ~models.Q(status__in=["failed", "reversed"]),
                name="unique_active_seller_payout_cycle",
            ),
        ]

    def __str__(self):
        seller_label = self.seller.store_name if self.seller else "Platform Fulfillment"
        return f"[{self.batch_reference}] {seller_label} - ₹{self.amount} ({self.get_status_display()})"


class PayoutAuditLog(models.Model):
    """
    Immutable audit log for all administrative and automated state transitions
    on a PayoutBatch (creation, approval, hold, release, rejection, disbursement, failure, reversal).
    """

    ACTION_CREATED = "created"
    ACTION_APPROVED = "approved"
    ACTION_HELD = "held"
    ACTION_RELEASED = "released"
    ACTION_REJECTED = "rejected"
    ACTION_DISBURSED = "disbursed"
    ACTION_FAILED = "failed"
    ACTION_REVERSED = "reversed"
    ACTION_RETRY = "retry"

    ACTION_CHOICES = (
        (ACTION_CREATED, "Batch Created"),
        (ACTION_APPROVED, "Approved for Payout"),
        (ACTION_HELD, "Payout Held"),
        (ACTION_RELEASED, "Hold Released"),
        (ACTION_REJECTED, "Payout Rejected"),
        (ACTION_DISBURSED, "Disbursement Executed"),
        (ACTION_FAILED, "Disbursement Failed"),
        (ACTION_REVERSED, "Batch Reversed"),
        (ACTION_RETRY, "Retry Attempted"),
    )

    payout_batch = models.ForeignKey(
        PayoutBatch,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        help_text="Associated payout batch.",
    )
    action = models.CharField(
        max_length=30,
        choices=ACTION_CHOICES,
        db_index=True,
        help_text="Lifecycle or control action performed.",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payout_audit_logs",
        help_text="Staff administrator or system actor executing the action.",
    )
    from_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Previous batch status.",
    )
    to_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="New batch status.",
    )
    reason = models.TextField(
        blank=True,
        default="",
        help_text="Mandatory or descriptive explanation for hold, rejection, or reversal.",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Snapshot context such as eligible counts, amounts, provider response.",
    )
    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Timestamp when the action was recorded.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Payout Audit Log"
        verbose_name_plural = "Payout Audit Logs"

    def __str__(self):
        return f"[{self.created_at.strftime('%Y-%m-%d %H:%M')}] {self.payout_batch.batch_reference}: {self.get_action_display()} by {self.actor}"


class SellerLedgerEntry(models.Model):
    """
    Immutable double-entry financial ledger record for multi-seller marketplace accounting (Phase 7).
    Tracks gross merchandise value, snapshotted commissions, shipping allocations,
    returns/refunds, cancellations, payouts, and approved manual adjustments.
    """

    TYPE_CREDIT = "credit"  # Increases amount payable to seller
    TYPE_DEBIT = "debit"    # Decreases amount payable to seller (deduction)

    ENTRY_TYPE_CHOICES = (
        (TYPE_CREDIT, "Credit (Payable to Seller)"),
        (TYPE_DEBIT, "Debit (Deduction from Seller)"),
    )

    CATEGORY_ORDER_SALE = "order_sale"
    CATEGORY_COMMISSION = "commission"
    CATEGORY_SHIPPING_CREDIT = "shipping_credit"
    CATEGORY_SHIPPING_DEBIT = "shipping_debit"
    CATEGORY_RETURN_REFUND = "return_refund"
    CATEGORY_COMMISSION_REVERSAL = "commission_reversal"
    CATEGORY_ORDER_CANCELLATION = "order_cancellation"
    CATEGORY_PAYOUT = "payout"
    CATEGORY_PAYOUT_REVERSAL = "payout_reversal"
    CATEGORY_MANUAL_ADJUSTMENT = "manual_adjustment"

    CATEGORY_CHOICES = (
        (CATEGORY_ORDER_SALE, "Order Sale (GMV)"),
        (CATEGORY_COMMISSION, "Platform Commission"),
        (CATEGORY_SHIPPING_CREDIT, "Customer Shipping Collected"),
        (CATEGORY_SHIPPING_DEBIT, "Actual Shipping Cost Allocation"),
        (CATEGORY_RETURN_REFUND, "Customer Return Refund Deduction"),
        (CATEGORY_COMMISSION_REVERSAL, "Commission Reversal on Return"),
        (CATEGORY_ORDER_CANCELLATION, "Order Cancellation Reversal"),
        (CATEGORY_PAYOUT, "Payout Batch Disbursement"),
        (CATEGORY_PAYOUT_REVERSAL, "Payout Batch Reversal"),
        (CATEGORY_MANUAL_ADJUSTMENT, "Approved Manual Adjustment"),
    )

    STATUS_POSTED = "posted"
    STATUS_PENDING_COD = "pending_cod"
    STATUS_REVERSED = "reversed"

    STATUS_CHOICES = (
        (STATUS_POSTED, "Posted / Immutable"),
        (STATUS_PENDING_COD, "Pending COD Reconciliation"),
        (STATUS_REVERSED, "Reversed by Offset Entry"),
    )

    seller = models.ForeignKey(
        "Seller",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text="Beneficiary seller. Null for platform-fulfilled store account.",
    )
    is_platform_fulfilled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if representing platform central fulfillment internal transactions.",
    )
    entry_type = models.CharField(
        max_length=10,
        choices=ENTRY_TYPE_CHOICES,
        db_index=True,
        help_text="Credit increases payable to seller; Debit reduces payable.",
    )
    category = models.CharField(
        max_length=30,
        choices=CATEGORY_CHOICES,
        db_index=True,
        help_text="Accounting categorization of this entry.",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Positive decimal transaction magnitude.",
    )
    currency = models.CharField(
        max_length=10,
        default="INR",
        help_text="ISO currency code.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_POSTED,
        db_index=True,
        help_text="Current posting status. Pending COD entries are strictly excluded from payouts.",
    )
    source_type = models.CharField(
        max_length=40,
        default="",
        blank=True,
        help_text="Originating source entity type (e.g. seller_order, refund_record, payout_batch, manual).",
    )
    source_reference = models.CharField(
        max_length=100,
        db_index=True,
        blank=True,
        default="",
        help_text="Human-readable business identifier of the source document.",
    )
    order = models.ForeignKey(
        "Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text="Associated parent order.",
    )
    seller_order = models.ForeignKey(
        "SellerOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text="Associated seller sub-order package.",
    )
    refund_record = models.ForeignKey(
        "RefundRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text="Associated refund record.",
    )
    payout_batch = models.ForeignKey(
        PayoutBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text="Payout batch that bundled and disbursed this entry. Null if unbatched / unsettled.",
    )
    idempotency_key = models.CharField(
        max_length=140,
        unique=True,
        db_index=True,
        help_text="Unique deterministic key preventing duplicate postings at the database level.",
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Clear human-readable summary of this ledger movement.",
    )
    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Creation timestamp of the entry.",
    )
    posted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when entry officially finalized/posted (e.g. when COD was reconciled).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_ledger_entries",
        help_text="System or staff user who authorized/posted the transaction.",
    )
    audit_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Detailed rate snapshots, commission calculations, and operational context.",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Seller Ledger Entry"
        verbose_name_plural = "Seller Ledger Entries"

    def __str__(self):
        seller_label = self.seller.store_name if self.seller else "Platform Fulfillment"
        sign = "+" if self.entry_type == self.TYPE_CREDIT else "-"
        return f"[{self.idempotency_key}] {seller_label} {sign}₹{self.amount} ({self.get_category_display()})"

    @property
    def signed_amount(self) -> Decimal:
        """Returns signed decimal amount: positive for credit, negative for debit."""
        if self.entry_type == self.TYPE_CREDIT:
            return self.amount
        return -self.amount

    @property
    def is_payout_eligible(self) -> bool:
        """Quick check if entry is posted and not yet covered by a payout batch."""
        return self.status == self.STATUS_POSTED and self.payout_batch_id is None


class CODReceiptRecord(models.Model):
    """
    Courier Cash-on-Delivery (COD) cash receipt and remittance reconciliation record (Phase 7).
    Explicitly tracks when courier remittance was confirmed, unlocking pending COD ledger entries.
    """

    remittance_reference = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Courier COD remittance reference number / bank UTR / consignment batch ID.",
    )
    seller_order = models.OneToOneField(
        "SellerOrder",
        on_delete=models.PROTECT,
        related_name="cod_reconciliation",
        help_text="Target sub-order package whose COD remittance is being confirmed.",
    )
    order = models.ForeignKey(
        "Order",
        on_delete=models.PROTECT,
        related_name="cod_reconciliations",
        help_text="Parent order.",
    )
    collected_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Actual cash amount collected by courier and remitted to platform.",
    )
    courier_name = models.CharField(
        max_length=80,
        blank=True,
        default="",
        help_text="Delivering courier agency (e.g. Blue Dart, Delhivery, Shadowfax via Shiprocket).",
    )
    remitted_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date on which funds were remitted by the logistics partner.",
    )
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cod_reconciliations",
        help_text="Platform administrator who verified and logged remittance.",
    )
    reconciled_at = models.DateTimeField(
        default=timezone.now,
        help_text="System timestamp when reconciliation took effect.",
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text="Discrepancy notes, courier invoice reference, or reconciliation details.",
    )
    audit_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Contextual metadata snapshot at time of reconciliation.",
    )

    class Meta:
        ordering = ["-reconciled_at"]
        verbose_name = "COD Receipt Reconciliation"
        verbose_name_plural = "COD Receipt Reconciliations"

    def __str__(self):
        return f"[COD-REC] #{self.seller_order.sub_order_number} - ₹{self.collected_amount} (Ref: {self.remittance_reference})"


# ==============================================================================
# INVOICE NUMBERING & IMMUTABLE INVOICE RECORDS (PHASE 8)
# ==============================================================================

class InvoiceSequence(models.Model):
    """
    Transaction-safe sequential counter for generating unique, monotonically
    increasing invoice numbers without race conditions or count() + 1 vulnerabilities.

    Uses database row-level locking (select_for_update) inside an atomic transaction.
    """
    prefix = models.CharField(
        max_length=32,
        unique=True,
        db_index=True,
        help_text="Sequence prefix identifier (e.g. 'FM-INV-202610' or 'FM-SINV-202610').",
    )
    last_number = models.PositiveIntegerField(
        default=0,
        help_text="Last allocated integer sequence number for this prefix.",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp when sequence was last incremented.",
    )

    class Meta:
        verbose_name = "Invoice Sequence Counter"
        verbose_name_plural = "Invoice Sequence Counters"

    def __str__(self):
        return f"Seq[{self.prefix}] = {self.last_number}"


class Invoice(models.Model):
    """
    Fitmeezo Immutable Invoice Document Record (Phase 8).

    Serves as the legally auditable record for issued customer parent invoices
    and seller package commercial invoices.

    Key Integrity Guarantees:
    - Unique, database-constrained invoice_number allocated safely via InvoiceSequence.
    - Historical financial figures (subtotal, shipping, discount, total) snapshotted at issue time.
    - Issuer and recipient identity (names, addresses, tax IDs if verified) snapshotted immutably.
    - Zero live recalculation of historical invoices from altered catalog prices or commission rates.
    - Distinct invoice_type ('customer_parent' vs 'seller_package').
    - Multi-seller isolation: seller_package invoices are linked to specific SellerOrder and Seller.
    """

    TYPE_CUSTOMER_PARENT = "customer_parent"
    TYPE_SELLER_PACKAGE = "seller_package"

    TYPE_CHOICES = [
        (TYPE_CUSTOMER_PARENT, "Customer Parent Invoice"),
        (TYPE_SELLER_PACKAGE, "Seller Package Invoice"),
    ]

    STATUS_ISSUED = "issued"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_ISSUED, "Issued / Valid"),
        (STATUS_CANCELLED, "Cancelled / Void"),
    ]

    invoice_number = models.CharField(
        max_length=48,
        unique=True,
        db_index=True,
        help_text="Unique invoice identifier (e.g. FM-INV-20261011-00001).",
    )
    invoice_type = models.CharField(
        max_length=25,
        choices=TYPE_CHOICES,
        default=TYPE_CUSTOMER_PARENT,
        db_index=True,
        help_text="Classification: customer parent order invoice or seller package commercial invoice.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ISSUED,
        db_index=True,
        help_text="Legal validity state of this document.",
    )

    # Order Associations
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name="invoices",
        help_text="Associated parent customer order.",
    )
    seller_order = models.ForeignKey(
        SellerOrder,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices",
        help_text="Associated seller sub-order package (null for customer parent invoice).",
    )
    seller = models.ForeignKey(
        Seller,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices",
        help_text="Fulfilling merchant (null for platform-issued parent invoice or platform inventory).",
    )

    # Financial Snapshots (Immutable at issue time)
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Snapshot of merchandise subtotal in INR.",
    )
    shipping_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Snapshot of shipping charge in INR.",
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Snapshot of discounts applied in INR.",
    )
    tax_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Snapshot of verified stored tax amount in INR (0.00 if unconfigured / inclusive).",
    )
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Snapshot of final payable invoice total in INR.",
    )
    currency = models.CharField(
        max_length=10,
        default="INR",
        help_text="Transaction currency (e.g. INR).",
    )

    # Payment Snapshot
    payment_method = models.CharField(
        max_length=30,
        help_text="Snapshot of payment method at time of issuance (e.g. COD, Razorpay).",
    )
    payment_status = models.CharField(
        max_length=30,
        help_text="Snapshot of payment settlement status at time of issuance.",
    )

    # Issuer Snapshot (Platform or Seller)
    issuer_name = models.CharField(
        max_length=200,
        help_text="Legal or commercial name of the issuing entity.",
    )
    issuer_address = models.TextField(
        blank=True,
        default="",
        help_text="Registered office address of issuer.",
    )
    issuer_gstin = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Verified GSTIN of issuing entity (empty string if unconfigured; never fabricated).",
    )
    issuer_pan = models.CharField(
        max_length=15,
        blank=True,
        default="",
        help_text="Verified PAN of issuing entity (empty string if unconfigured).",
    )

    # Recipient Snapshot (Customer)
    recipient_name = models.CharField(
        max_length=150,
        help_text="Customer / recipient name snapshot.",
    )
    recipient_phone = models.CharField(
        max_length=25,
        blank=True,
        default="",
        help_text="Customer contact phone snapshot.",
    )
    recipient_shipping_address = models.TextField(
        help_text="Full delivery address snapshot at purchase time.",
    )
    recipient_billing_address = models.TextField(
        blank=True,
        default="",
        help_text="Billing address snapshot if distinct.",
    )

    # Audit & Timestamps
    issued_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Exact timestamp when this invoice was officially issued.",
    )
    audit_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Detailed audit trail including item snapshots, rule configurations, and generator versions.",
    )

    class Meta:
        ordering = ["-issued_at"]
        verbose_name = "Invoice Record"
        verbose_name_plural = "Invoice Records"
        indexes = [
            models.Index(fields=["order", "invoice_type"], name="inv_order_type_idx"),
            models.Index(fields=["seller_order", "invoice_type"], name="inv_sellerorder_type_idx"),
            models.Index(fields=["seller", "issued_at"], name="inv_seller_issued_idx"),
            models.Index(fields=["invoice_number"], name="inv_num_idx"),
        ]
        constraints = [
            # Ensure at most one customer parent invoice per order
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(invoice_type="customer_parent"),
                name="unique_customer_parent_inv_per_order",
            ),
            # Ensure at most one seller package invoice per seller_order
            models.UniqueConstraint(
                fields=["seller_order"],
                condition=models.Q(invoice_type="seller_package", seller_order__isnull=False),
                name="unique_seller_pkg_inv_per_suborder",
            ),
        ]

    def __str__(self):
        return f"[{self.get_invoice_type_display()}] #{self.invoice_number} (Order #{self.order.order_number})"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    """Ensures each User has an associated CustomerProfile instance."""
    if created:
        CustomerProfile.objects.get_or_create(user=instance)


# Backward-compatibility alias so user.customerprofile accesses user.profile safely
try:
    from django.contrib.auth import get_user_model
    _User = get_user_model()
    if not hasattr(_User, "customerprofile"):
        _User.customerprofile = property(lambda self: getattr(self, "profile", None))
    if not hasattr(_User, "customer_phone"):
        def _get_customer_phone(self):
            try:
                p = getattr(self, "profile", None)
                return p.phone_number if p else ""
            except Exception:
                return ""
        _User.customer_phone = property(_get_customer_phone)
except Exception:
    pass





