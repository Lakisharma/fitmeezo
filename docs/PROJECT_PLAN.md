# Fitmeezo - Master Project Plan & Phase Roadmap

**Brand**: Fitmeezo  
**Tagline**: *SHOP SMART • LIVE BETTER*  
**Brand Palette**: Primary Orange (`#FF6B00`), Jet Black (`#121212`), Pure White (`#FFFFFF`), Light Grey (`#F5F5F7` / `#E5E7EB`)  
**Target Market**: India  
**Currency**: Indian Rupee (INR - ₹)  
**Core Business Strategy**: Initial direct brand commerce with planned expansion into a multi-category, multi-seller marketplace.

---

## Technical Stack Architecture

- **Backend**: Python 3.12+ & Django 5.x
- **API Engine**: Django REST Framework (DRF)
- **Primary Database**: PostgreSQL (Production & Staging), with automatic SQLite local development fallback
- **Frontend / Storefront**: Django Templates, Bootstrap 5, Custom CSS Design System, Vanilla JavaScript
- **Mobile Application**: Flutter (Android/iOS) — targeted after completion of the unified DRF API layer
- **Architecture Philosophy**: Single unified backend and database supporting both the web storefront and the future mobile app.

---

## The 20 Website Development Phases

### Phase 1: Project Foundation & Architecture (Current Phase)
- Initialize clean repository structure (`backend/`, `docs/`, `mobile_app/`).
- Establish Python virtual environment, package dependencies (`Django`, `djangorestframework`, `psycopg`, `python-dotenv`).
- Configure environment variable handling with automatic PostgreSQL / SQLite fallback.
- Build initial reusable `core` app with health-check endpoint (`/health/`) and automated test suite.
- Document master project plan and Windows setup guidelines.

### Phase 2: Design System & Core Brand Assets
- Define Fitmeezo brand design tokens in CSS (Orange, Black, White, Light Grey).
- Set up base responsive layout (`base.html`), header with search bar, announcement ribbon, navigation bar, and footer.
- Integrate Google Fonts (Inter / Outfit) and modern SVG icon system.
- Build context processors for brand-wide metadata and currency symbols (`₹`).

### Phase 3: Custom User Authentication & Profile Management
- Implement custom `User` model utilizing email as primary identifier with optional Indian mobile number support.
- User registration, login, logout, password reset, and profile management views.
- Indian address book model (support for PIN code, state, city, street, and address type - Home/Work).
- Session security and role distinction (Customer, Staff, Seller/Vendor).

### Phase 4: Product Catalog Architecture & Multi-Category Models
- Category hierarchy model (Parent Category -> Child Subcategory) with slugs and SEO metadata.
- Product model with title, brand, SKU, base price, discount price, and GST slab.
- Variant architecture (Size, Color, Material, Dimensions) supporting multi-category retail.
- Category navigation tree and breadcrumb generation.

### Phase 5: Product Media & Asset Management
- Multiple image uploads per product with primary/cover flag.
- Automatic image thumbnail generation and WebP conversion.
- Media storage abstraction (Local media for dev, Cloud/S3-compatible for production).
- Alt-text and accessibility tags for product imagery.

### Phase 6: Storefront Homepage & Brand Discovery
- Dynamic hero banner carousel with promotional call-to-actions.
- Featured categories grid and "Trending Now" product highlights.
- Brand value proposition highlights (Fast Delivery across India, Best Price Guarantee, Secure UPI Payments).
- Newsletter signup and promotional alerts.

### Phase 7: Product Listing, Filtering & Faceted Search
- Dynamic Product Listing Page (PLP) with responsive grid and list layouts.
- Faceted filtering: Category, Price Range (in INR), In-Stock status, and Rating.
- Multi-parameter sorting: Featured, Price: Low to High, Price: High to Low, Newest Arrivals.
- Full-text search across titles, descriptions, and SKU tags.

### Phase 8: Product Detail Page (PDP) & Customer Experience
- High-resolution image zoom and carousel preview.
- Dynamic variant selector (swatches for colors, buttons for sizes) with real-time stock and price updates.
- Indian PIN code delivery checker (serviceability simulation).
- Specifications table, return policy summary, and trust badges.

### Phase 9: Shopping Cart Engine (Session & Database Sync)
- Unified cart architecture: Anonymous session cart syncing automatically on login.
- Real-time quantity adjustments, cart item removal, and subtotal/tax calculation.
- Sticky mini-cart drawer and full cart review page.
- Out-of-stock and maximum-purchase-quantity validation.

### Phase 10: Wishlist & Saved Items
- Customer wishlist data model with one-click toggle from PLP and PDP.
- Dedicated "My Wishlist" interface with instant "Move to Cart" workflow.
- Wishlist item stock status notifications.

### Phase 11: Coupons, Discounts & Promotional Offers
- Coupon code engine: Percentage discount, flat INR discount, and minimum order value thresholds.
- Single-use and multi-use coupon rules with expiry dates.
- Real-time coupon application and instant breakdown in cart and checkout.

### Phase 12: Checkout Flow & Indian Address Validation
- Streamlined, high-conversion checkout flow.
- Saved address selection and new address entry with Indian state/PIN code validation.
- Delivery options (Standard Shipping, Express Shipping).
- Order summary with itemized GST, shipping charges, and discount deductions.

### Phase 13: Order Management & Lifecycle Tracking
- Order and OrderItem models with unique, human-readable Order IDs (e.g., `FMZ-2026-XXXX`).
- Order status state machine: `Placed`, `Confirmed`, `Processing`, `Shipped`, `Out for Delivery`, `Delivered`, `Cancelled`, `Returned`.
- Customer "My Orders" dashboard with detailed order tracking timeline and printable invoices.

### Phase 14: Payment Gateway Integration (India-Focused)
- Integration with major Indian payment gateways (Razorpay / Cashfree / PhonePe / Stripe).
- Support for UPI (Google Pay, PhonePe, Paytm), Credit/Debit Cards, Net Banking, and Cash on Delivery (COD).
- Secure webhook listener for asynchronous payment verification and signature validation.
- Failed payment recovery workflow.

### Phase 15: Automated Communications & Notifications
- Automated email triggers for Order Confirmation, Dispatch, and Delivery.
- Branded HTML email templates featuring Fitmeezo colors and logo.
- SMS / WhatsApp notification integration placeholders for Indian mobile numbers.
- Automated PDF invoice generation.

### Phase 16: Customer Reviews, Ratings & Social Proof
- Verified purchase review model (1 to 5 star rating, title, comments, and buyer photos).
- Customer review submission on completed orders.
- Review moderation system in Django admin.
- Aggregate rating calculations and breakdown display on PDP.

### Phase 17: Seller & Multi-Vendor Architecture Foundation
- Seller/Vendor profile model (GSTIN, PAN, business details, bank account info).
- Product ownership association (Fitmeezo direct inventory vs. Third-Party Seller inventory).
- Vendor payout and commission calculation logic.
- Vendor inventory isolation and multi-warehouse stock tracking.

### Phase 18: Admin Dashboard, Operations & Business Analytics
- Customized Django Admin interface with Fitmeezo brand theme.
- Operational dashboards: Daily sales (INR), top-selling categories, low stock alerts, pending orders.
- CSV export for orders, accounting, and tax filing (GST reports).

### Phase 19: Comprehensive REST API Suite for Mobile Integration
- Complete Django REST Framework API implementation with Token / JWT authentication:
  - Auth APIs: Login, Register, Profile, Address management.
  - Catalog APIs: Categories, Products, Filters, Search.
  - Cart & Checkout APIs: Add, update, remove, apply coupon, place order.
  - Payment APIs: Create payment order, verify payment signature.
  - Order APIs: Order history, tracking, reviews.
- API documentation via OpenAPI / Swagger / DRF Browsable API.

### Phase 20: Performance Optimization, Security Hardening & Launch Readiness
- Redis caching for product catalog, categories, and session data.
- PostgreSQL query optimization with indexes on slugs, categories, and foreign keys.
- Security hardening: CSRF, secure cookies, rate limiting, Content Security Policy (CSP).
- Production deployment configuration (Gunicorn, Nginx, WhiteNoise/CDN, SSL).
- Pre-launch quality assurance and end-to-end testing.

---

## Future Phase: Flutter Mobile Application Development

Following the completion of the unified DRF API in Phase 19 and platform hardening in Phase 20:
- **Framework**: Flutter (Dart) targeting Android (APK & App Bundle) and iOS.
- **Backend Consumption**: 100% shared API endpoints and database with zero duplication of business logic.
- **Features**:
  - Native splash screen and onboarding with Fitmeezo brand styling (`#FF6B00`, `#121212`).
  - Seamless authentication and persistent biometric/token login.
  - Native product browsing, image gestures, cart, and checkout.
  - Razorpay/Cashfree native SDK or UPI intent payment integration.
  - Push notifications for order dispatch, price drops, and flash sales.
