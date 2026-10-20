"""
Django settings for Fitmeezo project.

Fitmeezo - SHOP SMART • LIVE BETTER
Multi-category e-commerce brand platform for India (INR).
Phase 1: Project Foundation.
"""

import os
import urllib.parse
from pathlib import Path
from decimal import Decimal
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables: process env takes precedence, followed by root .env, then backend/.env
load_dotenv(BASE_DIR.parent / ".env", override=False)
load_dotenv(BASE_DIR / ".env", override=False)

# ==============================================================================
# CORE BRAND & LOCALIZATION METADATA
# ==============================================================================
BRAND_NAME = os.getenv("BRAND_NAME", "Fitmeezo")
BRAND_TAGLINE = os.getenv("BRAND_TAGLINE", "SHOP SMART • LIVE BETTER")
CURRENCY = os.getenv("CURRENCY", "INR")
CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "₹")

# ==============================================================================
# SECURITY SETTINGS
# ==============================================================================
DEBUG = os.getenv("DEBUG", "True").strip().lower() in ("true", "1", "yes")

DEV_SECRET_KEY = "django-insecure-fitmeezo-phase1-dev-key-change-in-production-739281!@#"
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = DEV_SECRET_KEY
    else:
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable is required when DEBUG=False in production. "
            "Please configure a cryptographically secure SECRET_KEY in your production environment."
        )
elif not DEBUG and SECRET_KEY == DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "Insecure development SECRET_KEY detected in production (DEBUG=False). "
        "Please generate a unique, cryptographically secure SECRET_KEY."
    )

_default_hosts = "127.0.0.1,localhost,testserver,fitmeezo.com,www.fitmeezo.com" if DEBUG else "fitmeezo.com,www.fitmeezo.com"
_allowed_hosts_raw = os.getenv("ALLOWED_HOSTS", _default_hosts)
ALLOWED_HOSTS = [host.strip() for host in _allowed_hosts_raw.split(",") if host.strip()]

# Explicitly ensure custom production domains are always permitted
for host in ("fitmeezo.com", "www.fitmeezo.com"):
    if host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(host)

# Automatically permit external hostname if set by platform
_render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if _render_host and _render_host not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_render_host)

if DEBUG and "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")

if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS environment variable must be set when DEBUG=False in production. "
        "Please specify your domain(s) in ALLOWED_HOSTS."
    )

_csrf_trusted_raw = os.getenv("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in _csrf_trusted_raw.split(",") if origin.strip()]
for origin in (
    "https://fitmeezo.com",
    "https://www.fitmeezo.com",
    "http://fitmeezo.com",
    "http://www.fitmeezo.com",
):
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)

if _render_host:
    _render_origin = f"https://{_render_host}"
    if _render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_render_origin)

# Trust forwarded host headers from reverse proxies (Render, Cloudflare, Nginx)
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# ==============================================================================
# HTTPS & PRODUCTION SECURITY HEADERS
# ==============================================================================
# In production (DEBUG=False), secure cookies and SSL redirection are enabled by default,
# but can be tuned or toggled via environment variables.
# In local development (DEBUG=True), all SSL redirects and secure cookies remain disabled.
SECURE_SSL_REDIRECT = os.getenv(
    "SECURE_SSL_REDIRECT",
    "True" if not DEBUG else "False"
).strip().lower() in ("true", "1", "yes")

SESSION_COOKIE_SECURE = os.getenv(
    "SESSION_COOKIE_SECURE",
    "True" if not DEBUG else "False"
).strip().lower() in ("true", "1", "yes")

CSRF_COOKIE_SECURE = os.getenv(
    "CSRF_COOKIE_SECURE",
    "True" if not DEBUG else "False"
).strip().lower() in ("true", "1", "yes")

# HTTP Strict Transport Security (HSTS)
# Recommended standard for production HTTPS: 31536000 seconds (1 year).
# Kept 0 during local development (DEBUG=True) to avoid forcing HTTPS on localhost.
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000" if not DEBUG else "0"))

# Explicitly kept False per instructions (do not enable preload or subdomains without explicit confirmation)
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# Reverse proxy SSL header support (when deployed behind Nginx, AWS ALB, Cloudflare, etc.)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ==============================================================================
# APPLICATION DEFINITION
# ==============================================================================
# ==============================================================================
# CLOUDINARY PERSISTENT MEDIA STORAGE CONFIGURATION
# ==============================================================================
# Provides durable, persistent media storage for product photographs, gallery images,
# category banners, and return condition photos when deployed to ephemeral hosting (Render).
# In local development without Cloudinary, zero-config FileSystemStorage is used automatically.
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()
CLOUDINARY_SECURE = os.getenv("CLOUDINARY_SECURE", "True").strip().lower() in ("true", "1", "yes")
CLOUDINARY_MEDIA_TAG = os.getenv("CLOUDINARY_MEDIA_TAG", "media").strip()

def _is_valid_cloudinary_credential(val: str) -> bool:
    cleaned = (val or "").strip().lower()
    return bool(cleaned and not cleaned.startswith(("replace_", "your_", "todo", "none")))

CLOUDINARY_ENABLED = bool(
    _is_valid_cloudinary_credential(CLOUDINARY_URL)
    or (
        _is_valid_cloudinary_credential(CLOUDINARY_CLOUD_NAME)
        and _is_valid_cloudinary_credential(CLOUDINARY_API_KEY)
        and _is_valid_cloudinary_credential(CLOUDINARY_API_SECRET)
    )
)

INSTALLED_APPS = [
    # Core Django apps
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",

    # Third-party apps
    "rest_framework",
    "rest_framework.authtoken",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",

    # Fitmeezo local apps
    "core.apps.CoreConfig",
    "management_console.apps.ManagementConsoleConfig",
]

if CLOUDINARY_ENABLED:
    # 'cloudinary_storage' must be placed before 'django.contrib.staticfiles' per docs
    _staticfiles_idx = (
        INSTALLED_APPS.index("django.contrib.staticfiles")
        if "django.contrib.staticfiles" in INSTALLED_APPS
        else 0
    )
    INSTALLED_APPS.insert(_staticfiles_idx, "cloudinary_storage")
    INSTALLED_APPS.append("cloudinary")

SITE_ID = 1

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise: serves static files efficiently in production (must be second, immediately after SecurityMiddleware)
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "fitmeezo.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.brand_context",
                "core.context_processors.cart_context",
                "core.context_processors.wishlist_context",
                "core.context_processors.oauth_context",
            ],
        },
    },
]

WSGI_APPLICATION = "fitmeezo.wsgi.application"
ASGI_APPLICATION = "fitmeezo.asgi.application"

# ==============================================================================
# DATABASE CONFIGURATION (PostgreSQL Production Ready with SQLite Fallback)
# ==============================================================================
# Supported configuration methods:
#   1. DATABASE_URL: e.g. postgresql://user:password@host:port/dbname?sslmode=require
#   2. Individual DB_* variables: DB_ENGINE, DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT
#   3. Zero-config fallback: local SQLite database (db.sqlite3)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").strip().lower()
DB_NAME = os.getenv("DB_NAME", "").strip()

if DATABASE_URL:
    parsed_db = urllib.parse.urlparse(DATABASE_URL)
    is_postgres = parsed_db.scheme in ("postgres", "postgresql", "pgsql")
    pg_options = {}
    query_params = urllib.parse.parse_qs(parsed_db.query)
    if "sslmode" in query_params:
        pg_options["sslmode"] = query_params["sslmode"][0]
    sslmode_env = os.getenv("DB_SSLMODE", "").strip()
    if sslmode_env:
        pg_options["sslmode"] = sslmode_env

    conn_max_age = int(os.getenv("DB_CONN_MAX_AGE", "60" if is_postgres else "0"))

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql" if is_postgres else "django.db.backends.sqlite3",
            "NAME": parsed_db.path.lstrip("/"),
            "USER": urllib.parse.unquote(parsed_db.username or ""),
            "PASSWORD": urllib.parse.unquote(parsed_db.password or ""),
            "HOST": parsed_db.hostname or "127.0.0.1",
            "PORT": str(parsed_db.port or "5432"),
            "CONN_MAX_AGE": conn_max_age,
            "CONN_HEALTH_CHECKS": is_postgres and conn_max_age > 0,
            "OPTIONS": pg_options,
        }
    }
elif DB_ENGINE in ("postgresql", "postgres", "django.db.backends.postgresql") and DB_NAME:
    pg_options = {}
    sslmode_env = os.getenv("DB_SSLMODE", "").strip()
    if sslmode_env:
        pg_options["sslmode"] = sslmode_env
    conn_max_age = int(os.getenv("DB_CONN_MAX_AGE", "60"))

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": DB_NAME,
            "USER": os.getenv("DB_USER", "postgres"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "CONN_MAX_AGE": conn_max_age,
            "CONN_HEALTH_CHECKS": conn_max_age > 0,
            "OPTIONS": pg_options,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "CONN_MAX_AGE": 0,
        }
    }

# ==============================================================================
# PASSWORD VALIDATION
# ==============================================================================
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# ==============================================================================
# INTERNATIONALIZATION & LOCALIZATION
# ==============================================================================
LANGUAGE_CODE = "en-in"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

# ==============================================================================
# STATIC & MEDIA FILES
# ==============================================================================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [
    BASE_DIR / "static",
]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ==============================================================================
# STORAGE ENGINES (CLOUDINARY PERSISTENT MEDIA & WHITENOISE COMPRESSED STATIC)
# ==============================================================================
CLOUDINARY_STORAGE = {
    "CLOUD_NAME": CLOUDINARY_CLOUD_NAME,
    "API_KEY": CLOUDINARY_API_KEY,
    "API_SECRET": CLOUDINARY_API_SECRET,
    "SECURE": CLOUDINARY_SECURE,
    "MEDIA_TAG": CLOUDINARY_MEDIA_TAG,
}
if CLOUDINARY_URL:
    CLOUDINARY_STORAGE["CLOUDINARY_URL"] = CLOUDINARY_URL

_staticfiles_backend = (
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
    if not DEBUG
    else "django.contrib.staticfiles.storage.StaticFilesStorage"
)

if CLOUDINARY_ENABLED:
    DEFAULT_FILE_STORAGE = "cloudinary_storage.storage.MediaCloudinaryStorage"
    STORAGES = {
        "default": {
            "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
        },
        "staticfiles": {
            "BACKEND": _staticfiles_backend,
        },
    }
else:
    DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": _staticfiles_backend,
        },
    }

# WhiteNoise serves compressed, cached static files from STATIC_ROOT in production.
STATICFILES_STORAGE = _staticfiles_backend

# WhiteNoise also serves local media directory at /media/ in local development / fallback
WHITENOISE_ROOT = BASE_DIR / "media"
WHITENOISE_AUTOREFRESH = DEBUG  # Autorefresh in dev, cached manifest in production

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ==============================================================================
# DJANGO REST FRAMEWORK CONFIGURATION
# ==============================================================================
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# ==============================================================================
# CUSTOMER AUTHENTICATION & LOGIN CONFIGURATION
# ==============================================================================
AUTHENTICATION_BACKENDS = [
    "core.backends.EmailOrUsernameModelBackend",
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LOGIN_URL = "core:account_login"
LOGIN_REDIRECT_URL = "core:account_dashboard"
LOGOUT_REDIRECT_URL = "core:home"

# ==============================================================================
# ALLAUTH & SOCIAL AUTHENTICATION CONFIGURATION (PROMPT 11)
# ==============================================================================
ACCOUNT_LOGIN_METHODS = {"email", "username"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "optional"
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https" if not DEBUG else "http"
ACCOUNT_PRESERVE_USERNAME_CASING = False

SOCIALACCOUNT_ADAPTER = "core.adapters.FitmeezoSocialAccountAdapter"
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_AUTO_SIGNUP = True

# Read Google OAuth client ID and secret strictly from environment variables (fallback/context)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

# Google OAuth Provider Settings
# Credentials are loaded exclusively from the database-backed SocialApp record
# to avoid duplicate app registrations and prevent django-allauth MultipleObjectsReturned errors.
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
        "VERIFIED_EMAIL": True,
    },
}

# ==============================================================================
# EMAIL CONFIGURATION (PASSWORD RESET, ORDER CONFIRMATIONS & NOTIFICATIONS)
# ==============================================================================
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").strip()
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() in ("true", "1", "t", "yes")
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() in ("true", "1", "t", "yes")

# Auto-switch to smtp backend whenever host user & password are provided (both locally and production)
# Otherwise fall back to console backend when DEBUG=True
_has_smtp_credentials = bool(EMAIL_HOST_USER and EMAIL_HOST_PASSWORD)
_default_email_backend = (
    "django.core.mail.backends.smtp.EmailBackend"
    if (_has_smtp_credentials or not DEBUG)
    else "django.core.mail.backends.console.EmailBackend"
)
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", _default_email_backend)

_default_from = f"Fitmeezo <{EMAIL_HOST_USER}>" if EMAIL_HOST_USER else "Fitmeezo <support@fitmeezo.com>"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", _default_from)
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@fitmeezo.com")
SUPPORT_PHONE = os.getenv("SUPPORT_PHONE", "+91 940885323")

# ==============================================================================
# RAZORPAY PAYMENT GATEWAY CONFIGURATION
# ==============================================================================
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
RAZORPAY_CURRENCY = os.getenv("RAZORPAY_CURRENCY", "INR").strip()

# Razorpay is enabled strictly when valid, non-placeholder credentials are provided.
# If credentials are missing or placeholders, payment safely falls back to COD.
RAZORPAY_ENABLED = bool(
    RAZORPAY_KEY_ID
    and RAZORPAY_KEY_SECRET
    and not RAZORPAY_KEY_ID.lower().startswith(("replace_", "your_", "todo"))
    and not RAZORPAY_KEY_SECRET.lower().startswith(("replace_", "your_", "todo"))
)

# Cache Configuration (Used for Shiprocket token caching and rate-estimate caching)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fitmeezo-default-cache",
    }
}

# ==============================================================================
# SHIPPING LOGISTICS & SHIPROCKET CONFIGURATION (PHASE 5)
# ==============================================================================
# Shiprocket API Base URL
SHIPROCKET_API_BASE_URL = os.getenv("SHIPROCKET_API_BASE_URL", "https://apiv2.shiprocket.in/v2/console").rstrip("/")

# Provider credentials (empty by default; loaded strictly from environment)
SHIPROCKET_API_EMAIL = os.getenv("SHIPROCKET_API_EMAIL", "").strip()
SHIPROCKET_API_PASSWORD = os.getenv("SHIPROCKET_API_PASSWORD", "").strip()

# HTTP Client Timeouts & Transient Retries
SHIPROCKET_CONNECT_TIMEOUT = float(os.getenv("SHIPROCKET_CONNECT_TIMEOUT", "5.0"))
SHIPROCKET_READ_TIMEOUT = float(os.getenv("SHIPROCKET_READ_TIMEOUT", "15.0"))
SHIPROCKET_MAX_RETRIES = int(os.getenv("SHIPROCKET_MAX_RETRIES", "3"))
SHIPROCKET_BACKOFF_FACTOR = float(os.getenv("SHIPROCKET_BACKOFF_FACTOR", "0.5"))
SHIPROCKET_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

# Mock Mode (allows offline / simulated testing without live network calls)
SHIPROCKET_MOCK_MODE = os.getenv("SHIPROCKET_MOCK_MODE", "False").lower() in ("true", "1", "t", "yes")

# Token Cache Timeout (Shiprocket tokens expire after 10 days = 864,000s; cache for 7 days = 604,800s)
SHIPROCKET_TOKEN_CACHE_TIMEOUT = int(os.getenv("SHIPROCKET_TOKEN_CACHE_TIMEOUT", str(86400 * 7)))

# Shiprocket is enabled only when non-placeholder credentials are provided
SHIPROCKET_ENABLED = bool(
    SHIPROCKET_API_EMAIL
    and SHIPROCKET_API_PASSWORD
    and not SHIPROCKET_API_EMAIL.lower().startswith(("replace_", "your_", "todo"))
    and not SHIPROCKET_API_PASSWORD.lower().startswith(("replace_", "your_", "todo"))
)

# Volumetric divisor assumption (default: 5000 for standard Indian surface logistics)
SHIPROCKET_VOLUMETRIC_DIVISOR = Decimal(os.getenv("SHIPROCKET_VOLUMETRIC_DIVISOR", "5000.00"))

# Safe platform central warehouse configuration (for platform-owned inventory fulfillment)
# Values are loaded safely from environment variables with non-secret defaults.
FITMEEZO_PLATFORM_WAREHOUSE = {
    "facility_name": os.getenv("PLATFORM_WAREHOUSE_NAME", "Fitmeezo Central Fulfillment Center"),
    "contact_name": os.getenv("PLATFORM_WAREHOUSE_CONTACT", "Fitmeezo Operations"),
    "contact_phone": os.getenv("PLATFORM_WAREHOUSE_PHONE", "940885323"),
    "contact_email": os.getenv("PLATFORM_WAREHOUSE_EMAIL", "support@fitmeezo.com"),
    "address_line1": os.getenv("PLATFORM_WAREHOUSE_ADDRESS1", "Plot 42, Industrial Area Phase II"),
    "address_line2": os.getenv("PLATFORM_WAREHOUSE_ADDRESS2", ""),
    "city": os.getenv("PLATFORM_WAREHOUSE_CITY", "Bengaluru"),
    "state": os.getenv("PLATFORM_WAREHOUSE_STATE", "Karnataka"),
    "postal_code": os.getenv("PLATFORM_WAREHOUSE_PIN", "560100"),
    "country": "India",
    "is_verified": os.getenv("PLATFORM_WAREHOUSE_VERIFIED", "False").lower() in ("true", "1", "t", "yes"),
}

# ==============================================================================
# RETURNS, EXCHANGES & REFUND MANAGEMENT CONFIGURATION (PHASE 6)
# ==============================================================================
# Standard return window in days after delivery (configurable default: 7 days)
FITMEEZO_RETURN_WINDOW_DAYS = int(os.getenv("FITMEEZO_RETURN_WINDOW_DAYS", "7"))
# Enables exchange / replacement variant selection
FITMEEZO_EXCHANGE_ENABLED = os.getenv("FITMEEZO_EXCHANGE_ENABLED", "True").lower() in ("true", "1", "t", "yes")
# Safe mock refund mode: strictly prevents contacting Razorpay/banking APIs
FITMEEZO_MOCK_REFUNDS = os.getenv("FITMEEZO_MOCK_REFUNDS", "True").lower() in ("true", "1", "t", "yes")

# ==============================================================================
# SELLER LEDGER, COMMISSION & PAYOUT CONFIGURATION (PHASE 7)
# ==============================================================================
# Safe mock payout mode: strictly prevents real bank transfers or Razorpay Route calls
FITMEEZO_MOCK_PAYOUTS = os.getenv("FITMEEZO_MOCK_PAYOUTS", "True").lower() in ("true", "1", "t", "yes")
# Cooling/holding window in days post-delivery before order funds qualify for payout eligibility
FITMEEZO_PAYOUT_HOLD_DAYS_AFTER_DELIVERY = int(os.getenv("FITMEEZO_PAYOUT_HOLD_DAYS_AFTER_DELIVERY", "7"))
# Minimum positive balance required to generate a payout batch (in INR)
FITMEEZO_MIN_PAYOUT_THRESHOLD = Decimal(os.getenv("FITMEEZO_MIN_PAYOUT_THRESHOLD", "1.00"))
# Policy rules for gateway fee, return shipping, and discount funding
FITMEEZO_GATEWAY_FEE_ALLOCATION = os.getenv("FITMEEZO_GATEWAY_FEE_ALLOCATION", "platform")  # 'platform' | 'seller'
FITMEEZO_RETURN_SHIPPING_BEARER = os.getenv("FITMEEZO_RETURN_SHIPPING_BEARER", "platform")  # 'platform' | 'seller'
FITMEEZO_DISCOUNT_BEARER = os.getenv("FITMEEZO_DISCOUNT_BEARER", "platform")  # 'platform' | 'seller'
# Tax withholding rates (Section 194-O TDS & GST TCS); set to 0.00 until accountant confirmation
FITMEEZO_TDS_RATE_PERCENT = Decimal(os.getenv("FITMEEZO_TDS_RATE_PERCENT", "0.00"))
FITMEEZO_TCS_GST_RATE_PERCENT = Decimal(os.getenv("FITMEEZO_TCS_GST_RATE_PERCENT", "0.00"))

# ==============================================================================
# INVOICE PDF, PACKING SLIP & TAX RECORDS CONFIGURATION (PHASE 8)
# ==============================================================================
# Invoice number prefix configuration (customizable without altering previously issued numbers)
FITMEEZO_INVOICE_PREFIX = os.getenv("FITMEEZO_INVOICE_PREFIX", "FM-INV")
FITMEEZO_SELLER_INVOICE_PREFIX = os.getenv("FITMEEZO_SELLER_INVOICE_PREFIX", "FM-SINV")

# Issuance rule: controls when customer can download an issued tax/commercial invoice
# Options: 'on_payment_or_cod' (default), 'on_dispatch', 'on_delivery', 'always_on_placement'
FITMEEZO_INVOICE_ISSUANCE_RULE = os.getenv("FITMEEZO_INVOICE_ISSUANCE_RULE", "on_payment_or_cod")

# Commercial/Tax invoicing issuer model (pending final Owner & Chartered Accountant confirmation):
# 'platform_marketplace' - Fitmeezo issues unified customer commercial invoice on behalf of platform
# 'seller_direct' - Each seller issues independent commercial invoices to customer
# 'composite' - Parent invoice for customer; seller-specific package invoice for logistics & seller records
FITMEEZO_INVOICE_ISSUER_MODEL = os.getenv("FITMEEZO_INVOICE_ISSUER_MODEL", "composite")

# Platform Legal Issuer metadata (Empty strings if unconfigured; never fabricated)
FITMEEZO_LEGAL_ENTITY_NAME = os.getenv("FITMEEZO_LEGAL_ENTITY_NAME", "Fitmeezo India Retail")
FITMEEZO_PLATFORM_GSTIN = os.getenv("FITMEEZO_PLATFORM_GSTIN", "").strip()
FITMEEZO_PLATFORM_PAN = os.getenv("FITMEEZO_PLATFORM_PAN", "").strip()
FITMEEZO_PLATFORM_CIN = os.getenv("FITMEEZO_PLATFORM_CIN", "").strip()
FITMEEZO_REGISTERED_OFFICE_ADDRESS = os.getenv(
    "FITMEEZO_REGISTERED_OFFICE_ADDRESS",
    "Plot 42, Industrial Area Phase II, Bengaluru, Karnataka 560100, India"
)

# Customer support and grievance details for invoice documents
FITMEEZO_SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@fitmeezo.com")
FITMEEZO_SUPPORT_PHONE = os.getenv("SUPPORT_PHONE", "+91 940885323")
FITMEEZO_SUPPORT_HOURS = os.getenv("SUPPORT_HOURS", "Mon - Sat, 10:00 AM - 7:00 PM IST")

# ==============================================================================
# SELLER OTP & SMS GATEWAY CONFIGURATION (PROD: MSG91 / TEXTLOCAL / FAST2SMS)
# ==============================================================================
# Supported options: 'msg91' (recommended India DLT), 'textlocal', 'fast2sms', 'twilio', 'console' (dev), 'mock' (tests)
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console" if DEBUG else "").strip().lower()
SMS_API_KEY = os.getenv("SMS_API_KEY", "").strip() or os.getenv("SMS_AUTH_KEY", "").strip()
SMS_AUTH_KEY = SMS_API_KEY
SMS_SENDER_ID = os.getenv("SMS_SENDER_ID", "").strip()
SMS_TEMPLATE_ID = os.getenv("SMS_TEMPLATE_ID", "").strip()

# Development bypass flag (strictly ignored and disabled in production when DEBUG=False)
SELLER_OTP_DEV_MODE = os.getenv("SELLER_OTP_DEV_MODE", "True" if DEBUG else "False").strip().lower() in ("true", "1", "yes")




