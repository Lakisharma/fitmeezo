from decimal import Decimal
from io import StringIO
from unittest.mock import patch, MagicMock
from urllib.parse import quote
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APIClient

from core.cart import Cart as CartService
from core.models import (
    Address,
    Cart,
    CartItem,
    Category,
    CustomerProfile,
    Product,
    ProductImage,
    ProductReview,
    ProductVariant,
    WishlistItem,
    Order,
    OrderItem,
)


class HealthCheckEndpointTest(TestCase):
    """
    Test suite for the Fitmeezo system health check endpoint.
    """

    def setUp(self):
        self.client = APIClient()

    def test_health_check_returns_200(self):
        """Test that /health/ returns HTTP 200 OK."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_health_check_payload_structure(self):
        """Test that the response contains expected brand, database, and status fields."""
        response = self.client.get("/health/")
        data = response.json()

        self.assertIn("status", data)
        self.assertEqual(data["status"], "ok")

        self.assertIn("app", data)
        self.assertEqual(data["app"], "Fitmeezo")

        self.assertIn("tagline", data)
        self.assertEqual(data["tagline"], "SHOP SMART • LIVE BETTER")

        self.assertIn("currency", data)
        self.assertEqual(data["currency"], "INR")

        self.assertIn("database_engine", data)
        self.assertIn(data["database_engine"], ["sqlite", "postgresql"])

        self.assertIn("environment", data)
        self.assertIn("timestamp", data)

    def test_health_check_via_named_reverse(self):
        """Test resolution via Django url reverse name."""
        url = reverse("health-check")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class SharedLayoutAndDesignSystemTest(TestCase):
    """
    Test suite for Fitmeezo shared website layout, base templates,
    and design system components (Phase 1, Prompt 2).
    """

    def setUp(self):
        self.client = APIClient()

    def test_home_preview_endpoint_returns_200(self):
        """Test that the root URL '/' successfully loads the preview layout."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_home_named_reverse(self):
        """Test that the reverse URL for 'core:home' resolves to '/'."""
        url = reverse("core:home")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_brand_constants_rendered_in_layout(self):
        """Verify that brand name, tagline, and currency symbol render into template."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        self.assertIn("Fitmeezo", content)
        self.assertIn("SHOP SMART • LIVE BETTER", content)
        self.assertIn("INR", content)
        self.assertIn("₹", content)

    def test_announcement_bar_present(self):
        """Verify the top announcement bar renders with required message."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        self.assertIn("fmAnnouncementBar", content)
        self.assertIn("Welcome to Fitmeezo — Shop Smart, Live Better", content)

    def test_header_components_present(self):
        """Verify header elements: search bar, logo, account, wishlist, and cart."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        # Search bar
        self.assertIn("fmSearchForm", content)
        self.assertIn("fmSearchInput", content)

        # Wordmark / logo asset link
        self.assertIn("fitmeezo-logo.svg", content)

        # Actions: Account, Wishlist, Cart
        self.assertIn("Sign In", content)
        self.assertIn("Wishlist", content)
        self.assertIn("Cart", content)

    def test_sample_categories_present(self):
        """Verify that the required 6 categories are rendered in the navigation."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        categories = [
            "Electronics",
            "Fashion",
            "Home &amp; Living",
            "Beauty",
            "Sports",
            "Baby &amp; Kids",
        ]
        for category in categories:
            self.assertIn(category, content)

    def test_footer_sections_present(self):
        """Verify footer policies, support contacts, and legal notices."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        footer_items = [
            "Customer Service",
            "About Us",
            "Contact Us",
            "Shipping Policy",
            "Return &amp; Refund Policy",
            "Privacy Policy",
            "Terms &amp; Conditions",
            "1800-123-456",
            "support@fitmeezo.com",
        ]
        for item in footer_items:
            self.assertIn(item, content)

    def test_static_asset_files_discoverable(self):
        """Verify static CSS, JS, and SVG assets are found and resolved by Django staticfiles."""
        import os
        from django.contrib.staticfiles import finders

        static_assets = [
            "css/theme.css",
            "css/layout.css",
            "css/components.css",
            "css/homepage.css",
            "js/main.js",
            "images/brand/fitmeezo-logo.svg",
            "images/brand/favicon.svg",
            "images/brand/product-fallback.svg",
        ]
        for asset in static_assets:
            found_path = finders.find(asset)
            self.assertIsNotNone(
                found_path,
                f"Static asset '{asset}' could not be located by Django staticfiles finders."
            )
            self.assertTrue(
                os.path.exists(found_path),
                f"Static file '{found_path}' does not exist on filesystem."
            )
            self.assertGreater(
                os.path.getsize(found_path),
                0,
                f"Static file '{found_path}' is unexpectedly empty."
            )


class HomepageStorefrontTest(TestCase):
    """
    Test suite for Fitmeezo actual e-commerce storefront homepage (Phase 1, Prompt 3).
    Verifies all 8 sections: Hero, Campaign, Categories, Featured Products,
    Benefits, Trending Products, Newsletter, and Final CTA.
    """

    def setUp(self):
        self.client = APIClient()
        self.response = self.client.get("/")
        self.content = self.response.content.decode("utf-8")

    def test_homepage_status_code(self):
        """Verify homepage returns HTTP 200 OK."""
        self.assertEqual(self.response.status_code, status.HTTP_200_OK)

    def test_section_1_hero_rendering(self):
        """Verify hero section headline, subtext, dual CTAs, and spotlight deal."""
        self.assertIn("Smart Value for", self.content)
        self.assertIn("Shop Now", self.content)
        self.assertIn("Browse Categories", self.content)
        self.assertIn("fm-hero-spotlight", self.content)
        self.assertIn("FitPulse Pro Max Smartwatch", self.content)

    def test_section_2_campaign_banner(self):
        """Verify promotional sales campaign banner with sample coupon code."""
        self.assertIn("fm-promo-campaign", self.content)
        self.assertIn("Great Indian Smart Savings Festival", self.content)
        self.assertIn("SMARTFIT200", self.content)

    def test_section_3_category_grid(self):
        """Verify shopping category grid with 6 core categories."""
        self.assertIn("category-grid", self.content)
        self.assertIn("Shop by Category", self.content)
        categories = ["Electronics", "Fashion", "Home &amp; Living", "Beauty", "Sports", "Baby &amp; Kids"]
        for cat in categories:
            self.assertIn(cat, self.content)

    def test_section_4_featured_products(self):
        """Verify featured products section and reusable product card elements."""
        self.assertIn("featured-products", self.content)
        self.assertIn("Featured Products", self.content)
        self.assertIn("SonicBass Wireless Noise Cancelling", self.content)
        self.assertIn("AeroGrip Performance Lightweight", self.content)
        self.assertIn("PureGlow Vitamin C", self.content)
        self.assertIn("Add to Cart", self.content)

    def test_section_5_benefits_strip(self):
        """Verify 4 core customer value propositions."""
        self.assertIn("fm-benefits-strip", self.content)
        self.assertIn("Free Delivery Across India", self.content)
        self.assertIn("100% Verified Genuine", self.content)
        self.assertIn("Easy 7-Day Replacement", self.content)
        self.assertIn("24/7 Dedicated Support", self.content)

    def test_section_6_trending_products(self):
        """Verify trending products section."""
        self.assertIn("trending-products", self.content)
        self.assertIn("Popular Demands", self.content)
        self.assertIn("PureLoom 300TC", self.content)
        self.assertIn("ProGrip Adjustable Hex", self.content)

    def test_section_7_newsletter_signup(self):
        """Verify newsletter signup card and email input."""
        self.assertIn("fmNewsletterForm", self.content)
        self.assertIn("fmNewsletterEmail", self.content)
        self.assertIn("Never Miss a Smart Saving", self.content)
        self.assertIn("Subscribe", self.content)

    def test_section_8_final_shopping_cta(self):
        """Verify final shopping call to action banner before footer."""
        self.assertIn("fm-final-cta-section", self.content)
        self.assertIn("Ready to Shop Smarter?", self.content)
        self.assertIn("Explore All Categories", self.content)
        self.assertIn("Browse Today's Deals", self.content)


class MobileResponsiveRedesignTest(TestCase):
    """
    Test suite for Fitmeezo mobile homepage redesign and responsive enhancements.
    Validates mobile bottom navigation, compact horizontal category strip,
    header touch targets, and two-column product display configuration.
    """

    def setUp(self):
        self.client = APIClient()
        self.response = self.client.get("/")
        self.content = self.response.content.decode("utf-8")

    def test_mobile_bottom_navigation_bar_present(self):
        """Verify mobile bottom navigation contains all 5 key actions and badges."""
        self.assertIn("fm-bottom-nav", self.content)
        self.assertIn("fm-bottom-nav-inner", self.content)

        # 5 Navigation items
        self.assertIn("Home", self.content)
        self.assertIn("Categories", self.content)
        self.assertIn("Wishlist", self.content)
        self.assertIn("Account", self.content)
        self.assertIn("Cart", self.content)

        # Active home item with valid reverse URL
        home_url = reverse("core:home")
        self.assertIn(f'href="{home_url}"', self.content)
        self.assertIn("fm-bottom-nav-item is-active", self.content)

        # Categories drawer trigger
        self.assertIn("fmBottomCategoriesBtn", self.content)
        self.assertIn('aria-controls="fmMobileDrawer"', self.content)

        # Badge counters for Wishlist and Cart
        self.assertIn("fm-bottom-wishlist-badge", self.content)
        self.assertIn("fm-bottom-cart-badge", self.content)

    def test_mobile_category_strip_present(self):
        """Verify compact horizontal category strip contains all required departments."""
        self.assertIn("fm-mobile-cat-strip", self.content)
        self.assertIn("fm-mobile-cat-scroll", self.content)

        required_categories = [
            "Electronics",
            "Fashion",
            "Home &amp; Living",
            "Beauty",
            "Sports",
            "Baby &amp; Kids",
        ]
        for category in required_categories:
            self.assertIn(category, self.content)

        # Initial active state on category strip
        self.assertIn('class="fm-mobile-cat-item is-active"', self.content)
        self.assertIn('aria-selected="true"', self.content)

    def test_header_mobile_touch_targets_and_accessibility(self):
        """Verify header controls maintain accessible labels and IDs."""
        self.assertIn('id="fmMenuToggle"', self.content)
        self.assertIn('aria-label="Open mobile navigation menu"', self.content)
        self.assertIn('id="fmSearchInput"', self.content)
        self.assertIn('aria-label="Search Fitmeezo catalogue"', self.content)

    def test_mobile_css_tokens_and_declarations(self):
        """Verify static CSS files contain mobile bottom nav, category strip, and two-column rules."""
        import os
        from django.contrib.staticfiles import finders

        # Check layout.css contains bottom nav and safe-area insets
        layout_path = finders.find("css/layout.css")
        self.assertIsNotNone(layout_path)
        with open(layout_path, "r", encoding="utf-8") as f:
            layout_css = f.read()

        self.assertIn(".fm-bottom-nav", layout_css)
        self.assertIn("safe-area-inset-bottom", layout_css)
        self.assertIn(".fm-mobile-cat-strip", layout_css)
        self.assertIn("overflow-x: auto", layout_css)

        # Check homepage.css contains two-column product grid
        homepage_path = finders.find("css/homepage.css")
        self.assertIsNotNone(homepage_path)
        with open(homepage_path, "r", encoding="utf-8") as f:
            homepage_css = f.read()

        self.assertIn("grid-template-columns: repeat(2, 1fr)", homepage_css)


class CategoryModelTest(TestCase):
    """
    Test suite for the Fitmeezo Category model (Task 1).
    Validates model creation, unique slugs, ordering, indexing, and helper properties.
    """

    def test_category_creation_and_attributes(self):
        """Verify Category can be created with required and optional fields."""
        category = Category.objects.create(
            name="Electronics",
            slug="electronics",
            description="Gadgets, audio, and laptops",
            display_order=1,
            is_active=True,
        )
        self.assertEqual(str(category), "Electronics")
        self.assertEqual(category.slug, "electronics")
        self.assertTrue(category.is_active)
        self.assertEqual(category.display_order, 1)
        self.assertIsNotNone(category.created_at)
        self.assertIsNotNone(category.updated_at)

    def test_category_slug_auto_generated_if_empty(self):
        """Verify slug is automatically populated from name when left blank."""
        category = Category.objects.create(
            name="Home & Living",
            slug="",
        )
        self.assertEqual(category.slug, "home-living")

    def test_category_unique_slug_enforced(self):
        """Verify database enforces unique slug constraint."""
        Category.objects.create(name="Fashion", slug="fashion")
        with self.assertRaises(IntegrityError):
            Category.objects.create(name="Another Fashion", slug="fashion")

    def test_category_default_ordering(self):
        """Verify default ordering is by display_order ascending, then name."""
        c3 = Category.objects.create(name="Sports", slug="sports", display_order=30)
        c1 = Category.objects.create(name="Beauty", slug="beauty", display_order=10)
        c2 = Category.objects.create(name="Books", slug="books", display_order=10)

        categories = list(Category.objects.all())
        self.assertEqual(categories, [c1, c2, c3])

    def test_category_get_absolute_url(self):
        """Verify get_absolute_url returns clean category detail URL."""
        category = Category.objects.create(name="Beauty", slug="beauty")
        self.assertEqual(category.get_absolute_url(), "/categories/beauty/")

    def test_category_icon_and_badge_properties(self):
        """Verify helper icon_class, icon alias, and badge properties."""
        cat_elec = Category.objects.create(name="Electronics", slug="electronics")
        self.assertEqual(cat_elec.icon_class, "bi-laptop")
        self.assertEqual(cat_elec.icon, "bi-laptop")
        self.assertEqual(cat_elec.badge, "Hot")

        cat_custom = Category.objects.create(name="Custom Goods", slug="custom-goods")
        self.assertEqual(cat_custom.icon_class, "bi-tag")
        self.assertEqual(cat_custom.badge, "")


class CategorySeedCommandTest(TestCase):
    """
    Test suite for seed_categories management command (Task 3).
    Validates seeding of 6 core categories and idempotency.
    """

    def test_seed_categories_creates_all_six(self):
        """Verify seed_categories creates the 6 required categories."""
        out = StringIO()
        call_command("seed_categories", stdout=out)

        self.assertEqual(Category.objects.count(), 6)
        expected_slugs = ["electronics", "fashion", "home-living", "beauty", "sports", "baby-kids"]
        existing_slugs = list(Category.objects.values_list("slug", flat=True))
        for slug in expected_slugs:
            self.assertIn(slug, existing_slugs)

    def test_seed_categories_is_idempotent(self):
        """Verify running seed_categories multiple times does not duplicate records."""
        out = StringIO()
        call_command("seed_categories", stdout=out)
        self.assertEqual(Category.objects.count(), 6)

        # Run a second time
        call_command("seed_categories", stdout=out)
        self.assertEqual(Category.objects.count(), 6)

    def test_seed_categories_preserves_custom_settings(self):
        """Verify existing category customizations are not overwritten on re-run."""
        out = StringIO()
        call_command("seed_categories", stdout=out)

        # Modify a category's description and display order
        cat = Category.objects.get(slug="fashion")
        cat.description = "Customized Boutique Fashion"
        cat.display_order = 99
        cat.save()

        # Re-run seed command
        call_command("seed_categories", stdout=out)

        cat.refresh_from_db()
        self.assertEqual(cat.description, "Customized Boutique Fashion")
        self.assertEqual(cat.display_order, 99)


class CategoryPagesAndViewsTest(TestCase):
    """
    Test suite for category listing and detail views (Tasks 4, 5, 6).
    Validates URL routing, active filtering, 404 behavior, pagination readiness, and empty states.
    """

    def setUp(self):
        self.client = APIClient()
        self.cat_active = Category.objects.create(
            name="Electronics",
            slug="electronics",
            description="Smartphones, audio, and computing gear",
            is_active=True,
            display_order=1,
        )
        self.cat_inactive = Category.objects.create(
            name="Archived Goods",
            slug="archived-goods",
            description="Inactive warehouse stock",
            is_active=False,
            display_order=99,
        )

    def test_category_list_url_resolution_and_status(self):
        """Verify /categories/ resolves and returns 200 OK."""
        url = reverse("core:category_list")
        self.assertEqual(url, "/categories/")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_category_list_shows_only_active_categories(self):
        """Verify category index excludes inactive categories."""
        response = self.client.get("/categories/")
        content = response.content.decode("utf-8")

        self.assertIn("Electronics", content)
        self.assertNotIn("Archived Goods", content)

    def test_category_list_empty_state_when_no_active(self):
        """Verify empty state is shown if all categories are inactive or deleted."""
        Category.objects.filter(is_active=True).delete()
        response = self.client.get("/categories/")
        content = response.content.decode("utf-8")

        self.assertIn("No Categories Found", content)

    def test_category_detail_url_resolution_and_content(self):
        """Verify /categories/<slug>/ resolves and returns 200 with category details."""
        url = reverse("core:category_detail", kwargs={"slug": "electronics"})
        self.assertEqual(url, "/categories/electronics/")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")

        self.assertIn("Electronics", content)
        self.assertIn("Smartphones, audio, and computing gear", content)
        self.assertIn("Breadcrumb navigation", content)

    def test_category_detail_shows_empty_state_for_zero_products(self):
        """Verify empty state is shown since products are not yet catalogued."""
        response = self.client.get("/categories/electronics/")
        content = response.content.decode("utf-8")

        self.assertIn("No Products in This Category Yet", content)
        self.assertIn("Browse All Categories", content)
        self.assertIn("Back to Homepage", content)

    def test_category_detail_returns_404_for_inactive_category(self):
        """Verify inactive category returns HTTP 404 Not Found."""
        url = reverse("core:category_detail", kwargs={"slug": "archived-goods"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_category_detail_returns_404_for_unknown_slug(self):
        """Verify unknown category slug returns HTTP 404 Not Found."""
        response = self.client.get("/categories/non-existent-category/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_category_detail_pagination_readiness(self):
        """Verify view supplies pagination structure (page_obj) to template context."""
        response = self.client.get("/categories/electronics/")
        self.assertIn("page_obj", response.context)
        self.assertIn("total_products_count", response.context)
        self.assertEqual(response.context["total_products_count"], 0)


class CategoryFrontendIntegrationTest(TestCase):
    """
    Test suite for frontend category integration across homepage, navigation, and drawer (Task 5).
    """

    def setUp(self):
        self.client = APIClient()
        out = StringIO()
        call_command("seed_categories", stdout=out)

    def test_all_six_category_urls_accessible(self):
        """Verify each of the 6 core category URLs returns HTTP 200 OK."""
        expected_slugs = [
            "electronics",
            "fashion",
            "home-living",
            "beauty",
            "sports",
            "baby-kids",
        ]
        for slug in expected_slugs:
            url = f"/categories/{slug}/"
            response = self.client.get(url)
            self.assertEqual(
                response.status_code,
                status.HTTP_200_OK,
                f"URL {url} failed with status {response.status_code}",
            )

    def test_homepage_links_to_real_category_pages(self):
        """Verify homepage renders real category links pointing to /categories/<slug>/."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        self.assertIn('/categories/electronics/', content)
        self.assertIn('/categories/fashion/', content)
        self.assertIn('/categories/home-living/', content)
        self.assertIn('/categories/beauty/', content)
        self.assertIn('/categories/sports/', content)
        self.assertIn('/categories/baby-kids/', content)
        self.assertIn('/categories/', content)


class ProductModelTest(TestCase):
    """
    Test suite for the Fitmeezo Product model (Task 1).
    Validates model fields, SKU uniqueness, Decimal price precision,
    discount calculation, stock constraints, and category relationship.
    """

    def setUp(self):
        self.category = Category.objects.create(
            name="Electronics",
            slug="electronics",
        )

    def test_product_creation_and_attributes(self):
        """Verify Product can be created with all valid fields."""
        product = Product.objects.create(
            name="SonicBass Wireless Headphones",
            slug="sonicbass-wireless-headphones",
            sku="EL-HD-001",
            category=self.category,
            short_description="Active noise cancelling headphones",
            description="Premium wireless over-ear headphones with 40h battery.",
            price=Decimal("1999.00"),
            compare_at_price=Decimal("3999.00"),
            stock_quantity=25,
            is_active=True,
            is_featured=True,
        )
        self.assertEqual(str(product), "SonicBass Wireless Headphones (EL-HD-001)")
        self.assertEqual(product.sku, "EL-HD-001")
        self.assertEqual(product.price, Decimal("1999.00"))
        self.assertEqual(product.stock_quantity, 25)
        self.assertTrue(product.is_in_stock)
        self.assertTrue(product.is_featured)
        self.assertEqual(product.category, self.category)

    def test_product_sku_uniqueness(self):
        """Verify SKU uniqueness is enforced."""
        Product.objects.create(
            name="Product A",
            sku="SKU-DUP-1",
            category=self.category,
            price=Decimal("499.00"),
        )
        with self.assertRaises((IntegrityError, ValidationError)):
            Product.objects.create(
                name="Product B",
                sku="SKU-DUP-1",
                category=self.category,
                price=Decimal("799.00"),
            )

    def test_product_slug_auto_generated(self):
        """Verify slug is auto-generated from name if left empty."""
        product = Product.objects.create(
            name="Smart Fitness Tracker Band",
            slug="",
            sku="EL-FIT-001",
            category=self.category,
            price=Decimal("1499.00"),
        )
        self.assertEqual(product.slug, "smart-fitness-tracker-band")

    def test_product_discount_percentage_calculation(self):
        """Verify discount percentage calculates correctly from prices."""
        product = Product.objects.create(
            name="Discounted Gadget",
            sku="EL-DSC-001",
            category=self.category,
            price=Decimal("1000.00"),
            compare_at_price=Decimal("2000.00"),
        )
        self.assertEqual(product.discount_percentage, 50)
        self.assertEqual(product.discount_percent, 50)

    def test_product_discount_zero_when_no_compare_at_price(self):
        """Verify discount is 0 when compare_at_price is not provided."""
        product = Product.objects.create(
            name="Regular Priced Item",
            sku="EL-REG-001",
            category=self.category,
            price=Decimal("999.00"),
            compare_at_price=None,
        )
        self.assertEqual(product.discount_percentage, 0)

    def test_product_compare_at_price_lower_than_price_raises_validation_error(self):
        """Verify compare_at_price cannot be lower than selling price."""
        product = Product(
            name="Invalid Price Item",
            sku="EL-INV-001",
            category=self.category,
            price=Decimal("1500.00"),
            compare_at_price=Decimal("1000.00"),
        )
        with self.assertRaises(ValidationError):
            product.clean()

    def test_product_negative_price_raises_validation_error(self):
        """Verify price cannot be negative."""
        product = Product(
            name="Negative Price Item",
            sku="EL-NEG-001",
            category=self.category,
            price=Decimal("-10.00"),
        )
        with self.assertRaises(ValidationError):
            product.clean()

    def test_product_negative_stock_raises_validation_error(self):
        """Verify stock quantity cannot be negative."""
        product = Product(
            name="Negative Stock Item",
            sku="EL-STK-001",
            category=self.category,
            price=Decimal("100.00"),
            stock_quantity=-5,
        )
        with self.assertRaises(ValidationError):
            product.clean()

    def test_product_is_in_stock_flag(self):
        """Verify is_in_stock reflects stock quantity."""
        in_stock = Product.objects.create(
            name="Available Item",
            sku="EL-AVL-001",
            category=self.category,
            price=Decimal("500.00"),
            stock_quantity=10,
        )
        self.assertTrue(in_stock.is_in_stock)

        out_of_stock = Product.objects.create(
            name="Sold Out Item",
            sku="EL-SLD-001",
            category=self.category,
            price=Decimal("500.00"),
            stock_quantity=0,
        )
        self.assertFalse(out_of_stock.is_in_stock)

    def test_product_category_reverse_relationship(self):
        """Verify category.products relation works properly."""
        p1 = Product.objects.create(
            name="Item 1",
            sku="SKU-REL-1",
            category=self.category,
            price=Decimal("100.00"),
        )
        p2 = Product.objects.create(
            name="Item 2",
            sku="SKU-REL-2",
            category=self.category,
            price=Decimal("200.00"),
        )
        self.assertEqual(list(self.category.products.all().order_by("sku")), [p1, p2])


class ProductQueryAndVisibilityTest(TestCase):
    """
    Test suite for product queries across storefront views (Task 3).
    Validates active/inactive filtering, featured queries, category isolation, and 404 behavior.
    """

    def setUp(self):
        self.client = APIClient()
        self.cat_elec = Category.objects.create(name="Electronics", slug="electronics")
        self.cat_fashion = Category.objects.create(name="Fashion", slug="fashion")

        self.p_active_feat = Product.objects.create(
            name="Active Featured Smartwatch",
            slug="active-featured-smartwatch",
            sku="SKU-AF-01",
            category=self.cat_elec,
            price=Decimal("2999.00"),
            is_active=True,
            is_featured=True,
        )
        self.p_inactive_feat = Product.objects.create(
            name="Inactive Featured Item",
            slug="inactive-featured-item",
            sku="SKU-IF-01",
            category=self.cat_elec,
            price=Decimal("1999.00"),
            is_active=False,
            is_featured=True,
        )
        self.p_fashion = Product.objects.create(
            name="Cotton Casual Shirt",
            slug="cotton-casual-shirt",
            sku="SKU-FSH-01",
            category=self.cat_fashion,
            price=Decimal("899.00"),
            is_active=True,
            is_featured=False,
        )

    def test_homepage_shows_active_featured_products(self):
        """Verify homepage renders active featured products and excludes inactive ones."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        self.assertIn("Active Featured Smartwatch", content)
        self.assertNotIn("Inactive Featured Item", content)

    def test_category_detail_shows_only_category_active_products(self):
        """Verify category detail shows active items belonging to category and excludes other categories."""
        response = self.client.get("/categories/electronics/")
        content = response.content.decode("utf-8")

        self.assertIn("Active Featured Smartwatch", content)
        self.assertNotIn("Inactive Featured Item", content)
        self.assertNotIn("Cotton Casual Shirt", content)

    def test_category_detail_empty_state_when_zero_products(self):
        """Verify empty state is preserved when category has 0 active products."""
        cat_empty = Category.objects.create(name="Beauty", slug="beauty")
        response = self.client.get("/categories/beauty/")
        content = response.content.decode("utf-8")

        self.assertIn("No Products in This Category Yet", content)

    def test_product_detail_view_returns_200(self):
        """Verify /products/<slug>/ resolves and returns 200 with product details."""
        url = reverse("core:product_detail", kwargs={"slug": "active-featured-smartwatch"})
        self.assertEqual(url, "/products/active-featured-smartwatch/")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode("utf-8")

        self.assertIn("Active Featured Smartwatch", content)
        self.assertIn("SKU-AF-01", content)
        self.assertIn("2999.00", content)

    def test_product_detail_view_returns_404_for_inactive_product(self):
        """Verify inactive product detail returns 404."""
        url = reverse("core:product_detail", kwargs={"slug": "inactive-featured-item"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_product_detail_view_returns_404_for_unknown_slug(self):
        """Verify unknown product slug returns 404."""
        response = self.client.get("/products/non-existent-product/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ProductCardRenderingTest(TestCase):
    """
    Test suite for reusable product_card.html (Task 4).
    Validates prices, discounts, stock state, out of stock badge, and honest reviews.
    """

    def setUp(self):
        self.client = APIClient()
        self.category = Category.objects.create(name="Sports", slug="sports")

    def test_product_card_out_of_stock_state(self):
        """Verify out of stock product renders disabled button and out of stock chip."""
        Product.objects.create(
            name="Adjustable Dumbbell Set",
            slug="adjustable-dumbbell-set",
            sku="SP-DB-001",
            category=self.category,
            price=Decimal("1999.00"),
            stock_quantity=0,
            is_active=True,
            is_featured=True,
        )
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        self.assertIn("Adjustable Dumbbell Set", content)
        self.assertIn("Out of Stock", content)
        self.assertIn("is-disabled", content)

    def test_product_card_shows_discount_chip_when_compare_at_price_higher(self):
        """Verify discount percentage chip appears only when compare_at_price > price."""
        Product.objects.create(
            name="Yoga Mat Non-Slip",
            slug="yoga-mat-non-slip",
            sku="SP-YM-001",
            category=self.category,
            price=Decimal("500.00"),
            compare_at_price=Decimal("1000.00"),
            stock_quantity=15,
            is_active=True,
            is_featured=True,
        )
        response = self.client.get("/")
        content = response.content.decode("utf-8")

        self.assertIn("Yoga Mat Non-Slip", content)
        self.assertIn("50% OFF", content)
        self.assertIn("1000.00", content)


class ProductAdminTest(TestCase):
    """
    Test suite for ProductAdmin configuration (Task 2).
    """

    def setUp(self):
        from django.contrib.admin.sites import site
        self.admin = site._registry[Product]
        self.category = Category.objects.create(name="Beauty", slug="beauty")
        self.product = Product.objects.create(
            name="Organic Face Wash",
            sku="BT-FW-001",
            category=self.category,
            price=Decimal("349.00"),
            compare_at_price=Decimal("499.00"),
            stock_quantity=5,
        )

    def test_admin_list_display_and_search_fields(self):
        """Verify required fields are registered in admin list_display and search_fields."""
        self.assertIn("name", self.admin.list_display)
        self.assertIn("sku", self.admin.list_display)
        self.assertIn("category", self.admin.list_display)
        self.assertIn("formatted_price", self.admin.list_display)
        self.assertIn("stock_status", self.admin.list_display)
        self.assertIn("is_active", self.admin.list_display)
        self.assertIn("is_featured", self.admin.list_display)

        self.assertIn("name", self.admin.search_fields)
        self.assertIn("sku", self.admin.search_fields)

    def test_admin_formatted_helpers(self):
        """Verify admin custom display helpers return expected values."""
        self.assertEqual(self.admin.formatted_price(self.product), "₹349.00")
        self.assertEqual(self.admin.stock_status(self.product), "Low Stock (5)")
        self.assertEqual(self.admin.calculated_discount(self.product), "30% OFF")


class ProductVariantModelTest(TestCase):
    """
    Test suite for ProductVariant model creation, normalization, constraints,
    effective price resolution, and discount calculation.
    """

    def setUp(self):
        self.category = Category.objects.create(name="Apparel", slug="apparel")
        self.product = Product.objects.create(
            name="Classic Cotton Crewneck T-Shirt",
            sku="AP-TSH-001",
            category=self.category,
            price=Decimal("499.00"),
            compare_at_price=Decimal("799.00"),
            stock_quantity=0,
        )

    def test_product_without_variants(self):
        """Verify standalone products without variants continue to work normally."""
        self.assertFalse(self.product.has_variants)
        self.assertEqual(self.product.active_variants.count(), 0)
        self.assertEqual(self.product.starting_price, Decimal("499.00"))
        self.assertEqual(self.product.available_inventory, 0)
        self.assertFalse(self.product.is_in_stock)

    def test_product_with_multiple_variants(self):
        """Verify creating multiple active variants for a parent product."""
        v_s = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="S",
            sku="AP-TSH-S",
            stock_quantity=10,
        )
        v_m = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="M",
            sku="AP-TSH-M",
            stock_quantity=15,
        )
        v_l = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="L",
            sku="AP-TSH-L",
            stock_quantity=0,
        )

        self.assertTrue(self.product.has_variants)
        self.assertEqual(self.product.active_variants.count(), 3)
        self.assertEqual(str(v_s), "Classic Cotton Crewneck T-Shirt - Size: S (AP-TSH-S)")

    def test_effective_price_fallback_to_parent(self):
        """Verify variant inherits parent price when override is blank/None."""
        variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="M",
            sku="AP-TSH-M",
            price=None,
            stock_quantity=5,
        )
        self.assertEqual(variant.effective_price, Decimal("499.00"))

    def test_effective_price_with_override(self):
        """Verify variant uses its own price when price override is provided."""
        variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="XXL",
            sku="AP-TSH-XXL",
            price=Decimal("599.00"),
            stock_quantity=5,
        )
        self.assertEqual(variant.effective_price, Decimal("599.00"))

    def test_effective_compare_at_price_fallback(self):
        """Verify variant inherits parent compare_at_price when override is None."""
        variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="M",
            sku="AP-TSH-M",
            compare_at_price=None,
            stock_quantity=5,
        )
        self.assertEqual(variant.effective_compare_at_price, Decimal("799.00"))

    def test_effective_compare_at_price_with_override(self):
        """Verify variant uses override compare_at_price when set."""
        variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="XL",
            sku="AP-TSH-XL",
            price=Decimal("599.00"),
            compare_at_price=Decimal("999.00"),
            stock_quantity=5,
        )
        self.assertEqual(variant.effective_compare_at_price, Decimal("999.00"))

    def test_variant_discount_calculation(self):
        """Verify savings percentage calculation for variants."""
        variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="M",
            sku="AP-TSH-M",
            price=Decimal("499.00"),
            compare_at_price=Decimal("999.00"),
            stock_quantity=5,
        )
        # diff = 500.00 / 999.00 = 50.05% -> 50%
        self.assertEqual(variant.discount_percentage, 50)
        self.assertEqual(variant.discount_percent, 50)

    def test_variant_discount_zero_when_no_compare_at_price(self):
        """Verify discount is 0 when effective compare-at price is absent."""
        prod_no_compare = Product.objects.create(
            name="Plain Item",
            sku="PLN-001",
            category=self.category,
            price=Decimal("200.00"),
            compare_at_price=None,
            stock_quantity=1,
        )
        variant = ProductVariant.objects.create(
            product=prod_no_compare,
            option_type="Pack",
            option_value="Pack of 1",
            sku="PLN-P1",
            price=None,
            compare_at_price=None,
            stock_quantity=1,
        )
        self.assertEqual(variant.discount_percentage, 0)

    def test_sku_normalization_uppercase_and_trim(self):
        """Verify SKU is stripped and converted to uppercase upon saving."""
        variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="XL",
            sku="  ap-tsh-xl  ",
            stock_quantity=5,
        )
        self.assertEqual(variant.sku, "AP-TSH-XL")

    def test_sku_uniqueness_enforced(self):
        """Verify duplicate SKU raises ValidationError or IntegrityError."""
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="S",
            sku="AP-UNIQ-SKU",
            stock_quantity=5,
        )
        dup = ProductVariant(
            product=self.product,
            option_type="Size",
            option_value="M",
            sku="AP-UNIQ-SKU",
            stock_quantity=5,
        )
        with self.assertRaises((ValidationError, IntegrityError)):
            dup.save()

    def test_sku_collision_with_product_prevented(self):
        """Verify variant cannot steal an existing product's SKU."""
        variant = ProductVariant(
            product=self.product,
            option_type="Size",
            option_value="S",
            sku=self.product.sku,  # already used by self.product
            stock_quantity=5,
        )
        with self.assertRaises(ValidationError):
            variant.clean()

    def test_invalid_negative_stock_raises_validation_error(self):
        """Verify negative stock quantity is disallowed."""
        variant = ProductVariant(
            product=self.product,
            option_type="Size",
            option_value="S",
            sku="NEG-STK-01",
            stock_quantity=-5,
        )
        with self.assertRaises(ValidationError):
            variant.clean()

    def test_invalid_negative_price_raises_validation_error(self):
        """Verify negative price override is disallowed."""
        variant = ProductVariant(
            product=self.product,
            option_type="Size",
            option_value="S",
            sku="NEG-PRC-01",
            price=Decimal("-10.00"),
            stock_quantity=5,
        )
        with self.assertRaises(ValidationError):
            variant.clean()

    def test_invalid_negative_compare_at_price_raises_validation_error(self):
        """Verify negative compare-at price override is disallowed."""
        variant = ProductVariant(
            product=self.product,
            option_type="Size",
            option_value="S",
            sku="NEG-CMP-01",
            compare_at_price=Decimal("-50.00"),
            stock_quantity=5,
        )
        with self.assertRaises(ValidationError):
            variant.clean()

    def test_compare_at_price_lower_than_selling_price_raises_error(self):
        """Verify compare-at price cannot be lower than effective selling price."""
        variant = ProductVariant(
            product=self.product,
            option_type="Size",
            option_value="S",
            sku="BAD-CMP-01",
            price=Decimal("500.00"),
            compare_at_price=Decimal("400.00"),
            stock_quantity=5,
        )
        with self.assertRaises(ValidationError):
            variant.clean()

    def test_duplicate_option_combination_for_same_product(self):
        """Verify duplicate (option_type, option_value) on same product is rejected."""
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="Medium",
            sku="TSH-MED-1",
            stock_quantity=5,
        )
        dup = ProductVariant(
            product=self.product,
            option_type="Size",
            option_value="Medium",
            sku="TSH-MED-2",
            stock_quantity=5,
        )
        with self.assertRaises((ValidationError, IntegrityError)):
            dup.clean()

    def test_variant_name_alias(self):
        """Verify variant_name property acts as alias for option_type."""
        v = ProductVariant(
            product=self.product,
            variant_name="Storage",
            option_value="256GB",
            sku="VAR-NAME-ALIAS",
            stock_quantity=3,
        )
        self.assertEqual(v.option_type, "Storage")
        self.assertEqual(v.variant_name, "Storage")
        v.variant_name = "Memory"
        self.assertEqual(v.option_type, "Memory")


class ProductVariantInventoryTest(TestCase):
    """
    Test suite for inventory calculation rules, stock indicators, and out-of-stock behavior.
    """

    def setUp(self):
        self.category = Category.objects.create(name="Shoes", slug="shoes")
        self.product = Product.objects.create(
            name="Running Pro Shoes",
            sku="SH-RUN-001",
            category=self.category,
            price=Decimal("2499.00"),
            stock_quantity=50,  # Parent product stock (should NOT be double-counted!)
        )

    def test_inventory_without_variants_uses_parent_stock(self):
        """When no variants exist, available_inventory returns product stock_quantity."""
        self.assertEqual(self.product.available_inventory, 50)
        self.assertTrue(self.product.is_in_stock)

    def test_inventory_with_variants_ignores_parent_stock(self):
        """When variants exist, available_inventory only sums active variant stock."""
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 8",
            sku="SH-RUN-8",
            stock_quantity=10,
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 9",
            sku="SH-RUN-9",
            stock_quantity=5,
            is_active=True,
        )
        # Variant stock is 10 + 5 = 15. Parent stock 50 must NOT be counted (no 65)!
        self.assertEqual(self.product.available_inventory, 15)
        self.assertTrue(self.product.is_in_stock)

    def test_inactive_variant_stock_excluded_from_available_inventory(self):
        """Verify inactive variants do not contribute to available inventory."""
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 8",
            sku="SH-RUN-8",
            stock_quantity=10,
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 10",
            sku="SH-RUN-10",
            stock_quantity=20,
            is_active=False,  # Inactive!
        )
        self.assertEqual(self.product.available_inventory, 10)

    def test_out_of_stock_when_all_active_variants_zero(self):
        """Product is out of stock when all its active variants have 0 inventory."""
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 8",
            sku="SH-RUN-8",
            stock_quantity=0,
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 9",
            sku="SH-RUN-9",
            stock_quantity=0,
            is_active=True,
        )
        self.assertEqual(self.product.available_inventory, 0)
        self.assertFalse(self.product.is_in_stock)

    def test_in_stock_when_at_least_one_variant_has_stock(self):
        """Product is in stock if any active variant has stock > 0."""
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 8",
            sku="SH-RUN-8",
            stock_quantity=0,
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 9",
            sku="SH-RUN-9",
            stock_quantity=3,
            is_active=True,
        )
        self.assertEqual(self.product.available_inventory, 3)
        self.assertTrue(self.product.is_in_stock)

    def test_starting_price_and_variable_pricing(self):
        """Verify min_price, max_price, and has_variable_pricing calculations."""
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 7",
            sku="SH-RUN-7",
            price=Decimal("1999.00"),
            stock_quantity=4,
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="UK 10",
            sku="SH-RUN-10",
            price=Decimal("2699.00"),
            stock_quantity=2,
            is_active=True,
        )
        self.assertEqual(self.product.min_price, Decimal("1999.00"))
        self.assertEqual(self.product.max_price, Decimal("2699.00"))
        self.assertEqual(self.product.starting_price, Decimal("1999.00"))
        self.assertTrue(self.product.has_variable_pricing)


class ProductVariantAdminTest(TestCase):
    """
    Test suite for Django Admin integration with ProductVariant.
    """

    def setUp(self):
        from django.contrib.admin.sites import site
        self.site = site
        self.category = Category.objects.create(name="Gadgets", slug="gadgets")
        self.product = Product.objects.create(
            name="Smart Ring",
            sku="EL-RNG-001",
            category=self.category,
            price=Decimal("4999.00"),
            stock_quantity=0,
        )
        self.v_in_stock = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="Size 8",
            sku="EL-RNG-8",
            stock_quantity=15,
            is_active=True,
        )
        self.v_low_stock = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="Size 9",
            sku="EL-RNG-9",
            price=Decimal("5499.00"),
            stock_quantity=3,
            is_active=True,
        )
        self.v_out_stock = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="Size 10",
            sku="EL-RNG-10",
            stock_quantity=0,
            is_active=True,
        )
        self.v_inactive = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="Size 11",
            sku="EL-RNG-11",
            stock_quantity=10,
            is_active=False,
        )

    def test_variant_admin_and_inline_registered(self):
        """Verify ProductVariant is registered in admin site and in ProductAdmin.inlines."""
        from core.admin import ProductVariantInline
        self.assertIn(ProductVariant, self.site._registry)
        product_admin = self.site._registry[Product]
        inline_classes = [inline.__class__ if not isinstance(inline, type) else inline for inline in product_admin.inlines]
        self.assertIn(ProductVariantInline, inline_classes)

    def test_variant_admin_stock_status_indicators(self):
        """Verify variant admin shows clear indicators for each stock state."""
        variant_admin = self.site._registry[ProductVariant]
        self.assertIn("In Stock", variant_admin.stock_status_indicator(self.v_in_stock))
        self.assertIn("Low Stock", variant_admin.stock_status_indicator(self.v_low_stock))
        self.assertIn("Out of Stock", variant_admin.stock_status_indicator(self.v_out_stock))
        self.assertIn("Inactive", variant_admin.stock_status_indicator(self.v_inactive))

    def test_variant_admin_effective_price_display(self):
        """Verify variant admin indicates inherited vs override pricing."""
        variant_admin = self.site._registry[ProductVariant]
        self.assertIn("Inherited", variant_admin.effective_price_display(self.v_in_stock))
        self.assertIn("Override", variant_admin.effective_price_display(self.v_low_stock))

    def test_product_admin_stock_status_with_variants(self):
        """Verify ProductAdmin displays total variant inventory summary."""
        product_admin = self.site._registry[Product]
        status_text = product_admin.stock_status(self.product)
        # Total active is 15 + 3 + 0 = 18 in 4 variants
        self.assertIn("18 in 4 variants", status_text)


class ProductVariantDetailViewTest(TestCase):
    """
    Test suite for product detail view and product card template rendering with variants.
    """

    def setUp(self):
        self.client = APIClient()
        self.category = Category.objects.create(name="Beauty", slug="beauty")
        self.product_with_variants = Product.objects.create(
            name="Radiance Face Moisturizer",
            slug="radiance-face-moisturizer",
            sku="BT-RFM-001",
            category=self.category,
            price=Decimal("799.00"),
            compare_at_price=Decimal("999.00"),
            stock_quantity=0,
            is_active=True,
        )
        self.v1 = ProductVariant.objects.create(
            product=self.product_with_variants,
            option_type="Pack",
            option_value="50ml",
            sku="BT-RFM-50",
            price=Decimal("799.00"),
            stock_quantity=20,
            is_active=True,
        )
        self.v2 = ProductVariant.objects.create(
            product=self.product_with_variants,
            option_type="Pack",
            option_value="100ml",
            sku="BT-RFM-100",
            price=Decimal("1299.00"),
            compare_at_price=Decimal("1599.00"),
            stock_quantity=5,
            is_active=True,
        )
        self.v_inactive = ProductVariant.objects.create(
            product=self.product_with_variants,
            option_type="Pack",
            option_value="200ml (Test)",
            sku="BT-RFM-200",
            price=Decimal("1999.00"),
            stock_quantity=5,
            is_active=False,
        )

        self.product_standalone = Product.objects.create(
            name="Artisan Wooden Comb",
            slug="artisan-wooden-comb",
            sku="BT-AWC-001",
            category=self.category,
            price=Decimal("249.00"),
            stock_quantity=12,
            is_active=True,
        )

    def test_product_detail_with_variants_renders_selectors(self):
        """Verify product detail page displays variant selectors and JSON payload."""
        response = self.client.get(reverse("core:product_detail", kwargs={"slug": self.product_with_variants.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        # Option type and values rendered
        self.assertIn("Select Pack:", content)
        self.assertIn("50ml", content)
        self.assertIn("100ml", content)
        self.assertIn("BT-RFM-50", content)
        self.assertIn("fmVariantsData", content)

        # Inactive variant must NOT be visible
        self.assertNotIn("200ml (Test)", content)
        self.assertNotIn("BT-RFM-200", content)

    def test_product_detail_without_variants_keeps_existing_experience(self):
        """Verify standalone product renders without variant selector container."""
        response = self.client.get(reverse("core:product_detail", kwargs={"slug": self.product_standalone.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn("Artisan Wooden Comb", content)
        self.assertIn("BT-AWC-001", content)
        self.assertNotIn("fmVariantsData", content)
        self.assertNotIn("fmVariantSelectors", content)

    def test_product_card_shows_select_options_for_variants(self):
        """Verify product card displays 'Select Options' link when variants exist."""
        response = self.client.get(reverse("core:category_detail", kwargs={"slug": self.category.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn("Select Options", content)


class ProductVariantAPITest(TestCase):
    """
    Test suite for public DRF Product Variant API endpoint.
    """

    def setUp(self):
        self.client = APIClient()
        self.category = Category.objects.create(name="Electronics", slug="electronics")
        self.active_product = Product.objects.create(
            name="Acoustic Wireless Earbuds",
            slug="acoustic-wireless-earbuds",
            sku="EL-EAR-001",
            category=self.category,
            price=Decimal("1999.00"),
            compare_at_price=Decimal("2999.00"),
            stock_quantity=0,
            is_active=True,
        )
        self.v_black = ProductVariant.objects.create(
            product=self.active_product,
            option_type="Color",
            option_value="Matte Black",
            sku="EL-EAR-BLK",
            price=Decimal("1999.00"),
            compare_at_price=Decimal("2999.00"),
            stock_quantity=15,
            is_active=True,
        )
        self.v_white = ProductVariant.objects.create(
            product=self.active_product,
            option_type="Color",
            option_value="Pearl White",
            sku="EL-EAR-WHT",
            price=Decimal("2199.00"),
            compare_at_price=Decimal("2999.00"),
            stock_quantity=0,
            is_active=True,
        )
        self.v_hidden = ProductVariant.objects.create(
            product=self.active_product,
            option_type="Color",
            option_value="Prototype Gold",
            sku="EL-EAR-GLD",
            price=Decimal("2999.00"),
            stock_quantity=5,
            is_active=False,  # Inactive!
        )

        self.inactive_product = Product.objects.create(
            name="Unreleased Prototype",
            slug="unreleased-prototype",
            sku="EL-PRT-001",
            category=self.category,
            price=Decimal("9999.00"),
            is_active=False,
        )

    def test_api_returns_active_variants_only(self):
        """Verify API returns 200 with active variants and excludes inactive ones."""
        url = reverse("core:product_variants_api", kwargs={"slug": self.active_product.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["product_slug"], "acoustic-wireless-earbuds")
        self.assertEqual(data["total_active_variants"], 2)
        skus = [v["sku"] for v in data["variants"]]
        self.assertIn("EL-EAR-BLK", skus)
        self.assertIn("EL-EAR-WHT", skus)
        self.assertNotIn("EL-EAR-GLD", skus)

    def test_api_variant_data_fields(self):
        """Verify API exposes required variant attributes."""
        url = reverse("core:product_variants_api", kwargs={"slug": self.active_product.slug})
        response = self.client.get(url)
        data = response.json()
        black_var = next(v for v in data["variants"] if v["sku"] == "EL-EAR-BLK")

        self.assertEqual(black_var["option_type"], "Color")
        self.assertEqual(black_var["option_value"], "Matte Black")
        self.assertEqual(black_var["effective_price"], "1999.00")
        self.assertEqual(black_var["effective_compare_at_price"], "2999.00")
        self.assertEqual(black_var["discount_percentage"], 33)
        self.assertEqual(black_var["stock_quantity"], 15)
        self.assertTrue(black_var["is_in_stock"])

    def test_api_returns_404_for_inactive_product(self):
        """Verify API returns 404 for inactive products."""
        url = reverse("core:product_variants_api", kwargs={"slug": self.inactive_product.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_api_returns_404_for_nonexistent_product(self):
        """Verify API returns 404 for unknown product slug."""
        url = reverse("core:product_variants_api", kwargs={"slug": "non-existent-slug"})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_api_product_without_variants(self):
        """Verify API returns 200 with empty variants array for product with no variants."""
        prod = Product.objects.create(
            name="Standalone Speaker",
            slug="standalone-speaker",
            sku="EL-SPK-001",
            category=self.category,
            price=Decimal("899.00"),
            is_active=True,
        )
        url = reverse("core:product_variants_api", kwargs={"slug": prod.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["total_active_variants"], 0)
        self.assertFalse(data["has_variants"])
        self.assertEqual(data["variants"], [])


class ShoppingCartServiceTest(TestCase):
    """
    Test suite for the Fitmeezo Shopping Cart service layer (core/cart.py).
    Validates business logic, Decimal calculations, inventory constraints,
    anonymous sessions, authenticated DB carts, and stock alert states.
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="testshopper", password="testpassword123")

        self.category = Category.objects.create(name="Electronics", slug="electronics")

        # Product without variants
        self.product_no_var = Product.objects.create(
            name="Smart Wireless Mouse",
            slug="smart-wireless-mouse",
            sku="EL-MOU-001",
            category=self.category,
            price=Decimal("799.00"),
            compare_at_price=Decimal("1299.00"),
            stock_quantity=10,
            is_active=True,
        )

        # Inactive product
        self.product_inactive = Product.objects.create(
            name="Discontinued Keyboard",
            slug="discontinued-keyboard",
            sku="EL-KEY-OLD",
            category=self.category,
            price=Decimal("499.00"),
            stock_quantity=5,
            is_active=False,
        )

        # Product with variants
        self.product_with_vars = Product.objects.create(
            name="Fitmeezo Athletic T-Shirt",
            slug="athletic-t-shirt",
            sku="FA-TSH-BASE",
            category=self.category,
            price=Decimal("599.00"),
            compare_at_price=Decimal("999.00"),
            stock_quantity=0,
            is_active=True,
        )

        # Variant 1: Size M (price override ₹649.00, stock 5)
        self.variant_m = ProductVariant.objects.create(
            product=self.product_with_vars,
            option_type="Size",
            option_value="M",
            sku="FA-TSH-M",
            price=Decimal("649.00"),
            compare_at_price=Decimal("1099.00"),
            stock_quantity=5,
            is_active=True,
        )

        # Variant 2: Size L (inherits parent price ₹599.00, stock 2)
        self.variant_l = ProductVariant.objects.create(
            product=self.product_with_vars,
            option_type="Size",
            option_value="L",
            sku="FA-TSH-L",
            price=None,
            compare_at_price=None,
            stock_quantity=2,
            is_active=True,
        )

        # Variant 3: Size XL (inactive, stock 3)
        self.variant_xl_inactive = ProductVariant.objects.create(
            product=self.product_with_vars,
            option_type="Size",
            option_value="XL",
            sku="FA-TSH-XL",
            price=Decimal("699.00"),
            stock_quantity=3,
            is_active=False,
        )

    def _get_anonymous_cart(self):
        session = self.client.session
        request = type("MockRequest", (), {"session": session, "user": None})()
        return CartService(request)

    def _get_authenticated_cart(self, user=None):
        u = user or self.user
        session = self.client.session
        request = type("MockRequest", (), {"session": session, "user": u})()
        return CartService(request)

    def test_empty_cart_defaults(self):
        """Verify initial state of an empty shopping cart."""
        cart = self._get_anonymous_cart()
        self.assertTrue(cart.is_empty)
        self.assertEqual(cart.total_units, 0)
        self.assertEqual(cart.subtotal, Decimal("0.00"))
        self.assertEqual(cart.total_discount, Decimal("0.00"))
        self.assertEqual(cart.total_payable, Decimal("0.00"))
        self.assertFalse(cart.has_warnings)
        self.assertFalse(cart.is_checkout_ready)
        self.assertEqual(len(cart.get_lines()), 0)

    def test_add_product_without_variants(self):
        """Verify adding a product with no variants."""
        cart = self._get_anonymous_cart()
        success, msg, key = cart.add(self.product_no_var, variant=None, quantity=2)
        self.assertTrue(success)
        self.assertFalse(cart.is_empty)
        self.assertEqual(cart.total_units, 2)
        self.assertEqual(cart.subtotal, Decimal("1598.00"))
        self.assertEqual(cart.total_discount, Decimal("1000.00"))
        self.assertEqual(cart.total_payable, Decimal("1598.00"))

        lines = cart.get_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line.product.id, self.product_no_var.id)
        self.assertIsNone(line.variant)
        self.assertEqual(line.quantity, 2)
        self.assertEqual(line.unit_price, Decimal("799.00"))
        self.assertEqual(line.line_subtotal, Decimal("1598.00"))
        self.assertTrue(line.is_valid)

    def test_add_product_with_valid_variant(self):
        """Verify adding a product variant with price override."""
        cart = self._get_anonymous_cart()
        success, msg, key = cart.add(self.product_with_vars, variant=self.variant_m, quantity=1)
        self.assertTrue(success)
        self.assertEqual(cart.total_units, 1)
        self.assertEqual(cart.subtotal, Decimal("649.00"))

        lines = cart.get_lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line.sku, "FA-TSH-M")
        self.assertEqual(line.variant.option_value, "M")
        self.assertEqual(line.unit_price, Decimal("649.00"))

    def test_reject_adding_product_with_missing_or_foreign_variant(self):
        """Verify adding a variant-based product without selecting a variant or with foreign variant is rejected."""
        cart = self._get_anonymous_cart()

        # Missing variant on product with variants
        success, msg, key = cart.add(self.product_with_vars, variant=None, quantity=1)
        self.assertFalse(success)
        self.assertIn("Please select an available option", msg)

        # Foreign variant (belonging to another product)
        other_prod = Product.objects.create(
            name="Other Product",
            slug="other-prod",
            sku="OT-001",
            category=self.category,
            price=Decimal("100.00"),
            stock_quantity=5,
            is_active=True,
        )
        foreign_var = ProductVariant.objects.create(
            product=other_prod,
            option_type="Color",
            option_value="Red",
            sku="OT-RED",
            stock_quantity=5,
            is_active=True,
        )
        success, msg, key = cart.add(self.product_with_vars, variant=foreign_var, quantity=1)
        self.assertFalse(success)

    def test_reject_adding_inactive_variant(self):
        """Verify inactive variants cannot be added to cart."""
        cart = self._get_anonymous_cart()
        success, msg, key = cart.add(self.product_with_vars, variant=self.variant_xl_inactive, quantity=1)
        self.assertFalse(success)
        self.assertIn("inactive or unavailable", msg)

    def test_reject_adding_inactive_product(self):
        """Verify inactive products cannot be added to cart."""
        cart = self._get_anonymous_cart()
        success, msg, key = cart.add(self.product_inactive, variant=None, quantity=1)
        self.assertFalse(success)
        self.assertIn("inactive", msg)

    def test_reject_zero_negative_malformed_quantities(self):
        """Verify invalid quantity requests are strictly rejected."""
        cart = self._get_anonymous_cart()
        success, msg, _ = cart.add(self.product_no_var, quantity=0)
        self.assertFalse(success)

        success, msg, _ = cart.add(self.product_no_var, quantity=-3)
        self.assertFalse(success)

        success, msg, _ = cart.add(self.product_no_var, quantity="abc")
        self.assertFalse(success)

    def test_reject_quantity_exceeding_stock(self):
        """Verify adding more units than available stock is rejected."""
        cart = self._get_anonymous_cart()
        # variant_l has stock 2
        success, msg, _ = cart.add(self.product_with_vars, variant=self.variant_l, quantity=3)
        self.assertFalse(success)
        self.assertIn("exceeds available stock", msg)

    def test_duplicate_cart_line_updates_quantity_and_respects_stock(self):
        """Verify adding identical item updates quantity instead of creating duplicate line."""
        cart = self._get_anonymous_cart()
        # Add 1 unit of variant_l (stock 2)
        success1, _, _ = cart.add(self.product_with_vars, variant=self.variant_l, quantity=1)
        self.assertTrue(success1)
        self.assertEqual(cart.total_units, 1)
        self.assertEqual(len(cart.get_lines()), 1)

        # Add 1 more unit of same variant
        success2, _, _ = cart.add(self.product_with_vars, variant=self.variant_l, quantity=1)
        self.assertTrue(success2)
        self.assertEqual(cart.total_units, 2)
        self.assertEqual(len(cart.get_lines()), 1)

        # Try adding 1 more unit (total would be 3, exceeding stock 2)
        success3, msg3, _ = cart.add(self.product_with_vars, variant=self.variant_l, quantity=1)
        self.assertFalse(success3)
        self.assertIn("Cannot add 1 more", msg3)
        self.assertEqual(cart.total_units, 2)

    def test_update_quantity_valid_and_invalid(self):
        """Verify updating quantity on an existing cart line."""
        cart = self._get_anonymous_cart()
        cart.add(self.product_no_var, quantity=3)
        key = str(self.product_no_var.id)

        # Valid update
        success, _ = cart.update_quantity(key, 5)
        self.assertTrue(success)
        self.assertEqual(cart.total_units, 5)

        # Update to 0 rejected
        success, msg = cart.update_quantity(key, 0)
        self.assertFalse(success)

        # Update exceeding stock (stock is 10)
        success, msg = cart.update_quantity(key, 15)
        self.assertFalse(success)
        self.assertIn("available in stock", msg)

    def test_remove_item_and_clear_cart(self):
        """Verify removing single item and clearing the entire cart."""
        cart = self._get_anonymous_cart()
        cart.add(self.product_no_var, quantity=2)
        cart.add(self.product_with_vars, variant=self.variant_m, quantity=1)
        self.assertEqual(len(cart.get_lines()), 2)
        self.assertEqual(cart.total_units, 3)

        # Remove mouse
        key_mouse = str(self.product_no_var.id)
        success, _ = cart.remove(key_mouse)
        self.assertTrue(success)
        self.assertEqual(len(cart.get_lines()), 1)
        self.assertEqual(cart.total_units, 1)

        # Clear cart
        cart.clear()
        self.assertTrue(cart.is_empty)
        self.assertEqual(cart.total_units, 0)

    def test_price_inheritance_and_decimal_subtotals(self):
        """Verify variant with no price override inherits parent price exactly."""
        cart = self._get_anonymous_cart()
        # variant_l price is None, parent product is 599.00
        cart.add(self.product_with_vars, variant=self.variant_l, quantity=2)
        lines = cart.get_lines()
        self.assertEqual(lines[0].unit_price, Decimal("599.00"))
        self.assertEqual(lines[0].line_subtotal, Decimal("1198.00"))
        self.assertEqual(cart.subtotal, Decimal("1198.00"))

    def test_stock_changes_after_item_added_produces_warning(self):
        """Verify warning is flagged if stock decreases below in-cart quantity."""
        cart = self._get_anonymous_cart()
        cart.add(self.product_with_vars, variant=self.variant_l, quantity=2)
        self.assertFalse(cart.has_warnings)

        # Inventory decreases in DB to 1
        self.variant_l.stock_quantity = 1
        self.variant_l.save(update_fields=["stock_quantity"])

        # Re-fetch cart lines
        lines = cart.get_lines()
        self.assertTrue(lines[0].has_stock_warning)
        self.assertTrue(cart.has_warnings)
        self.assertIn("Only 1 unit in stock", lines[0].warning_message)
        self.assertFalse(cart.is_checkout_ready)

    def test_session_cart_to_authenticated_user_cart_merge(self):
        """Verify anonymous session items are merged into DB Cart when user authenticates."""
        # Step 1: Anonymous session adds items
        session = self.client.session
        request_anon = type("MockRequest", (), {"session": session, "user": None})()
        cart_anon = CartService(request_anon)
        cart_anon.add(self.product_no_var, quantity=2)

        # Step 2: User logs in; cart service initialized with user
        request_auth = type("MockRequest", (), {"session": session, "user": self.user})()
        cart_auth = CartService(request_auth)

        # Cart should now be backed by CartModel and CartItemModel
        self.assertEqual(cart_auth.total_units, 2)
        db_items = CartItem.objects.filter(cart__user=self.user)
        self.assertEqual(db_items.count(), 1)
        self.assertEqual(db_items.first().quantity, 2)


class ShoppingCartViewsAndEndpointsTest(TestCase):
    """
    Test suite for Fitmeezo Shopping Cart HTTP views, routes, CSRF enforcement,
    and HTML template rendering.
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="cartuser", password="password123")

        self.category = Category.objects.create(name="Fashion", slug="fashion")

        self.product = Product.objects.create(
            name="Classic Denim Jacket",
            slug="classic-denim-jacket",
            sku="FA-JKT-001",
            category=self.category,
            price=Decimal("1999.00"),
            compare_at_price=Decimal("2999.00"),
            stock_quantity=8,
            is_active=True,
        )

        self.variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="L",
            sku="FA-JKT-L",
            price=Decimal("2199.00"),
            stock_quantity=4,
            is_active=True,
        )

    def test_cart_detail_empty_page(self):
        """Verify /cart/ displays empty-cart state when empty."""
        url = reverse("core:cart_detail")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your Shopping Cart is Empty")
        self.assertContains(response, "Continue Shopping")

    def test_cart_detail_renders_items_and_totals(self):
        """Verify /cart/ renders cart lines, unit prices, and totals."""
        add_url = reverse("core:cart_add")
        self.client.post(add_url, {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 2,
        })

        cart_url = reverse("core:cart_detail")
        response = self.client.get(cart_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Classic Denim Jacket")
        self.assertContains(response, "FA-JKT-L")
        self.assertContains(response, "Size: <strong>L</strong>")
        self.assertContains(response, "₹4398.00")  # 2199 * 2
        self.assertContains(response, "Total Payable")

    def test_cart_mutation_endpoints_reject_get_method(self):
        """Verify GET requests to cart mutation endpoints return 405 Method Not Allowed."""
        endpoints = [
            reverse("core:cart_add"),
            reverse("core:cart_update"),
            reverse("core:cart_remove"),
            reverse("core:cart_clear"),
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 405)

    def test_cart_add_view_standard_post_redirects(self):
        """Verify standard form submission to cart_add redirects to cart_detail."""
        add_url = reverse("core:cart_add")
        response = self.client.post(add_url, {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 1,
        })
        self.assertRedirects(response, reverse("core:cart_detail"))

    def test_cart_add_view_ajax_returns_json(self):
        """Verify AJAX POST to cart_add returns JSON payload."""
        add_url = reverse("core:cart_add")
        response = self.client.post(
            add_url,
            {
                "product_id": self.product.id,
                "variant_id": self.variant.id,
                "quantity": 1,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["cart_total_units"], 1)
        self.assertEqual(data["cart_subtotal"], "2199.00")

    def test_cart_update_view_post(self):
        """Verify cart_update updates quantity."""
        # Add item
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 1,
        })

        item_key = f"{self.product.id}_{self.variant.id}"
        update_url = reverse("core:cart_update")
        response = self.client.post(update_url, {
            "item_key": item_key,
            "quantity": 3,
        })
        self.assertRedirects(response, reverse("core:cart_detail"))

        # Verify in cart
        cart = CartService(type("MockReq", (), {"session": self.client.session, "user": None})())
        self.assertEqual(cart.total_units, 3)

    def test_cart_remove_view_post(self):
        """Verify cart_remove removes line item."""
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 1,
        })

        item_key = f"{self.product.id}_{self.variant.id}"
        remove_url = reverse("core:cart_remove")
        response = self.client.post(remove_url, {"item_key": item_key})
        self.assertRedirects(response, reverse("core:cart_detail"))

        cart = CartService(type("MockReq", (), {"session": self.client.session, "user": None})())
        self.assertTrue(cart.is_empty)

    def test_cart_clear_view_post(self):
        """Verify cart_clear empties entire cart."""
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 1,
        })

        clear_url = reverse("core:cart_clear")
        response = self.client.post(clear_url)
        self.assertRedirects(response, reverse("core:cart_detail"))

        cart = CartService(type("MockReq", (), {"session": self.client.session, "user": None})())
        self.assertTrue(cart.is_empty)

    def test_cart_badge_reflects_actual_units_across_pages(self):
        """Verify cart badge in header displays accurate units on homepage."""
        # Initially badge is 0
        resp = self.client.get(reverse("core:home"))
        self.assertContains(resp, 'id="fmHeaderCartBadge"')
        self.assertContains(resp, '>0</span>')

        # Add 2 items
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 2,
        })

        # Check homepage again
        resp2 = self.client.get(reverse("core:home"))
        self.assertContains(resp2, '>2</span>')

    def test_authenticated_user_cart_persistence(self):
        """Verify cart persists in database for authenticated users."""
        self.client.force_login(self.user)
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 2,
        })

        # Check DB directly
        self.assertTrue(Cart.objects.filter(user=self.user).exists())
        self.assertEqual(CartItem.objects.filter(cart__user=self.user).count(), 1)
        self.assertEqual(CartItem.objects.filter(cart__user=self.user).first().quantity, 2)


# ==============================================================================
# PROMPT 8: CUSTOMER AUTHENTICATION & PROFILE MANAGEMENT TEST SUITE
# ==============================================================================

class CustomerRegistrationTest(TestCase):
    """
    Test suite for customer registration, validation, password rules,
    email normalization, duplicate rejection, and cart migration on sign-up.
    """

    def setUp(self):
        self.client = APIClient()
        self.register_url = reverse("core:account_register")

    def test_registration_page_loads_200(self):
        """Verify registration page renders HTTP 200 with required form fields."""
        response = self.client.get(self.register_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Account")
        self.assertContains(response, 'name="full_name"')
        self.assertContains(response, 'name="email"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'name="confirm_password"')
        self.assertContains(response, 'name="terms"')

    def test_successful_registration(self):
        """Verify valid registration creates user, profile, hashes password, and logs in."""
        User = get_user_model()
        data = {
            "full_name": "Aarav Sharma",
            "email": "Aarav.Sharma@Example.COM",
            "password": "SecurePassword#2026",
            "confirm_password": "SecurePassword#2026",
            "terms": True,
        }
        response = self.client.post(self.register_url, data, follow=True)
        self.assertRedirects(response, reverse("core:account_dashboard"))

        # Verify user created with normalized email and split name
        user = User.objects.get(email="aarav.sharma@example.com")
        self.assertEqual(user.first_name, "Aarav")
        self.assertEqual(user.last_name, "Sharma")
        self.assertTrue(user.check_password("SecurePassword#2026"))
        self.assertNotEqual(user.password, "SecurePassword#2026")  # Never plaintext

        # Verify profile auto-created
        self.assertTrue(CustomerProfile.objects.filter(user=user).exists())

        # Verify logged in
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_duplicate_email_registration_rejected(self):
        """Verify registering with an existing email returns an error without leaking details."""
        User = get_user_model()
        User.objects.create_user(
            username="existing@fitmeezo.com",
            email="existing@fitmeezo.com",
            password="StrongPass1234!",
        )

        data = {
            "full_name": "Duplicate User",
            "email": "EXISTING@Fitmeezo.com",  # case-insensitive duplicate
            "password": "SecurePassword#2026",
            "confirm_password": "SecurePassword#2026",
            "terms": True,
        }
        response = self.client.post(self.register_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "An account with this email address already exists.")

    def test_password_mismatch_rejected(self):
        """Verify registration fails when password and confirm_password do not match."""
        data = {
            "full_name": "Mismatch Tester",
            "email": "mismatch@example.com",
            "password": "SecurePassword#2026",
            "confirm_password": "DifferentPassword#2026",
            "terms": True,
        }
        response = self.client.post(self.register_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Passwords do not match.")

    def test_weak_password_rejected(self):
        """Verify registration rejects passwords failing Django password validators."""
        data = {
            "full_name": "Weak Tester",
            "email": "weak@example.com",
            "password": "123",  # Too short / entirely numeric
            "confirm_password": "123",
            "terms": True,
        }
        response = self.client.post(self.register_url, data)
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertTrue(form.errors.get("password"))

    def test_terms_page_loads_200(self):
        """Verify terms and conditions placeholder page renders successfully."""
        terms_url = reverse("core:terms")
        response = self.client.get(terms_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Terms of Service")


class CustomerAuthenticationAndLogoutTest(TestCase):
    """
    Test suite for customer login (email or username), invalid credentials,
    safe redirect handling, and secure POST-only logout.
    """

    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            username="priya_patel",
            email="priya.patel@example.com",
            password="Password@Secure123",
            first_name="Priya",
            last_name="Patel",
        )
        self.admin = User.objects.create_superuser(
            username="admin_super",
            email="admin@fitmeezo.com",
            password="AdminPassword@123",
        )
        self.login_url = reverse("core:account_login")
        self.logout_url = reverse("core:account_logout")

    def test_login_page_loads_200(self):
        """Verify login page renders HTTP 200 with credentials form."""
        response = self.client.get(self.login_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Customer Sign In")
        self.assertContains(response, 'name="username_or_email"')
        self.assertContains(response, 'name="password"')

    def test_login_success_with_email(self):
        """Verify customer can log in using their email address (case-insensitive)."""
        data = {
            "username_or_email": "PRIYA.PATEL@EXAMPLE.COM",
            "password": "Password@Secure123",
        }
        response = self.client.post(self.login_url, data, follow=True)
        self.assertRedirects(response, reverse("core:account_dashboard"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_login_success_with_username(self):
        """Verify customer can log in using their username."""
        data = {
            "username_or_email": "priya_patel",
            "password": "Password@Secure123",
        }
        response = self.client.post(self.login_url, data, follow=True)
        self.assertRedirects(response, reverse("core:account_dashboard"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_django_admin_login_works(self):
        """Verify superuser can authenticate using username, keeping Django Admin working."""
        data = {
            "username_or_email": "admin_super",
            "password": "AdminPassword@123",
        }
        response = self.client.post(self.login_url, data, follow=True)
        self.assertRedirects(response, reverse("core:account_dashboard"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin.pk))

    def test_login_failure_invalid_credentials(self):
        """Verify invalid credentials render error message and keep user unauthenticated."""
        data = {
            "username_or_email": "priya.patel@example.com",
            "password": "WrongPassword!",
        }
        response = self.client.post(self.login_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid email/username or password")

    def test_safe_redirect_on_login(self):
        """Verify valid internal redirect URLs are honored and unsafe external ones are rejected."""
        # Safe internal redirect
        cart_url = reverse("core:cart_detail")
        login_with_next = f"{self.login_url}?next={cart_url}"
        response = self.client.post(login_with_next, {
            "username_or_email": "priya.patel@example.com",
            "password": "Password@Secure123",
        })
        self.assertRedirects(response, cart_url)

        # Logout
        self.client.logout()

        # Unsafe external redirect should redirect to dashboard
        evil_url = f"{self.login_url}?next=https://malicious-site.com/steal"
        response2 = self.client.post(evil_url, {
            "username_or_email": "priya.patel@example.com",
            "password": "Password@Secure123",
        })
        self.assertRedirects(response2, reverse("core:account_dashboard"))

    def test_logout_post_success(self):
        """Verify POST to logout view logs user out and redirects to home."""
        self.client.force_login(self.user)
        response = self.client.post(self.logout_url)
        self.assertRedirects(response, reverse("core:home"))

        # Verify session user is anonymous
        resp = self.client.get(reverse("core:account_dashboard"))
        self.assertRedirects(resp, f"{self.login_url}?next={reverse('core:account_dashboard')}")

    def test_logout_get_method_rejected(self):
        """Verify GET to logout view returns 405 Method Not Allowed to prevent CSRF logout."""
        self.client.force_login(self.user)
        response = self.client.get(self.logout_url)
        self.assertEqual(response.status_code, 405)


class CustomerProfileManagementTest(TestCase):
    """
    Test suite for customer profile viewing, editing, phone number updates,
    email change safety, and cross-user isolation.
    """

    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user1 = User.objects.create_user(
            username="user_one@example.com",
            email="user_one@example.com",
            password="Password@Secure1",
            first_name="Rohan",
            last_name="Verma",
        )
        self.profile1, _ = CustomerProfile.objects.get_or_create(user=self.user1)

        self.user2 = User.objects.create_user(
            username="user_two@example.com",
            email="user_two@example.com",
            password="Password@Secure2",
            first_name="Anita",
            last_name="Desai",
        )
        self.profile_url = reverse("core:account_profile")
        self.dashboard_url = reverse("core:account_dashboard")

    def test_profile_requires_login(self):
        """Verify unauthenticated access to profile redirects to login."""
        response = self.client.get(self.profile_url)
        self.assertRedirects(response, f"{reverse('core:account_login')}?next={self.profile_url}")

    def test_dashboard_requires_login(self):
        """Verify unauthenticated access to dashboard redirects to login."""
        response = self.client.get(self.dashboard_url)
        self.assertRedirects(response, f"{reverse('core:account_login')}?next={self.dashboard_url}")

    def test_profile_view_displays_customer_info(self):
        """Verify profile page renders current user information."""
        self.client.force_login(self.user1)
        response = self.client.get(self.profile_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rohan")
        self.assertContains(response, "user_one@example.com")

    def test_profile_update_success(self):
        """Verify user can update their name and phone number."""
        self.client.force_login(self.user1)
        data = {
            "full_name": "Rohan Deep Verma",
            "email": "user_one@example.com",
            "phone_number": "+91 9876543210",
        }
        response = self.client.post(self.profile_url, data, follow=True)
        self.assertContains(response, "profile details have been updated successfully")

        self.user1.refresh_from_db()
        self.profile1.refresh_from_db()
        self.assertEqual(self.user1.first_name, "Rohan")
        self.assertEqual(self.user1.last_name, "Deep Verma")
        self.assertEqual(self.profile1.phone_number, "+91 9876543210")

    def test_profile_email_update_safe(self):
        """Verify email can be updated if new email is unique."""
        self.client.force_login(self.user1)
        data = {
            "full_name": "Rohan Verma",
            "email": "rohan.newemail@example.com",
            "phone_number": "",
        }
        response = self.client.post(self.profile_url, data, follow=True)
        self.assertContains(response, "profile details have been updated successfully")

        self.user1.refresh_from_db()
        self.assertEqual(self.user1.email, "rohan.newemail@example.com")

    def test_profile_rejects_duplicate_email(self):
        """Verify user cannot update their email to an address already taken by user2."""
        self.client.force_login(self.user1)
        data = {
            "full_name": "Rohan Verma",
            "email": "user_two@example.com",  # Owned by user2
            "phone_number": "",
        }
        response = self.client.post(self.profile_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This email address is already in use by another account.")

        self.user1.refresh_from_db()
        self.assertEqual(self.user1.email, "user_one@example.com")  # Unchanged


class CustomerAddressManagementTest(TestCase):
    """
    Test suite for customer address CRUD, default address management,
    PIN code validation, and cross-user address IDOR protection.
    """

    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user1 = User.objects.create_user(
            username="addr_user1@example.com",
            email="addr_user1@example.com",
            password="Password@Secure1",
            first_name="Vikram",
        )
        self.user2 = User.objects.create_user(
            username="addr_user2@example.com",
            email="addr_user2@example.com",
            password="Password@Secure2",
            first_name="Kavita",
        )

        self.addr1 = Address.objects.create(
            user=self.user1,
            full_name="Vikram Seth",
            phone_number="9876543210",
            address_line1="123 Park Street",
            city="Kolkata",
            state="West Bengal",
            postal_code="700016",
            address_type="home",
            is_default=True,
        )

    def test_address_list_requires_login(self):
        """Verify unauthenticated user cannot view addresses."""
        response = self.client.get(reverse("core:account_addresses"))
        self.assertRedirects(response, f"{reverse('core:account_login')}?next={reverse('core:account_addresses')}")

    def test_address_creation_auto_default_for_first(self):
        """Verify user's first address is automatically set as default."""
        self.client.force_login(self.user2)
        data = {
            "full_name": "Kavita Roy",
            "phone_number": "9123456789",
            "address_line1": "Flat 4B, Silver Heights",
            "address_line2": "MG Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560001",
            "address_type": "home",
            "is_default": False,  # Even if marked false, first address auto-defaults
        }
        response = self.client.post(reverse("core:account_address_add"), data, follow=True)
        self.assertRedirects(response, reverse("core:account_addresses"))

        addr = Address.objects.filter(user=self.user2).first()
        self.assertIsNotNone(addr)
        self.assertTrue(addr.is_default)

    def test_default_address_uniqueness_enforced(self):
        """Verify marking a new address as default unsets previous default address."""
        self.client.force_login(self.user1)
        data = {
            "full_name": "Vikram Seth Work",
            "phone_number": "9876543210",
            "address_line1": "Tech Park Tower 2",
            "city": "Kolkata",
            "state": "West Bengal",
            "postal_code": "700091",
            "address_type": "work",
            "is_default": True,
        }
        self.client.post(reverse("core:account_address_add"), data)

        self.addr1.refresh_from_db()
        new_addr = Address.objects.get(postal_code="700091")
        self.assertTrue(new_addr.is_default)
        self.assertFalse(self.addr1.is_default)  # Old address no longer default

        # Strictly 1 default address
        default_count = Address.objects.filter(user=self.user1, is_default=True).count()
        self.assertEqual(default_count, 1)

    def test_address_edit_success(self):
        """Verify user can edit their own address."""
        self.client.force_login(self.user1)
        edit_url = reverse("core:account_address_edit", kwargs={"pk": self.addr1.id})
        data = {
            "full_name": "Vikram Seth",
            "phone_number": "9876543210",
            "address_line1": "456 Updated Lane",
            "city": "Kolkata",
            "state": "West Bengal",
            "postal_code": "700016",
            "address_type": "home",
            "is_default": True,
        }
        response = self.client.post(edit_url, data, follow=True)
        self.assertRedirects(response, reverse("core:account_addresses"))

        self.addr1.refresh_from_db()
        self.assertEqual(self.addr1.address_line1, "456 Updated Lane")

    def test_address_delete_promotes_remaining_address_to_default(self):
        """Verify deleting a default address safely promotes remaining address to default."""
        self.client.force_login(self.user1)
        addr2 = Address.objects.create(
            user=self.user1,
            full_name="Vikram Alternate",
            phone_number="9876543210",
            address_line1="789 Salt Lake",
            city="Kolkata",
            state="West Bengal",
            postal_code="700064",
            address_type="other",
            is_default=False,
        )

        # Delete default addr1
        delete_url = reverse("core:account_address_delete", kwargs={"pk": self.addr1.id})
        self.client.post(delete_url)

        self.assertFalse(Address.objects.filter(id=self.addr1.id).exists())
        addr2.refresh_from_db()
        self.assertTrue(addr2.is_default)  # Promoted to default

    def test_cross_user_address_access_rejected_idor(self):
        """Verify user2 cannot view, edit, delete, or set default on user1's address."""
        self.client.force_login(self.user2)

        # Try to edit user1's address
        edit_url = reverse("core:account_address_edit", kwargs={"pk": self.addr1.id})
        resp_edit = self.client.get(edit_url)
        self.assertEqual(resp_edit.status_code, 404)

        # Try to delete user1's address
        del_url = reverse("core:account_address_delete", kwargs={"pk": self.addr1.id})
        resp_del = self.client.post(del_url)
        self.assertEqual(resp_del.status_code, 404)
        self.assertTrue(Address.objects.filter(id=self.addr1.id).exists())

        # Try to set user1's address as default
        def_url = reverse("core:account_address_set_default", kwargs={"pk": self.addr1.id})
        resp_def = self.client.post(def_url)
        self.assertEqual(resp_def.status_code, 404)

    def test_postal_code_validation(self):
        """Verify Indian PIN code formats and general alphanumeric postal codes are validated."""
        self.client.force_login(self.user1)
        # Invalid: too short
        data_invalid = {
            "full_name": "Test Name",
            "phone_number": "9876543210",
            "address_line1": "Street 1",
            "city": "Mumbai",
            "state": "Maharashtra",
            "postal_code": "12",  # Too short
            "address_type": "home",
        }
        resp = self.client.post(reverse("core:account_address_add"), data_invalid)
        self.assertEqual(resp.status_code, 200)
        form = resp.context["form"]
        self.assertTrue(form.errors.get("postal_code"))


class CartAuthenticationMergeIntegrationTest(TestCase):
    """
    Test suite for merging guest session cart into user's persistent cart upon login/registration,
    revalidating stock, preventing duplicate lines, and logout cart isolation.
    """

    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            username="merge_user@example.com",
            email="merge_user@example.com",
            password="Password@Secure123",
        )

        self.category = Category.objects.create(name="Fitness", slug="fitness")
        self.product = Product.objects.create(
            name="Resistance Band Set",
            slug="resistance-band-set",
            sku="FIT-RB-001",
            category=self.category,
            price=Decimal("799.00"),
            stock_quantity=10,
            is_active=True,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Resistance",
            option_value="Heavy",
            sku="FIT-RB-HVY",
            price=Decimal("899.00"),
            stock_quantity=5,
            is_active=True,
        )

    def test_anonymous_cart_merges_on_login(self):
        """Verify anonymous cart items are transferred to user's database cart after login."""
        # 1. Add item as anonymous visitor
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 2,
        })

        # Verify session has the cart
        session = self.client.session
        self.assertIn("fitmeezo_cart", session)
        self.assertEqual(len(session["fitmeezo_cart"]), 1)

        # 2. Log in
        self.client.post(reverse("core:account_login"), {
            "username_or_email": "merge_user@example.com",
            "password": "Password@Secure123",
        })

        # 3. Verify user's database cart now contains the 2 units
        db_cart = Cart.objects.get(user=self.user)
        self.assertEqual(db_cart.items.count(), 1)
        item = db_cart.items.first()
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.variant, self.variant)

    def test_cart_merge_duplicate_line_sums_quantity_and_caps_at_stock(self):
        """Verify when item exists in both session cart and user DB cart, quantities are merged safely."""
        # Pre-populate user's DB cart with 3 units of variant (stock is 5)
        user_cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(
            cart=user_cart,
            product=self.product,
            variant=self.variant,
            quantity=3,
        )

        # Anonymous visitor adds 4 units of same item (stock is 5, so total 7 exceeds 5)
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 4,
        })

        # Log in
        self.client.post(reverse("core:account_login"), {
            "username_or_email": "merge_user@example.com",
            "password": "Password@Secure123",
        })

        # Cart merge should not exceed available stock of 5
        user_cart.refresh_from_db()
        self.assertEqual(user_cart.items.count(), 1)
        item = user_cart.items.first()
        self.assertLessEqual(item.quantity, self.variant.stock_quantity)
        self.assertEqual(item.quantity, 5)

    def test_logout_does_not_leak_cart_to_guest(self):
        """Verify logging out clears cart from session so next guest does not inherit it."""
        # Log in and add item
        self.client.force_login(self.user)
        self.client.post(reverse("core:cart_add"), {
            "product_id": self.product.id,
            "variant_id": self.variant.id,
            "quantity": 2,
        })

        # Log out
        self.client.post(reverse("core:account_logout"))

        # Verify guest cart is empty
        resp = self.client.get(reverse("core:cart_detail"))
        self.assertContains(resp, "Your Shopping Cart is Empty")


class CustomerPasswordResetTest(TestCase):
    """
    Test suite for password reset request, security token generation,
    handling invalid/expired tokens, and successful password reset.
    """

    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            username="reset_user@example.com",
            email="reset_user@example.com",
            password="OldPassword#2026",
            first_name="Karan",
        )
        self.reset_request_url = reverse("core:password_reset")

    def test_password_reset_page_loads_200(self):
        """Verify password reset request form renders HTTP 200."""
        response = self.client.get(self.reset_request_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Forgot Your Password?")
        self.assertContains(response, 'name="email"')

    def test_password_reset_generic_response_for_nonexistent_email(self):
        """Verify request returns success screen without revealing if email exists."""
        response = self.client.post(self.reset_request_url, {
            "email": "nonexistent_email_12345@domain.com",
        }, follow=True)
        self.assertRedirects(response, reverse("core:password_reset_done"))
        self.assertContains(response, "Password Reset Instructions Sent")

    def test_password_reset_sends_email_with_token(self):
        """Verify valid email generates reset link and sends email via configured backend."""
        mail.outbox.clear()
        response = self.client.post(self.reset_request_url, {
            "email": "reset_user@example.com",
        }, follow=True)
        self.assertRedirects(response, reverse("core:password_reset_done"))

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Fitmeezo", email.subject)
        self.assertIn("password-reset/confirm", email.body)

    def test_password_reset_invalid_token(self):
        """Verify invalid or tampered token shows invalid link screen."""
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        invalid_token = "invalid-token-123"
        confirm_url = reverse("core:password_reset_confirm", kwargs={"uidb64": uidb64, "token": invalid_token})

        response = self.client.get(confirm_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Password Reset Link Expired")

    def test_password_reset_success_with_valid_token(self):
        """Verify valid token enables setting new password and authenticating with it."""
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        confirm_url = reverse("core:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token})

        # GET loads form (following Django's referer protection redirect)
        response = self.client.get(confirm_url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Set New Password")

        # POST submits new password to the redirected form url
        post_url = response.redirect_chain[-1][0] if response.redirect_chain else confirm_url
        post_response = self.client.post(post_url, {
            "new_password1": "BrandNewSecretPassword#2026",
            "new_password2": "BrandNewSecretPassword#2026",
        }, follow=True)
        self.assertRedirects(post_response, reverse("core:password_reset_complete"))
        self.assertContains(post_response, "Password Successfully Changed")

        # Verify old password no longer works and new password works
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("OldPassword#2026"))
        self.assertTrue(self.user.check_password("BrandNewSecretPassword#2026"))

        # Customer can now log in with new password
        login_resp = self.client.post(reverse("core:account_login"), {
            "username_or_email": "reset_user@example.com",
            "password": "BrandNewSecretPassword#2026",
        }, follow=True)
        self.assertRedirects(login_resp, reverse("core:account_dashboard"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))


class ProductReviewTests(TestCase):
    """
    Comprehensive test suite for Product Reviews & Ratings (Prompt 9).
    Covers model validation, submission, duplicate prevention, ownership checks,
    editing, deleting, moderation status, aggregations, pagination, and DRF API.
    """

    def setUp(self):
        User = get_user_model()
        self.client = APIClient()
        self.category = Category.objects.create(name="Fitness Tech", slug="fitness-tech")
        self.product = Product.objects.create(
            name="FitPulse Watch Pro",
            slug="fitpulse-watch-pro",
            sku="FW-PRO-001",
            category=self.category,
            price=Decimal("2999.00"),
            stock_quantity=20,
        )

        self.user1 = User.objects.create_user(
            username="aarav@example.com",
            email="aarav@example.com",
            password="StrongPassword#2026",
            first_name="Aarav",
            last_name="Sharma",
        )
        self.user2 = User.objects.create_user(
            username="priya@example.com",
            email="priya@example.com",
            password="StrongPassword#2026",
            first_name="Priya",
            last_name="Patel",
        )
        self.user3 = User.objects.create_user(
            username="rohit123",
            email="rohit@example.com",
            password="StrongPassword#2026",
            first_name="Rohit",
            last_name="",
        )

    def test_review_creation_and_string_representation(self):
        """Verify ProductReview can be created with valid fields and provides informative string representation."""
        review = ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="Exceptional Smartwatch!",
            comment="Exceeded all my fitness tracking expectations. Battery lasts 7 days.",
            status=ProductReview.STATUS_APPROVED,
        )
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.status, "approved")
        self.assertEqual(review.customer_display_name, "Aarav S.")
        self.assertIn("5★ review by Aarav S.", str(review))

    def test_rating_boundary_validation_between_1_and_5(self):
        """Verify rating is strictly restricted between 1 and 5 stars."""
        # 0 is invalid
        review_zero = ProductReview(
            product=self.product,
            user=self.user1,
            rating=0,
            title="Zero rating",
            comment="Invalid",
        )
        with self.assertRaises(ValidationError):
            review_zero.full_clean()

        # 6 is invalid
        review_six = ProductReview(
            product=self.product,
            user=self.user1,
            rating=6,
            title="Six rating",
            comment="Invalid",
        )
        with self.assertRaises(ValidationError):
            review_six.full_clean()

        # 1 and 5 are valid
        review_one = ProductReview(
            product=self.product,
            user=self.user1,
            rating=1,
            title="One rating",
            comment="Valid",
        )
        review_one.full_clean()  # Should not raise

        review_five = ProductReview(
            product=self.product,
            user=self.user2,
            rating=5,
            title="Five rating",
            comment="Valid",
        )
        review_five.full_clean()  # Should not raise

    def test_customer_display_name_privacy(self):
        """Verify customer name is privacy-conscious and does not leak private full names or emails."""
        # First + Last -> "Aarav S."
        r1 = ProductReview(product=self.product, user=self.user1, rating=5, title="T", comment="C")
        self.assertEqual(r1.customer_display_name, "Aarav S.")

        # First only -> "Rohit"
        r2 = ProductReview(product=self.product, user=self.user3, rating=4, title="T", comment="C")
        self.assertEqual(r2.customer_display_name, "Rohit")

        # User with no first/last name and email username -> masked
        User = get_user_model()
        user_anon = User.objects.create_user(
            username="secretbuyer@test.com",
            email="secretbuyer@test.com",
            password="StrongPassword#2026",
        )
        r3 = ProductReview(product=self.product, user=user_anon, rating=5, title="T", comment="C")
        self.assertNotIn("@", r3.customer_display_name)
        self.assertNotIn("secretbuyer@test.com", r3.customer_display_name)

    def test_unique_review_per_customer_per_product_database_constraint(self):
        """Verify unique constraint prevents duplicate reviews for the same product by the same user."""
        ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="First Review",
            comment="Great!",
        )
        with self.assertRaises(IntegrityError):
            ProductReview.objects.create(
                product=self.product,
                user=self.user1,
                rating=4,
                title="Duplicate Attempt",
                comment="Another review",
            )

    def test_logged_in_customer_can_submit_review(self):
        """Verify authenticated customer can submit a review via web form and it is published."""
        self.client.force_login(self.user1)
        url = reverse("core:product_review_submit", kwargs={"slug": self.product.slug})
        response = self.client.post(url, {
            "rating": 5,
            "title": "Superb Quality",
            "comment": "Loved the design and accuracy of heart rate sensor.",
        }, follow=True)

        self.assertRedirects(response, f"{self.product.get_absolute_url()}#reviews")
        self.assertTrue(ProductReview.objects.filter(product=self.product, user=self.user1).exists())
        review = ProductReview.objects.get(product=self.product, user=self.user1)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.status, ProductReview.STATUS_APPROVED)
        self.assertContains(response, "Superb Quality")
        self.assertContains(response, "Aarav S.")

    def test_anonymous_user_cannot_submit_review(self):
        """Verify anonymous user is redirected to login when attempting to submit a review."""
        url = reverse("core:product_review_submit", kwargs={"slug": self.product.slug})
        response = self.client.post(url, {
            "rating": 5,
            "title": "Anon review",
            "comment": "Should not be allowed.",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:account_login"), response.url)
        self.assertEqual(ProductReview.objects.count(), 0)

    def test_duplicate_review_submission_via_web_rejected(self):
        """Verify submitting a second review for the same product gives a friendly error and does not duplicate."""
        ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="Initial Review",
            comment="First review.",
            status=ProductReview.STATUS_APPROVED,
        )
        self.client.force_login(self.user1)
        url = reverse("core:product_review_submit", kwargs={"slug": self.product.slug})
        response = self.client.post(url, {
            "rating": 4,
            "title": "Second Review Attempt",
            "comment": "Duplicate text.",
        }, follow=True)

        self.assertRedirects(response, f"{self.product.get_absolute_url()}#reviews")
        self.assertEqual(ProductReview.objects.filter(product=self.product, user=self.user1).count(), 1)
        self.assertContains(response, "You have already submitted a review for this product")

    def test_customer_can_edit_own_review(self):
        """Verify customer can edit their own review rating, title, and comments."""
        review = ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=4,
            title="Good Product",
            comment="Initial thoughts.",
            status=ProductReview.STATUS_APPROVED,
        )
        self.client.force_login(self.user1)
        edit_url = reverse("core:review_edit", kwargs={"pk": review.pk})

        # GET loads edit form
        get_resp = self.client.get(edit_url)
        self.assertEqual(get_resp.status_code, 200)
        self.assertContains(get_resp, "Good Product")

        # POST updates review
        post_resp = self.client.post(edit_url, {
            "rating": 5,
            "title": "Updated to Excellent!",
            "comment": "After 2 weeks of use, it is definitely 5 stars.",
        }, follow=True)

        self.assertRedirects(post_resp, f"{self.product.get_absolute_url()}#reviews")
        review.refresh_from_db()
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.title, "Updated to Excellent!")

    def test_cross_user_cannot_edit_another_customers_review(self):
        """Verify User B receives 403 Forbidden when trying to edit User A's review."""
        review = ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="User 1 Review",
            comment="Private content.",
        )
        self.client.force_login(self.user2)
        edit_url = reverse("core:review_edit", kwargs={"pk": review.pk})

        get_resp = self.client.get(edit_url)
        self.assertEqual(get_resp.status_code, 403)

        post_resp = self.client.post(edit_url, {
            "rating": 1,
            "title": "Hacked review",
            "comment": "Malicious edit",
        })
        self.assertEqual(post_resp.status_code, 403)
        review.refresh_from_db()
        self.assertEqual(review.title, "User 1 Review")

    def test_customer_can_delete_own_review(self):
        """Verify customer can delete their own review."""
        review = ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="To be deleted",
            comment="Temporary text.",
        )
        self.client.force_login(self.user1)
        delete_url = reverse("core:review_delete", kwargs={"pk": review.pk})

        # GET renders confirmation screen
        get_resp = self.client.get(delete_url)
        self.assertEqual(get_resp.status_code, 200)
        self.assertContains(get_resp, "Delete Your Review?")

        # POST performs deletion
        post_resp = self.client.post(delete_url, follow=True)
        self.assertRedirects(post_resp, f"{self.product.get_absolute_url()}#reviews")
        self.assertFalse(ProductReview.objects.filter(pk=review.pk).exists())

    def test_cross_user_cannot_delete_another_customers_review(self):
        """Verify User B receives 403 Forbidden when trying to delete User A's review."""
        review = ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="Keep this review",
            comment="Important.",
        )
        self.client.force_login(self.user2)
        delete_url = reverse("core:review_delete", kwargs={"pk": review.pk})

        response = self.client.post(delete_url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ProductReview.objects.filter(pk=review.pk).exists())

    def test_moderation_visibility_public_vs_private(self):
        """Verify approved reviews are visible publicly, while pending/rejected reviews are excluded."""
        # Approved review
        ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="Approved Public Review",
            comment="Visible to everyone.",
            status=ProductReview.STATUS_APPROVED,
        )
        # Pending review
        ProductReview.objects.create(
            product=self.product,
            user=self.user2,
            rating=4,
            title="Pending Secret Review",
            comment="Under moderation review.",
            status=ProductReview.STATUS_PENDING,
        )
        # Rejected review
        ProductReview.objects.create(
            product=self.product,
            user=self.user3,
            rating=1,
            title="Rejected Spam Review",
            comment="Spam link http://spam.com",
            status=ProductReview.STATUS_REJECTED,
        )

        # Anonymous public request
        response = self.client.get(self.product.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approved Public Review")
        self.assertNotContains(response, "Pending Secret Review")
        self.assertNotContains(response, "Rejected Spam Review")

    def test_average_rating_and_review_counts_aggregation(self):
        """Verify average rating and count calculate solely from approved reviews."""
        self.assertEqual(self.product.average_rating, 0.0)
        self.assertEqual(self.product.review_count, 0)

        ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="R1",
            comment="C1",
            status=ProductReview.STATUS_APPROVED,
        )
        ProductReview.objects.create(
            product=self.product,
            user=self.user2,
            rating=4,
            title="R2",
            comment="C2",
            status=ProductReview.STATUS_APPROVED,
        )
        # Rejected review should not affect average or count
        ProductReview.objects.create(
            product=self.product,
            user=self.user3,
            rating=1,
            title="R3",
            comment="C3",
            status=ProductReview.STATUS_REJECTED,
        )

        self.assertEqual(self.product.review_count, 2)
        # (5 + 4) / 2 = 4.5
        self.assertEqual(self.product.average_rating, 4.5)

        summary = self.product.get_rating_breakdown()
        self.assertEqual(summary["average_rating"], 4.5)
        self.assertEqual(summary["review_count"], 2)
        self.assertEqual(summary["stars"][5]["count"], 1)
        self.assertEqual(summary["stars"][4]["count"], 1)
        self.assertEqual(summary["stars"][3]["count"], 0)

    def test_product_detail_zero_reviews_renders_clean_empty_state(self):
        """Verify product page with 0 reviews renders clean message without errors."""
        response = self.client.get(self.product.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No customer reviews yet")

    def test_reviews_pagination_on_product_page(self):
        """Verify pagination divides reviews into pages of 5 items."""
        User = get_user_model()
        for i in range(7):
            u = User.objects.create_user(
                username=f"tester_{i}@test.com",
                email=f"tester_{i}@test.com",
                password="StrongPassword#2026",
                first_name=f"User{i}",
            )
            ProductReview.objects.create(
                product=self.product,
                user=u,
                rating=5,
                title=f"Review Number {i}",
                comment=f"Feedback text for number {i}",
                status=ProductReview.STATUS_APPROVED,
            )

        # Page 1
        resp_p1 = self.client.get(f"{self.product.get_absolute_url()}?review_page=1")
        self.assertEqual(resp_p1.status_code, 200)
        self.assertEqual(len(resp_p1.context["reviews_page"]), 5)

        # Page 2
        resp_p2 = self.client.get(f"{self.product.get_absolute_url()}?review_page=2")
        self.assertEqual(resp_p2.status_code, 200)
        self.assertEqual(len(resp_p2.context["reviews_page"]), 2)

    def test_account_reviews_page(self):
        """Verify customer can view their submitted reviews in account dashboard."""
        ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="My Smartwatch Review",
            comment="Awesome watch.",
            status=ProductReview.STATUS_APPROVED,
        )
        self.client.force_login(self.user1)
        response = self.client.get(reverse("core:account_reviews"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Smartwatch Review")
        self.assertContains(response, "FitPulse Watch Pro")

    def test_product_reviews_api_public_get(self):
        """Verify DRF API public GET returns approved reviews, average rating, and safe display names."""
        ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="API Approved Review",
            comment="Works seamlessly.",
            status=ProductReview.STATUS_APPROVED,
        )
        ProductReview.objects.create(
            product=self.product,
            user=self.user2,
            rating=2,
            title="API Rejected Review",
            comment="Rejected.",
            status=ProductReview.STATUS_REJECTED,
        )

        api_url = reverse("core:product_reviews_api", kwargs={"slug": self.product.slug})
        response = self.client.get(api_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["product_slug"], self.product.slug)
        self.assertEqual(data["review_count"], 1)
        self.assertEqual(data["average_rating"], 5.0)
        self.assertEqual(len(data["reviews"]), 1)

        review_item = data["reviews"][0]
        self.assertEqual(review_item["title"], "API Approved Review")
        self.assertEqual(review_item["customer_name"], "Aarav S.")
        # Ensure private emails or user IDs are not leaked
        self.assertNotIn("email", review_item)
        self.assertNotIn("user", review_item)

    def test_product_reviews_api_unauthenticated_post_rejected(self):
        """Verify anonymous POST to reviews API returns 401 Unauthorized."""
        api_url = reverse("core:product_reviews_api", kwargs={"slug": self.product.slug})
        response = self.client.post(api_url, {
            "rating": 5,
            "title": "API Review",
            "comment": "Testing auth",
        })
        self.assertEqual(response.status_code, 401)

    def test_product_reviews_api_authenticated_post_and_duplicate_prevention(self):
        """Verify authenticated customer can post review via API and duplicates are rejected."""
        self.client.force_login(self.user1)
        api_url = reverse("core:product_reviews_api", kwargs={"slug": self.product.slug})

        # 1. First review succeeds
        post_resp = self.client.post(api_url, {
            "rating": 5,
            "title": "API Submitted Review",
            "comment": "Submitted via DRF endpoint.",
        })
        self.assertEqual(post_resp.status_code, 201)
        self.assertEqual(post_resp.json()["title"], "API Submitted Review")

        # 2. Duplicate review fails
        dup_resp = self.client.post(api_url, {
            "rating": 4,
            "title": "Duplicate API Review",
            "comment": "Duplicate attempt.",
        })
        self.assertEqual(dup_resp.status_code, 400)
        self.assertIn("detail", dup_resp.json())

    def test_admin_review_moderation_actions(self):
        """Verify Django Admin actions approve_reviews and reject_reviews update status correctly."""
        from django.contrib.admin.sites import AdminSite
        from core.admin import ProductReviewAdmin

        review = ProductReview.objects.create(
            product=self.product,
            user=self.user1,
            rating=5,
            title="Needs Moderation",
            comment="Review text",
            status=ProductReview.STATUS_PENDING,
        )

        admin = ProductReviewAdmin(ProductReview, AdminSite())
        qs = ProductReview.objects.filter(pk=review.pk)

        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage

        factory = RequestFactory()
        req = factory.get('/')
        req.session = {}
        req._messages = FallbackStorage(req)

        admin.approve_reviews(req, qs)
        review.refresh_from_db()
        self.assertEqual(review.status, ProductReview.STATUS_APPROVED)

        # Reject action
        admin.reject_reviews(req, qs)
        review.refresh_from_db()
        self.assertEqual(review.status, ProductReview.STATUS_REJECTED)


# ==============================================================================
# PROMPT 10: WISHLIST SYSTEM TEST SUITE
# ==============================================================================

class WishlistModelAndUniqueConstraintTest(TestCase):
    """
    Tests WishlistItem model constraints, uniqueness, indexing, and deletion behavior.
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="wl_user", email="wl@example.com", password="PassWord123!")
        self.category = Category.objects.create(name="Electronics", slug="electronics")
        self.product = Product.objects.create(
            name="Wireless Headphones",
            slug="wireless-headphones",
            sku="WL-HP-001",
            category=self.category,
            price=Decimal("2999.00"),
            stock_quantity=10,
            is_active=True,
        )

    def test_create_wishlist_item(self):
        """Verify WishlistItem creation and string representation."""
        item = WishlistItem.objects.create(user=self.user, product=self.product)
        self.assertIsNotNone(item.created_at)
        self.assertEqual(str(item), f"{self.user.username} - {self.product.name}")

    def test_database_level_uniqueness_enforced(self):
        """Verify database UniqueConstraint prevents saving the same product more than once for a customer."""
        WishlistItem.objects.create(user=self.user, product=self.product)
        with self.assertRaises(IntegrityError):
            WishlistItem.objects.create(user=self.user, product=self.product)

    def test_cascade_delete_on_user_removal(self):
        """Verify WishlistItem is deleted when customer account is deleted."""
        WishlistItem.objects.create(user=self.user, product=self.product)
        self.user.delete()
        self.assertEqual(WishlistItem.objects.count(), 0)

    def test_cascade_delete_on_product_removal(self):
        """Verify WishlistItem is deleted if product is deleted."""
        WishlistItem.objects.create(user=self.user, product=self.product)
        self.product.delete()
        self.assertEqual(WishlistItem.objects.count(), 0)


class WishlistStorefrontAndViewsTest(TestCase):
    """
    Tests wishlist views: access control, anonymous redirection, adding,
    removing, toggling, moving to cart, out-of-stock validation, and badges.
    """

    def setUp(self):
        User = get_user_model()
        self.user1 = User.objects.create_user(username="customer1", email="c1@example.com", password="PassWord123!")
        self.user2 = User.objects.create_user(username="customer2", email="c2@example.com", password="PassWord123!")
        self.category = Category.objects.create(name="Fashion", slug="fashion")

        self.product_active = Product.objects.create(
            name="Fitmeezo Classic Hoodie",
            slug="fitmeezo-classic-hoodie",
            sku="FA-HOD-001",
            category=self.category,
            price=Decimal("1499.00"),
            compare_at_price=Decimal("2499.00"),
            stock_quantity=15,
            is_active=True,
        )
        self.product_out_of_stock = Product.objects.create(
            name="Sold Out Shoes",
            slug="sold-out-shoes",
            sku="FA-SHO-001",
            category=self.category,
            price=Decimal("3499.00"),
            stock_quantity=0,
            is_active=True,
        )
        self.product_inactive = Product.objects.create(
            name="Archived Watch",
            slug="archived-watch",
            sku="FA-WAT-001",
            category=self.category,
            price=Decimal("4999.00"),
            stock_quantity=5,
            is_active=False,
        )
        self.product_with_variants = Product.objects.create(
            name="Premium Denim Jacket",
            slug="premium-denim-jacket",
            sku="FA-DEN-001",
            category=self.category,
            price=Decimal("2999.00"),
            stock_quantity=0,
            is_active=True,
        )
        self.variant_in_stock = ProductVariant.objects.create(
            product=self.product_with_variants,
            option_type="Size",
            option_value="M",
            sku="FA-DEN-M",
            price=Decimal("3199.00"),
            stock_quantity=5,
            is_active=True,
        )

        self.wishlist_url = reverse("core:wishlist_detail")
        self.toggle_url = reverse("core:wishlist_toggle")
        self.add_url = reverse("core:wishlist_add")

    def test_anonymous_access_to_wishlist_page_redirects_to_login(self):
        """Verify unauthenticated visitor cannot access wishlist page and is safely redirected to login."""
        response = self.client.get(self.wishlist_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:account_login"), response.url)
        self.assertIn(quote(self.wishlist_url), response.url)

    def test_anonymous_wishlist_mutation_redirects_with_safe_next(self):
        """Verify anonymous POST to wishlist_toggle redirects to login with safe next URL."""
        response = self.client.post(self.toggle_url, {
            "product_id": self.product_active.id,
            "next": self.product_active.get_absolute_url(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:account_login"), response.url)
        self.assertIn(quote(self.product_active.get_absolute_url()), response.url)

    def test_anonymous_wishlist_mutation_ajax_returns_401_json(self):
        """Verify anonymous AJAX POST to wishlist_toggle returns 401 with login URL."""
        response = self.client.post(
            self.toggle_url,
            {"product_id": self.product_active.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 401)
        data = response.json()
        self.assertTrue(data["login_required"])
        self.assertIn(reverse("core:account_login"), data["login_url"])

    def test_authenticated_customer_can_view_empty_wishlist(self):
        """Verify authenticated customer sees empty wishlist state."""
        self.client.force_login(self.user1)
        response = self.client.get(self.wishlist_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your Wishlist is Empty")
        self.assertContains(response, "Continue Shopping")

    def test_authenticated_customer_add_and_remove_wishlist_item(self):
        """Verify customer can add and remove items from wishlist."""
        self.client.force_login(self.user1)

        # 1. Add item
        add_resp = self.client.post(self.add_url, {"product_id": self.product_active.id}, follow=True)
        self.assertRedirects(add_resp, self.wishlist_url)
        self.assertTrue(WishlistItem.objects.filter(user=self.user1, product=self.product_active).exists())
        self.assertContains(add_resp, "Fitmeezo Classic Hoodie")
        self.assertContains(add_resp, "₹1499.00")

        # 2. Remove item
        item = WishlistItem.objects.get(user=self.user1, product=self.product_active)
        remove_url = reverse("core:wishlist_remove", kwargs={"pk": item.pk})
        rem_resp = self.client.post(remove_url, follow=True)
        self.assertRedirects(rem_resp, self.wishlist_url)
        self.assertFalse(WishlistItem.objects.filter(user=self.user1, product=self.product_active).exists())
        self.assertContains(rem_resp, "Your Wishlist is Empty")

    def test_wishlist_toggle_ajax(self):
        """Verify wishlist_toggle toggles state dynamically via AJAX."""
        self.client.force_login(self.user1)

        # First toggle: Adds item
        resp1 = self.client.post(
            self.toggle_url,
            {"product_id": self.product_active.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertTrue(data1["success"])
        self.assertTrue(data1["is_in_wishlist"])
        self.assertEqual(data1["wishlist_count"], 1)

        # Second toggle: Removes item
        resp2 = self.client.post(
            self.toggle_url,
            {"product_id": self.product_active.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2["success"])
        self.assertFalse(data2["is_in_wishlist"])
        self.assertEqual(data2["wishlist_count"], 0)

    def test_duplicate_prevention_on_add(self):
        """Verify calling add twice does not create duplicate entries."""
        self.client.force_login(self.user1)
        self.client.post(self.add_url, {"product_id": self.product_active.id})
        self.client.post(self.add_url, {"product_id": self.product_active.id})
        self.assertEqual(WishlistItem.objects.filter(user=self.user1, product=self.product_active).count(), 1)

    def test_customer_isolation_and_ownership(self):
        """Verify User 1 cannot view, access, or delete User 2's wishlist items."""
        item_user2 = WishlistItem.objects.create(user=self.user2, product=self.product_active)

        # User 1 views their wishlist
        self.client.force_login(self.user1)
        resp = self.client.get(self.wishlist_url)
        self.assertNotContains(resp, "Fitmeezo Classic Hoodie")

        # User 1 attempts to delete User 2's item -> returns 404
        bad_delete_url = reverse("core:wishlist_remove", kwargs={"pk": item_user2.pk})
        del_resp = self.client.post(bad_delete_url)
        self.assertEqual(del_resp.status_code, 404)
        self.assertTrue(WishlistItem.objects.filter(pk=item_user2.pk).exists())

    def test_reject_adding_inactive_or_nonexistent_product(self):
        """Verify inactive or nonexistent products cannot be added to wishlist."""
        self.client.force_login(self.user1)

        # Inactive product
        self.client.post(self.add_url, {"product_id": self.product_inactive.id})
        self.assertFalse(WishlistItem.objects.filter(user=self.user1, product=self.product_inactive).exists())

        # Nonexistent product
        self.client.post(self.add_url, {"product_id": 999999})
        self.assertFalse(WishlistItem.objects.filter(user=self.user1, product_id=999999).exists())

    def test_move_wishlist_item_to_cart(self):
        """Verify moving a wishlist product to cart reuses the Cart service."""
        self.client.force_login(self.user1)
        item = WishlistItem.objects.create(user=self.user1, product=self.product_active)

        move_url = reverse("core:wishlist_move_to_cart", kwargs={"pk": item.pk})
        response = self.client.post(move_url, follow=True)
        self.assertRedirects(response, self.wishlist_url)
        self.assertContains(response, "to your cart")

        # Cart should now contain this item
        cart = CartService(type("MockReq", (), {"session": self.client.session, "user": self.user1})())
        self.assertEqual(cart.total_units, 1)

    def test_move_wishlist_item_with_variants_to_cart(self):
        """Verify moving a product with variants to cart picks active in-stock variant."""
        self.client.force_login(self.user1)
        item = WishlistItem.objects.create(user=self.user1, product=self.product_with_variants)

        move_url = reverse("core:wishlist_move_to_cart", kwargs={"pk": item.pk})
        response = self.client.post(move_url, {"variant_id": self.variant_in_stock.id}, follow=True)
        self.assertRedirects(response, self.wishlist_url)

        cart = CartService(type("MockReq", (), {"session": self.client.session, "user": self.user1})())
        self.assertEqual(cart.total_units, 1)
        lines = cart.get_lines()
        self.assertEqual(lines[0].sku, "FA-DEN-M")

    def test_cannot_add_out_of_stock_wishlist_item_to_cart(self):
        """Verify out-of-stock items cannot be added to cart from the wishlist."""
        self.client.force_login(self.user1)
        item = WishlistItem.objects.create(user=self.user1, product=self.product_out_of_stock)

        move_url = reverse("core:wishlist_move_to_cart", kwargs={"pk": item.pk})
        response = self.client.post(move_url, follow=True)
        self.assertRedirects(response, self.wishlist_url)
        self.assertContains(response, "out of stock")

        cart = CartService(type("MockReq", (), {"session": self.client.session, "user": self.user1})())
        self.assertEqual(cart.total_units, 0)

    def test_header_wishlist_count_context_processor(self):
        """Verify wishlist_context context processor reflects authenticated user's count and doesn't leak."""
        # Unauthenticated customer: wishlist count is 0
        resp_anon = self.client.get(reverse("core:home"))
        self.assertEqual(resp_anon.context["wishlist_count"], 0)

        # Authenticated customer with 2 items
        WishlistItem.objects.create(user=self.user1, product=self.product_active)
        WishlistItem.objects.create(user=self.user1, product=self.product_out_of_stock)

        self.client.force_login(self.user1)
        resp_auth = self.client.get(reverse("core:home"))
        self.assertEqual(resp_auth.context["wishlist_count"], 2)
        self.assertIn(self.product_active.id, resp_auth.context["user_wishlist_product_ids"])

        # Another user has 0 items
        self.client.force_login(self.user2)
        resp_user2 = self.client.get(reverse("core:home"))
        self.assertEqual(resp_user2.context["wishlist_count"], 0)


class WishlistAPITest(TestCase):
    """
    Test suite for DRF Wishlist API endpoints (/api/wishlist/).
    """

    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(username="api_user", email="api@example.com", password="PassWord123!")
        self.user2 = User.objects.create_user(username="api_user2", email="api2@example.com", password="PassWord123!")

        self.category = Category.objects.create(name="Gadgets", slug="gadgets")
        self.product = Product.objects.create(
            name="Smart Fitness Tracker",
            slug="smart-fitness-tracker",
            sku="GD-TRK-001",
            category=self.category,
            price=Decimal("2499.00"),
            stock_quantity=10,
            is_active=True,
        )
        self.api_url = reverse("core:wishlist_api")

    def test_anonymous_api_access_returns_401(self):
        """Verify anonymous requests to /api/wishlist/ receive 401 Unauthorized."""
        response = self.client.get(self.api_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        post_resp = self.client.post(self.api_url, {"product_id": self.product.id})
        self.assertEqual(post_resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_api_list_and_add(self):
        """Verify authenticated customer can list and add wishlist items via DRF API."""
        self.client.force_login(self.user)

        # Empty initially
        get_resp = self.client.get(self.api_url)
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(get_resp.json()["count"], 0)

        # Add item
        post_resp = self.client.post(self.api_url, {"product_id": self.product.id})
        self.assertEqual(post_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(post_resp.json()["product_name"], "Smart Fitness Tracker")

        # List again
        get_resp2 = self.client.get(self.api_url)
        self.assertEqual(get_resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(get_resp2.json()["count"], 1)

    def test_api_delete_wishlist_item(self):
        """Verify authenticated customer can delete an item from their wishlist."""
        item = WishlistItem.objects.create(user=self.user, product=self.product)
        detail_url = reverse("core:wishlist_api_detail", kwargs={"pk": item.pk})

        # User 2 cannot delete User 1's item
        self.client.force_login(self.user2)
        del_resp_unauthorized = self.client.delete(detail_url)
        self.assertEqual(del_resp_unauthorized.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(WishlistItem.objects.filter(pk=item.pk).exists())

        # Owner deletes item
        self.client.force_login(self.user)
        del_resp = self.client.delete(detail_url)
        self.assertEqual(del_resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(WishlistItem.objects.filter(pk=item.pk).exists())


class OAuthSocialLoginTest(TestCase):
    """
    Test suite for Google & Facebook OAuth authentication, account linking protections,
    adapter logic, UI buttons, credential fallbacks, and security policies (Prompt 11).
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="existing_customer",
            email="existing@fitmeezo.com",
            first_name="Existing",
            password="StrongPassword123!",
        )
        self.profile = CustomerProfile.objects.get(user=self.user)

    def test_oauth_buttons_rendered_on_login_and_register_pages(self):
        """Verify Google & Facebook buttons and setup badges are properly rendered on login and register pages."""
        login_resp = self.client.get(reverse("core:account_login"))
        self.assertEqual(login_resp.status_code, 200)
        self.assertContains(login_resp, "Continue with Google")
        self.assertContains(login_resp, "Continue with Facebook")
        self.assertContains(login_resp, "fmGoogleLoginBtn")
        self.assertContains(login_resp, "fmFacebookLoginBtn")

        register_resp = self.client.get(reverse("core:account_register"))
        self.assertEqual(register_resp.status_code, 200)
        self.assertContains(register_resp, "fmGoogleRegisterBtn")
        self.assertContains(register_resp, "fmFacebookRegisterBtn")

    def test_oauth_context_processor_flags(self):
        """Verify oauth_context processor injects boolean flags for template rendering."""
        from django.test import RequestFactory
        from core.context_processors import oauth_context

        rf = RequestFactory()
        req = rf.get("/")
        ctx = oauth_context(req)
        self.assertIn("google_oauth_enabled", ctx)
        self.assertIn("facebook_oauth_enabled", ctx)

    def test_missing_oauth_credentials_graceful_redirect(self):
        """When OAuth client IDs are empty, custom views display notice and safely redirect."""
        with self.settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET=""):
            resp_g = self.client.get(reverse("core:oauth_google"), follow=True)
            self.assertEqual(resp_g.status_code, 200)
            self.assertRedirects(resp_g, reverse("core:account_login"))
            messages_list = list(resp_g.context["messages"])
            self.assertTrue(any("Google Sign-In is not configured yet" in m.message for m in messages_list))

        with self.settings(FACEBOOK_CLIENT_ID="", FACEBOOK_CLIENT_SECRET=""):
            resp_fb = self.client.get(reverse("core:oauth_facebook"), follow=True)
            self.assertEqual(resp_fb.status_code, 200)
            self.assertRedirects(resp_fb, reverse("core:account_login"))
            messages_list = list(resp_fb.context["messages"])
            self.assertTrue(any("Facebook Login is not configured yet" in m.message for m in messages_list))

    def test_oauth_redirect_to_allauth_provider_when_configured(self):
        """When credentials exist, custom views route customer to allauth provider initiation URL."""
        with self.settings(GOOGLE_CLIENT_ID="dummy-client-id", GOOGLE_CLIENT_SECRET="dummy-secret"):
            resp_g = self.client.get(reverse("core:oauth_google"))
            self.assertEqual(resp_g.status_code, 302)
            self.assertEqual(resp_g["Location"], reverse("google_login"))

        with self.settings(FACEBOOK_CLIENT_ID="dummy-fb-id", FACEBOOK_CLIENT_SECRET="dummy-secret"):
            resp_fb = self.client.get(reverse("core:oauth_facebook"))
            self.assertEqual(resp_fb.status_code, 302)
            self.assertEqual(resp_fb["Location"], reverse("facebook_login"))

    def test_google_login_flow_redirects_to_accounts_google_com(self):
        """When credentials are configured, the full flow redirects to Google's OAuth endpoint."""
        dummy_app = {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "secret": "test-client-secret",
            "key": "",
        }
        with self.settings(
            GOOGLE_CLIENT_ID="test-client-id.apps.googleusercontent.com",
            GOOGLE_CLIENT_SECRET="test-client-secret",
            SOCIALACCOUNT_PROVIDERS={"google": {"APP": dummy_app, "SCOPE": ["profile", "email"]}},
        ):
            resp_init = self.client.get(reverse("core:oauth_google"))
            self.assertEqual(resp_init.status_code, 302)
            self.assertEqual(resp_init["Location"], reverse("google_login"))

            resp_google = self.client.get(reverse("google_login"))
            self.assertEqual(resp_google.status_code, 302)
            self.assertIn("accounts.google.com/o/oauth2/v2/auth", resp_google["Location"])
            self.assertIn("test-client-id.apps.googleusercontent.com", resp_google["Location"])

    def test_oauth_safe_next_url_handling(self):
        """Verify safe next parameters are preserved in session and external URLs are rejected."""
        with self.settings(GOOGLE_CLIENT_ID="dummy-client-id", GOOGLE_CLIENT_SECRET="dummy-secret"):
            # Safe local redirect
            self.client.get(reverse("core:oauth_google") + "?next=/cart/")
            session = self.client.session
            self.assertEqual(session.get("socialaccount_next"), "/cart/")

            # Malicious external redirect must not be saved
            self.client.get(reverse("core:oauth_google") + "?next=https://malicious-phishing.com/steal")
            session2 = self.client.session
            self.assertNotEqual(session2.get("socialaccount_next"), "https://malicious-phishing.com/steal")

    def test_social_account_adapter_google_verified_email_auto_linking(self):
        """Verified Google emails safely link to existing account."""
        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from allauth.socialaccount.models import SocialAccount, SocialLogin
        from core.adapters import FitmeezoSocialAccountAdapter

        rf = RequestFactory()
        req = rf.get("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req._messages = FallbackStorage(req)
        req.user = AnonymousUser()

        sa = SocialAccount(
            provider="google",
            uid="google-uid-12345",
            extra_data={"email": self.user.email, "email_verified": True},
        )
        sl = SocialLogin(account=sa)
        adapter = FitmeezoSocialAccountAdapter()
        adapter.pre_social_login(req, sl)

        self.assertTrue(sl.is_existing)
        self.assertEqual(sl.user, self.user)

    def test_social_account_adapter_google_unverified_email_rejected(self):
        """Unverified Google email with existing account raises ImmediateHttpResponse."""
        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from allauth.socialaccount.models import SocialAccount, SocialLogin
        from allauth.core.exceptions import ImmediateHttpResponse
        from core.adapters import FitmeezoSocialAccountAdapter

        rf = RequestFactory()
        req = rf.get("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req._messages = FallbackStorage(req)
        req.user = AnonymousUser()

        sa = SocialAccount(
            provider="google",
            uid="google-uid-unverified",
            extra_data={"email": self.user.email, "email_verified": False},
        )
        sl = SocialLogin(account=sa)
        adapter = FitmeezoSocialAccountAdapter()

        with self.assertRaises(ImmediateHttpResponse):
            adapter.pre_social_login(req, sl)

    def test_social_account_adapter_facebook_existing_email_requires_password_login(self):
        """Facebook login matching existing user requires password sign-in first (no automatic link)."""
        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from allauth.socialaccount.models import SocialAccount, SocialLogin
        from allauth.core.exceptions import ImmediateHttpResponse
        from core.adapters import FitmeezoSocialAccountAdapter

        rf = RequestFactory()
        req = rf.get("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req._messages = FallbackStorage(req)
        req.user = AnonymousUser()

        sa = SocialAccount(
            provider="facebook",
            uid="fb-uid-12345",
            extra_data={"email": self.user.email, "name": "Existing User"},
        )
        sl = SocialLogin(account=sa)
        adapter = FitmeezoSocialAccountAdapter()

        with self.assertRaises(ImmediateHttpResponse):
            adapter.pre_social_login(req, sl)

    def test_social_account_adapter_new_user_population(self):
        """New customer registering with social login has username and name normalized."""
        from django.test import RequestFactory
        from allauth.socialaccount.models import SocialAccount, SocialLogin
        from core.adapters import FitmeezoSocialAccountAdapter

        rf = RequestFactory()
        req = rf.get("/")
        sa = SocialAccount(
            provider="google",
            uid="google-new-999",
            extra_data={"email": "newcustomer@fitmeezo.com", "name": "Priya Sharma"},
        )
        User = get_user_model()
        sl = SocialLogin(account=sa, user=User())
        adapter = FitmeezoSocialAccountAdapter()
        user = adapter.populate_user(req, sl, {"name": "Priya Sharma", "email": "newcustomer@fitmeezo.com"})

        self.assertEqual(user.first_name, "Priya")
        self.assertEqual(user.last_name, "Sharma")
        self.assertEqual(user.email, "newcustomer@fitmeezo.com")
        self.assertTrue(user.username.startswith("newcustomer"))

    def test_social_account_adapter_safe_redirect(self):
        """get_login_redirect_url validates target host against open redirect."""
        from django.test import RequestFactory
        from core.adapters import FitmeezoSocialAccountAdapter

        rf = RequestFactory()
        adapter = FitmeezoSocialAccountAdapter()

        # Safe local path
        req_local = rf.get("/?next=/cart/")
        self.assertEqual(adapter.get_login_redirect_url(req_local), "/cart/")

        # Malicious external URL fallback to dashboard
        req_evil = rf.get("/?next=https://attacker-site.com/")
        self.assertEqual(adapter.get_login_redirect_url(req_evil), reverse("core:account_dashboard"))

    def test_social_account_adapter_auth_error_handling(self):
        """Authentication error returns graceful redirect to login without crashing."""
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from allauth.core.exceptions import ImmediateHttpResponse
        from core.adapters import FitmeezoSocialAccountAdapter

        rf = RequestFactory()
        req = rf.get("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req._messages = FallbackStorage(req)
        adapter = FitmeezoSocialAccountAdapter()
        with self.assertRaises(ImmediateHttpResponse) as cm:
            adapter.on_authentication_error(req, "google")
        self.assertEqual(cm.exception.response.status_code, 302)
        self.assertEqual(cm.exception.response["Location"], reverse("core:account_login"))

    def test_allauth_user_logged_in_signal_creates_profile_and_merges_cart(self):
        """allauth user_logged_in signal ensures CustomerProfile and merges session cart."""
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from allauth.account.signals import user_logged_in
        from core.models import CustomerProfile

        User = get_user_model()
        new_user = User.objects.create_user(username="signal_new_user", email="signal@test.com")
        CustomerProfile.objects.filter(user=new_user).delete()
        self.assertFalse(CustomerProfile.objects.filter(user=new_user).exists())

        rf = RequestFactory()
        req = rf.get("/")
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        req._messages = FallbackStorage(req)

        user_logged_in.send(sender=new_user.__class__, request=req, user=new_user)
        self.assertTrue(CustomerProfile.objects.filter(user=new_user).exists())

    def test_connected_social_accounts_in_profile_view(self):
        """Profile view renders connected social account status."""
        from allauth.socialaccount.models import SocialAccount

        SocialAccount.objects.create(
            user=self.user,
            provider="google",
            uid="google-conn-123",
            extra_data={"email": "existing@fitmeezo.com"},
        )
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:account_profile"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Connected Social Accounts")
        self.assertContains(resp, "Connected as existing@fitmeezo.com")

    def test_existing_password_login_still_works(self):
        """Existing password login remains functional and backward-compatible."""
        resp = self.client.post(
            reverse("core:account_login"),
            {
                "username_or_email": "existing_customer",
                "password": "StrongPassword123!",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["user"].is_authenticated)

    def test_existing_registration_still_works(self):
        """Standard customer registration with password still works."""
        resp = self.client.post(
            reverse("core:account_register"),
            {
                "full_name": "Rohan Mehra",
                "email": "rohan.mehra@example.com",
                "password": "ValidPassword987!",
                "confirm_password": "ValidPassword987!",
                "terms": True,
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["user"].is_authenticated)
        self.assertEqual(resp.context["user"].email, "rohan.mehra@example.com")

    def test_anonymous_cannot_access_private_dashboard_and_profile(self):
        """Anonymous requests to dashboard and profile redirect to login page."""
        resp_dash = self.client.get(reverse("core:account_dashboard"))
        self.assertEqual(resp_dash.status_code, 302)
        self.assertIn(reverse("core:account_login"), resp_dash["Location"])

        resp_prof = self.client.get(reverse("core:account_profile"))
        self.assertEqual(resp_prof.status_code, 302)
        self.assertIn(reverse("core:account_login"), resp_prof["Location"])


class ProductSearchFeatureTests(TestCase):
    """
    Comprehensive test suite for Fitmeezo backend-powered Product Search feature.
    Verifies:
    - Route resolution and header search form attributes/action
    - Keyword matching by title, short description, description, SKU
    - Variant SKU and variant option value matching
    - Multi-word and case-insensitive query support
    - Category filtering (both with and without search term)
    - Safe handling of empty, whitespace, and unusually long queries
    - Safe ORM parameterized queries (SQL injection resilient)
    - Friendly empty-results states (no matches and empty query prompt)
    - Preservation of search term and category in header form inputs
    - Catalog business rules (inactive items and inactive categories excluded)
    - Sorting by price, newest, rating, and featured
    - Pagination and query string preservation
    """

    def setUp(self):
        self.cat_electronics = Category.objects.create(
            name="Electronics",
            slug="electronics",
            is_active=True,
            display_order=1,
        )
        self.cat_fashion = Category.objects.create(
            name="Fashion",
            slug="fashion",
            is_active=True,
            display_order=2,
        )
        self.cat_inactive = Category.objects.create(
            name="Inactive Dept",
            slug="inactive-dept",
            is_active=False,
            display_order=3,
        )

        # Active Product 1
        self.prod_smartwatch = Product.objects.create(
            name="FitPulse Pro Max Smartwatch",
            slug="fitpulse-pro-max-smartwatch",
            sku="FP-SMW-001",
            category=self.cat_electronics,
            price=Decimal("2499.00"),
            compare_at_price=Decimal("4999.00"),
            short_description="AMOLED Fitness Tracker",
            description="Waterproof heart rate monitor with GPS navigation",
            stock_quantity=15,
            is_active=True,
            is_featured=True,
        )

        # Active Product 2
        self.prod_headphones = Product.objects.create(
            name="SonicBass Wireless Headphones",
            slug="sonicbass-wireless-headphones",
            sku="SB-HDP-002",
            category=self.cat_electronics,
            price=Decimal("1999.00"),
            compare_at_price=Decimal("3999.00"),
            short_description="Active Noise Cancelling",
            description="Bluetooth over-ear headphones with deep bass",
            stock_quantity=8,
            is_active=True,
            is_featured=False,
        )

        # Active Product 3 (Out of stock)
        self.prod_shoes = Product.objects.create(
            name="AeroGrip Performance Running Shoes",
            slug="aerogrip-performance-running-shoes",
            sku="AG-SHO-003",
            category=self.cat_fashion,
            price=Decimal("1499.00"),
            compare_at_price=Decimal("2499.00"),
            short_description="Lightweight breathable footwear",
            description="Athletic mesh sports shoes for marathon training",
            stock_quantity=0,
            is_active=True,
            is_featured=False,
        )

        # Active Product 4 (With active variants)
        self.prod_tshirt = Product.objects.create(
            name="PureCotton Casual T-Shirt",
            slug="purecotton-casual-t-shirt",
            sku="PC-TSH-004",
            category=self.cat_fashion,
            price=Decimal("799.00"),
            short_description="100% Organic breathable cotton",
            description="Comfort fit everyday casual wear",
            stock_quantity=0,
            is_active=True,
            is_featured=False,
        )
        self.var_xl = ProductVariant.objects.create(
            product=self.prod_tshirt,
            option_type="Size",
            option_value="Extra Large",
            sku="PC-TSH-XL",
            stock_quantity=5,
            price=Decimal("849.00"),
            is_active=True,
        )
        self.var_color = ProductVariant.objects.create(
            product=self.prod_tshirt,
            option_type="Color",
            option_value="Midnight Black",
            sku="PC-TSH-BLK",
            stock_quantity=3,
            price=Decimal("899.00"),
            is_active=True,
        )

        # Inactive product (Must NEVER appear in search)
        self.prod_inactive = Product.objects.create(
            name="Hidden Ghost Device",
            slug="hidden-ghost-device",
            sku="HID-GHO-005",
            category=self.cat_electronics,
            price=Decimal("999.00"),
            stock_quantity=10,
            is_active=False,
        )

        # Product in inactive department (Must NEVER appear in search)
        self.prod_discontinued = Product.objects.create(
            name="Discontinued Retro Player",
            slug="discontinued-retro-player",
            sku="DIS-RET-006",
            category=self.cat_inactive,
            price=Decimal("499.00"),
            stock_quantity=5,
            is_active=True,
        )

    def test_header_search_form_renders_and_submits_to_search(self):
        """Verify the header search form submits GET requests to /search/."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn('id="fmSearchForm"', content)
        self.assertIn('action="/search/"', content)
        self.assertIn('name="q"', content)
        self.assertIn('name="category"', content)

    def test_search_url_resolves_and_renders_search_page(self):
        """Verify /search/ endpoint resolves and responds with 200 OK."""
        response = self.client.get(reverse("core:product_search"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "products/search_results.html")
        # Alias reverse 'core:search' also works
        self.assertEqual(reverse("core:search"), "/search/")

    def test_search_by_product_name(self):
        """Search query matches product name correctly."""
        response = self.client.get("/search/?q=Smartwatch")
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].id, self.prod_smartwatch.id)
        self.assertContains(response, "FitPulse Pro Max Smartwatch")

    def test_search_by_short_description(self):
        """Search query matches short_description field."""
        response = self.client.get("/search/?q=AMOLED")
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].id, self.prod_smartwatch.id)

    def test_search_by_full_description(self):
        """Search query matches full product description."""
        response = self.client.get("/search/?q=Bluetooth")
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].id, self.prod_headphones.id)

    def test_search_by_sku(self):
        """Search query matches exact and partial SKU identifiers."""
        response = self.client.get("/search/?q=SB-HDP-002")
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].id, self.prod_headphones.id)

    def test_search_by_variant_sku(self):
        """Search query matches product variant SKU."""
        response = self.client.get("/search/?q=PC-TSH-XL")
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].id, self.prod_tshirt.id)

    def test_search_by_variant_option_value(self):
        """Search query matches variant option value (e.g. 'Midnight Black')."""
        response = self.client.get("/search/?q=Midnight Black")
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].id, self.prod_tshirt.id)

    def test_search_multi_word_and_case_insensitive(self):
        """Multi-word search terms and varied letter casing match correctly."""
        response_lower = self.client.get("/search/?q=wireless headphones")
        self.assertEqual(response_lower.status_code, 200)
        self.assertEqual(len(response_lower.context["products"]), 1)
        self.assertEqual(response_lower.context["products"][0].id, self.prod_headphones.id)

        response_upper = self.client.get("/search/?q=SMARTWATCH")
        self.assertEqual(response_upper.status_code, 200)
        self.assertEqual(len(response_upper.context["products"]), 1)
        self.assertEqual(response_upper.context["products"][0].id, self.prod_smartwatch.id)

    def test_search_no_results_displays_friendly_empty_state(self):
        """Non-matching search returns 0 products and shows helpful message with suggestions."""
        response = self.client.get("/search/?q=CompletelyNonExistentGadget999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["products"]), 0)
        self.assertEqual(response.context["total_products_count"], 0)
        self.assertContains(response, "No Matching Products Found")
        self.assertContains(response, "Search Tips:")
        self.assertContains(response, "Browse All Categories")

    def test_empty_query_safely_handled(self):
        """Empty query or whitespace returns 200 OK and shows friendly prompt without errors."""
        response_empty = self.client.get("/search/?q=")
        self.assertEqual(response_empty.status_code, 200)
        self.assertEqual(len(response_empty.context["products"]), 0)
        self.assertTrue(response_empty.context["is_empty_query"])
        self.assertContains(response_empty, "Looking for Something Specific?")

        response_spaces = self.client.get("/search/?q=    ")
        self.assertEqual(response_spaces.status_code, 200)
        self.assertEqual(len(response_spaces.context["products"]), 0)
        self.assertTrue(response_spaces.context["is_empty_query"])

        response_no_q = self.client.get("/search/")
        self.assertEqual(response_no_q.status_code, 200)
        self.assertTrue(response_no_q.context["is_empty_query"])

    def test_unusually_long_query_safely_handled(self):
        """Excessively long query string is safely clamped and does not cause server errors."""
        long_query = "headphones " * 250
        response = self.client.get("/search/", {"q": long_query})
        self.assertEqual(response.status_code, 200)
        # Search query should be clamped to at most 100 characters
        self.assertLessEqual(len(response.context["search_query"]), 100)

    def test_special_characters_and_sql_injection_safety(self):
        """Potentially dangerous SQL injection strings and special characters are handled safely."""
        dangerous_query = "' OR '1'='1' -- ; DROP TABLE core_product; <script>alert(1)</script>"
        response = self.client.get("/search/", {"q": dangerous_query})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Product.objects.exists())
        self.assertEqual(len(response.context["products"]), 0)

    def test_category_filtering_with_search_query(self):
        """Category filter restricts search results to specified category."""
        # 'Running' in Fashion category finds shoes
        resp_fashion = self.client.get("/search/?q=Running&category=fashion")
        self.assertEqual(resp_fashion.status_code, 200)
        self.assertEqual(len(resp_fashion.context["products"]), 1)
        self.assertEqual(resp_fashion.context["products"][0].id, self.prod_shoes.id)

        # 'Running' in Electronics category finds nothing
        resp_electronics = self.client.get("/search/?q=Running&category=electronics")
        self.assertEqual(resp_electronics.status_code, 200)
        self.assertEqual(len(resp_electronics.context["products"]), 0)

        # 'Running' with category=all finds shoes
        resp_all = self.client.get("/search/?q=Running&category=all")
        self.assertEqual(resp_all.status_code, 200)
        self.assertEqual(len(resp_all.context["products"]), 1)

    def test_category_filtering_without_search_query(self):
        """Browsing category via search view returns all active products in that department."""
        response = self.client.get("/search/?category=electronics")
        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"])
        self.assertEqual(len(products), 2)
        prod_ids = {p.id for p in products}
        self.assertIn(self.prod_smartwatch.id, prod_ids)
        self.assertIn(self.prod_headphones.id, prod_ids)

    def test_inactive_products_and_categories_excluded(self):
        """Inactive products and products in inactive categories are never returned."""
        resp_inactive_prod = self.client.get("/search/?q=Ghost")
        self.assertEqual(len(resp_inactive_prod.context["products"]), 0)

        resp_inactive_cat = self.client.get("/search/?q=Retro")
        self.assertEqual(len(resp_inactive_cat.context["products"]), 0)

    def test_search_terms_and_category_preserved_in_input(self):
        """Search query and selected category are retained in header form fields."""
        response = self.client.get("/search/?q=Smartwatch&category=electronics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["search_query"], "Smartwatch")
        self.assertEqual(response.context["selected_category"], "electronics")
        content = response.content.decode("utf-8")
        self.assertIn('value="Smartwatch"', content)
        self.assertIn('value="electronics" selected', content)

    def test_sorting_options(self):
        """Sort parameters order products by price ascending, descending, or newest."""
        # Price ascending
        resp_asc = self.client.get("/search/?category=electronics&sort=price-asc")
        prods_asc = list(resp_asc.context["products"])
        self.assertEqual(prods_asc[0].id, self.prod_headphones.id)  # 1999 < 2499
        self.assertEqual(prods_asc[1].id, self.prod_smartwatch.id)

        # Price descending
        resp_desc = self.client.get("/search/?category=electronics&sort=price-desc")
        prods_desc = list(resp_desc.context["products"])
        self.assertEqual(prods_desc[0].id, self.prod_smartwatch.id)  # 2499 > 1999
        self.assertEqual(prods_desc[1].id, self.prod_headphones.id)

    def test_pagination_and_query_string_preservation(self):
        """Paginates results at 12 items and preserves search parameters in navigation links."""
        # Create 13 additional products to exceed 12 items per page
        for i in range(13):
            Product.objects.create(
                name=f"FitPulse Additional Unit {i}",
                slug=f"fitpulse-additional-unit-{i}",
                sku=f"FP-ADD-{i:03d}",
                category=self.cat_electronics,
                price=Decimal("1200.00"),
                stock_quantity=5,
                is_active=True,
            )

        resp_p1 = self.client.get("/search/?q=FitPulse&page=1")
        self.assertEqual(resp_p1.status_code, 200)
        self.assertEqual(len(resp_p1.context["products"]), 12)
        self.assertTrue(resp_p1.context["page_obj"].has_next())
        content_p1 = response_content = resp_p1.content.decode("utf-8")
        self.assertIn("page=2", content_p1)
        self.assertIn("q=FitPulse", content_p1)

        resp_p2 = self.client.get("/search/?q=FitPulse&page=2")
        self.assertEqual(resp_p2.status_code, 200)
        self.assertEqual(len(resp_p2.context["products"]), 2)  # 1 original + 13 new = 14 total; 14 - 12 = 2
        self.assertFalse(resp_p2.context["page_obj"].has_next())


class CheckoutAndOrderManagementTest(TestCase):
    """
    Comprehensive test suite for Fitmeezo Checkout & Order Management System.
    Validates checkout access, server-side totals, stock validation,
    database locking, COD payment status, address snapshots, order ownership,
    duplicate submission idempotency, and admin management.
    """

    def setUp(self):
        self.client = APIClient()
        self.User = get_user_model()

        # Customer 1
        self.user1 = self.User.objects.create_user(
            username="customer1@fitmeezo.in",
            email="customer1@fitmeezo.in",
            password="StrongPassword123!",
            first_name="Aarav",
            last_name="Patel",
        )

        # Customer 2
        self.user2 = self.User.objects.create_user(
            username="customer2@fitmeezo.in",
            email="customer2@fitmeezo.in",
            password="StrongPassword123!",
            first_name="Riya",
            last_name="Sharma",
        )

        # Saved Address for Customer 1
        self.address1 = Address.objects.create(
            user=self.user1,
            full_name="Aarav Patel",
            phone_number="9876543210",
            address_line1="Flat 402, Sunshine Heights",
            address_line2="MG Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            landmark="Near Metro Station",
            address_type="home",
            is_default=True,
        )

        # Saved Address for Customer 2
        self.address2 = Address.objects.create(
            user=self.user2,
            full_name="Riya Sharma",
            phone_number="9876543211",
            address_line1="B-12, Sector 15",
            city="Noida",
            state="Uttar Pradesh",
            postal_code="201301",
            address_type="home",
            is_default=True,
        )

        # Category
        self.category = Category.objects.create(
            name="Electronics",
            slug="electronics",
        )

        # Standalone Product (₹1,999.00, stock=20)
        self.product_standalone = Product.objects.create(
            name="FitPulse Active Smartwatch",
            slug="fitpulse-active-smartwatch",
            sku="FP-ACT-001",
            category=self.category,
            price=Decimal("1999.00"),
            compare_at_price=Decimal("3499.00"),
            stock_quantity=20,
            is_active=True,
        )

        # Product with Variants
        self.product_with_variants = Product.objects.create(
            name="FitPulse Pro Earbuds",
            slug="fitpulse-pro-earbuds",
            sku="FP-EAR-001",
            category=self.category,
            price=Decimal("999.00"),
            stock_quantity=0,
            is_active=True,
        )
        self.variant_black = ProductVariant.objects.create(
            product=self.product_with_variants,
            option_type="Color",
            option_value="Midnight Black",
            sku="FP-EAR-BLK",
            price=Decimal("1099.00"),
            stock_quantity=15,
            is_active=True,
        )
        self.variant_white = ProductVariant.objects.create(
            product=self.product_with_variants,
            option_type="Color",
            option_value="Pearl White",
            sku="FP-EAR-WHT",
            price=Decimal("1099.00"),
            stock_quantity=5,
            is_active=True,
        )

        # Low-priced product under ₹499 for testing shipping charges
        self.product_cheap = Product.objects.create(
            name="FitPulse Sports Band Strap",
            slug="fitpulse-sports-band-strap",
            sku="FP-STR-001",
            category=self.category,
            price=Decimal("299.00"),
            stock_quantity=50,
            is_active=True,
        )

    def _add_to_cart(self, user, product, variant=None, quantity=1):
        """Helper to populate persistent CartItem records for authenticated test users."""
        cart_db, _ = Cart.objects.get_or_create(user=user)
        item, created = CartItem.objects.get_or_create(
            cart=cart_db,
            product=product,
            variant=variant,
            defaults={"quantity": quantity},
        )
        if not created:
            item.quantity += quantity
            item.save(update_fields=["quantity"])
        return item

    def test_checkout_requires_authentication(self):
        """Unauthenticated visitor accessing /checkout/ must be redirected to login with next parameter."""
        checkout_url = reverse("core:checkout")
        response = self.client.get(checkout_url)
        expected_redirect = f"{reverse('core:account_login')}?next={checkout_url}"
        self.assertRedirects(response, expected_redirect)

    def test_checkout_empty_cart_redirects_to_cart_detail(self):
        """Authenticated user with an empty shopping cart is redirected to /cart/ with warning."""
        self.client.force_login(self.user1)
        response = self.client.get(reverse("core:checkout"))
        self.assertRedirects(response, reverse("core:cart_detail"))

    def test_checkout_renders_successfully_with_items(self):
        """Authenticated user with cart items can view the checkout page with addresses and server totals."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=1)

        response = self.client.get(reverse("core:checkout"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Secure Checkout")
        self.assertContains(response, "Flat 402, Sunshine Heights")
        self.assertContains(response, "Cash on Delivery (COD)")
        self.assertContains(response, "idempotency_token")
        # Ensure form has novalidate to prevent silent HTML5 validation aborts on hidden elements
        self.assertContains(response, 'id="fmCheckoutForm"')
        self.assertContains(response, "novalidate")
        # Ensure address_mode is initialized to 'existing' when user has saved addresses
        self.assertContains(response, 'value="existing"')

    def test_checkout_form_rendering_without_saved_addresses(self):
        """When user has no saved addresses, address_mode initializes to 'new'."""
        self.client.force_login(self.user2)
        Address.objects.filter(user=self.user2).delete()
        self._add_to_cart(self.user2, self.product_standalone, quantity=1)

        response = self.client.get(reverse("core:checkout"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="new"')
        self.assertContains(response, "novalidate")

    def test_successful_cod_order_placement_standalone_product(self):
        """
        Verify placing an order via Cash on Delivery (COD):
        - Validates server calculation of totals
        - Saves full address snapshots
        - Decrements inventory stock in DB
        - Sets payment_status to 'unpaid'
        - Clears customer shopping cart
        """
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=2)

        initial_stock = self.product_standalone.stock_quantity  # 20
        idempotency_token = "test-token-uuid-12345"

        post_data = {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
            "idempotency_token": idempotency_token,
            "customer_notes": "Please ring bell twice",
        }

        response = self.client.post(reverse("core:checkout"), post_data)

        # Should redirect to order confirmation
        order = Order.objects.filter(user=self.user1).first()
        self.assertIsNotNone(order)
        self.assertRedirects(response, reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))

        # Verify Order data & address snapshot
        self.assertTrue(order.order_number.startswith("FM-"))
        self.assertEqual(order.shipping_full_name, "Aarav Patel")
        self.assertEqual(order.shipping_phone_number, "9876543210")
        self.assertEqual(order.shipping_address_line1, "Flat 402, Sunshine Heights")
        self.assertEqual(order.shipping_city, "Bengaluru")
        self.assertEqual(order.shipping_postal_code, "560001")
        self.assertEqual(order.customer_notes, "Please ring bell twice")

        # Verify Payment and Order status
        self.assertEqual(order.payment_method, Order.PAYMENT_METHOD_COD)
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)
        self.assertEqual(order.status, Order.STATUS_PENDING)

        # Verify Server Financial Calculation: 2 * 1999 = 3998 >= 499 (Free shipping)
        self.assertEqual(order.subtotal, Decimal("3998.00"))
        self.assertEqual(order.shipping_fee, Decimal("0.00"))
        self.assertEqual(order.total_amount, Decimal("3998.00"))

        # Verify OrderItem snapshot
        self.assertEqual(order.items.count(), 1)
        item = order.items.first()
        self.assertEqual(item.product, self.product_standalone)
        self.assertIsNone(item.variant)
        self.assertEqual(item.product_name, self.product_standalone.name)
        self.assertEqual(item.sku, self.product_standalone.sku)
        self.assertEqual(item.unit_price, Decimal("1999.00"))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.line_total, Decimal("3998.00"))

        # Verify stock decrement
        self.product_standalone.refresh_from_db()
        self.assertEqual(self.product_standalone.stock_quantity, initial_stock - 2)

        # Verify cart cleared
        cart_items_count = CartItem.objects.filter(cart__user=self.user1).count()
        self.assertEqual(cart_items_count, 0)

    def test_successful_order_placement_with_variant(self):
        """Verifies placing an order with a product variant records variant snapshots and reduces variant stock."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_with_variants, variant=self.variant_black, quantity=3)

        initial_var_stock = self.variant_black.stock_quantity  # 15

        post_data = {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
            "idempotency_token": "token-variant-order-1",
        }

        response = self.client.post(reverse("core:checkout"), post_data)
        order = Order.objects.filter(user=self.user1).first()
        self.assertIsNotNone(order)
        self.assertRedirects(response, reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))

        item = order.items.first()
        self.assertEqual(item.variant, self.variant_black)
        self.assertEqual(item.variant_label, "Color: Midnight Black")
        self.assertEqual(item.sku, "FP-EAR-BLK")
        self.assertEqual(item.unit_price, Decimal("1099.00"))
        self.assertEqual(item.quantity, 3)
        self.assertEqual(item.line_total, Decimal("3297.00"))

        # Verify variant stock decremented
        self.variant_black.refresh_from_db()
        self.assertEqual(self.variant_black.stock_quantity, initial_var_stock - 3)

    def test_server_side_shipping_fee_under_threshold(self):
        """Verify that orders below ₹499 have ₹49 shipping charge calculated on server."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_cheap, quantity=1)  # 1 * 299 = ₹299.00 < ₹499.00

        post_data = {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
            "idempotency_token": "token-cheap-shipping-test",
        }

        self.client.post(reverse("core:checkout"), post_data)
        order = Order.objects.filter(user=self.user1).first()

        self.assertIsNotNone(order)
        self.assertEqual(order.subtotal, Decimal("299.00"))
        self.assertEqual(order.shipping_fee, Decimal("49.00"))
        self.assertEqual(order.total_amount, Decimal("348.00"))

    def test_new_address_creation_during_checkout(self):
        """Verify that a customer can enter and save a new address directly on the checkout page."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=1)

        post_data = {
            "address_mode": "new",
            "full_name": "Aarav Work Office",
            "phone_number": "9876543299",
            "address_line1": "Level 5, Tech Park",
            "address_line2": "Outer Ring Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560103",
            "landmark": "Near Bellandur Flyover",
            "address_type": "work",
            "payment_method": "cod",
            "idempotency_token": "token-new-addr-test",
        }

        response = self.client.post(reverse("core:checkout"), post_data)
        order = Order.objects.filter(user=self.user1).first()
        self.assertIsNotNone(order)
        self.assertRedirects(response, reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))

        self.assertEqual(order.shipping_full_name, "Aarav Work Office")
        self.assertEqual(order.shipping_address_line1, "Level 5, Tech Park")
        self.assertEqual(order.shipping_postal_code, "560103")

        # Address saved to customer's profile
        saved = Address.objects.filter(user=self.user1, postal_code="560103").exists()
        self.assertTrue(saved)

    def test_invalid_address_rejection(self):
        """Selecting another user's address or submitting missing address data is rejected."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=1)

        # 1. Attempting to select user2's address while logged in as user1
        post_data_foreign = {
            "address_mode": "existing",
            "address_id": self.address2.pk,  # belongs to user2
            "payment_method": "cod",
        }
        resp = self.client.post(reverse("core:checkout"), post_data_foreign)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

        # 2. Submitting empty new address
        post_data_empty = {
            "address_mode": "new",
            "full_name": "",
            "phone_number": "",
            "address_line1": "",
            "city": "",
            "state": "",
            "postal_code": "",
            "payment_method": "cod",
        }
        resp2 = self.client.post(reverse("core:checkout"), post_data_empty)
        self.assertEqual(resp2.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_insufficient_stock_prevents_order_and_rolls_back(self):
        """If inventory drops before checkout completion, order is aborted and no stock is lost."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=5)

        # Simulate concurrent purchase that reduces stock to 2
        self.product_standalone.stock_quantity = 2
        self.product_standalone.save(update_fields=["stock_quantity"])

        post_data = {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
        }

        response = self.client.post(reverse("core:checkout"), post_data)
        self.assertRedirects(response, reverse("core:cart_detail"))
        self.assertEqual(Order.objects.count(), 0)

        # Stock remains at 2 without decrement
        self.product_standalone.refresh_from_db()
        self.assertEqual(self.product_standalone.stock_quantity, 2)

    def test_inactive_product_prevents_order(self):
        """Deactivated products cannot be purchased."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=1)

        self.product_standalone.is_active = False
        self.product_standalone.save(update_fields=["is_active"])

        post_data = {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
        }

        response = self.client.post(reverse("core:checkout"), post_data)
        self.assertRedirects(response, reverse("core:cart_detail"))
        self.assertEqual(Order.objects.count(), 0)

    def test_duplicate_submission_idempotency_prevents_double_ordering(self):
        """Repeated submissions with the same idempotency token do not create duplicate orders or deduct stock twice."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=2)

        token = "idempotent-repeat-test-token"
        post_data = {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
            "idempotency_token": token,
        }

        # First submission
        resp1 = self.client.post(reverse("core:checkout"), post_data)
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.first()
        self.assertRedirects(resp1, reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))

        initial_stock_after_first = self.product_standalone.stock_quantity - 2

        # Second rapid submission with the same idempotency token
        resp2 = self.client.post(reverse("core:checkout"), post_data)
        self.assertEqual(Order.objects.count(), 1)  # Still exactly 1 order
        self.assertRedirects(resp2, reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))

        # Stock was only deducted once
        self.product_standalone.refresh_from_db()
        self.assertEqual(self.product_standalone.stock_quantity, initial_stock_after_first)

    def test_order_ownership_permission_protection(self):
        """Customer B cannot view Customer A's order confirmation or order detail page."""
        # User 1 places an order
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=1)
        self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
        })
        order = Order.objects.filter(user=self.user1).first()
        self.assertIsNotNone(order)

        # User 2 logs in on their own browser session
        client2 = APIClient()
        client2.force_login(self.user2)

        # Attempt to access User 1's confirmation page
        conf_url = reverse("core:order_confirmation", kwargs={"order_number": order.order_number})
        resp_conf = client2.get(conf_url)
        self.assertEqual(resp_conf.status_code, 404)

        # Attempt to access User 1's order details page
        detail_url = reverse("core:order_detail", kwargs={"order_number": order.order_number})
        resp_detail = client2.get(detail_url)
        self.assertEqual(resp_detail.status_code, 404)

        # User 2's My Orders page does not display User 1's order
        orders_resp = client2.get(reverse("core:account_orders"))
        self.assertEqual(orders_resp.status_code, 200)
        self.assertNotContains(orders_resp, order.order_number)

    def test_authenticated_my_orders_page_and_dashboard_integration(self):
        """User can view their order history list and dashboard displays active order count."""
        self.client.force_login(self.user1)
        self._add_to_cart(self.user1, self.product_standalone, quantity=1)
        self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.address1.pk,
            "payment_method": "cod",
        })
        order = Order.objects.filter(user=self.user1).first()

        # My Orders page
        resp_orders = self.client.get(reverse("core:account_orders"))
        self.assertEqual(resp_orders.status_code, 200)
        self.assertContains(resp_orders, order.order_number)
        self.assertContains(resp_orders, "COD (Unpaid)")

        # Dashboard page
        resp_dash = self.client.get(reverse("core:account_dashboard"))
        self.assertEqual(resp_dash.status_code, 200)
        self.assertContains(resp_dash, "Manage Orders (1)")

    def test_admin_order_management_actions_and_status(self):
        """Admin can view orders and update fulfillment/payment statuses via actions."""
        admin_user = self.User.objects.create_superuser(
            username="admin@fitmeezo.in",
            email="admin@fitmeezo.in",
            password="AdminPassword123!",
        )
        self.client.force_login(admin_user)

        order = Order.objects.create(
            order_number="FM-TEST-0001",
            user=self.user1,
            shipping_full_name="Aarav Patel",
            shipping_phone_number="9876543210",
            shipping_address_line1="402 Sunshine",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1999.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1999.00"),
            status=Order.STATUS_PENDING,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )

        # Admin changelist
        changelist_url = reverse("admin:core_order_changelist")
        resp = self.client.get(changelist_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "FM-TEST-0001")

        # Admin action: Mark as Shipped
        self.client.post(changelist_url, {
            "action": "mark_shipped",
            "_selected_action": [order.pk],
        })
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_SHIPPED)

        # Admin action: Mark payment as Paid
        self.client.post(changelist_url, {
            "action": "mark_payment_paid",
            "_selected_action": [order.pk],
        })
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)


class OnlinePaymentAndOrderAdminTest(TestCase):
    """
    Test suite for Razorpay Online Payment Gateway, Webhooks,
    Email notifications, Order Administration rules, and Staff permission enforcement.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="onlinebuyer",
            email="onlinebuyer@fitmeezo.in",
            password="StrongPassword123!",
        )
        self.staff_user = get_user_model().objects.create_user(
            username="staffmanager",
            email="staff@fitmeezo.in",
            password="StaffPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        self.category = Category.objects.create(
            name="Fitness Trackers",
            slug="fitness-trackers",
        )
        self.product = Product.objects.create(
            name="Smart Health Band 5",
            slug="smart-health-band-5",
            sku="SHB5-BASE",
            category=self.category,
            price=Decimal("1500.00"),
            stock_quantity=15,
            is_active=True,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Color",
            option_value="Obsidian Black",
            sku="SHB5-BLK",
            price=Decimal("1500.00"),
            stock_quantity=10,
            is_active=True,
        )
        self.address = Address.objects.create(
            user=self.user,
            full_name="Online Buyer",
            phone_number="9876543210",
            address_line1="404 Tech Park",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            address_type="work",
            is_default=True,
        )
        self.client.force_login(self.user)
        mail.outbox.clear()

    def _add_to_cart(self, quantity=1):
        cart, _ = Cart.objects.get_or_create(user=self.user)
        CartItem.objects.create(
            cart=cart,
            product=self.product,
            variant=self.variant,
            quantity=quantity,
        )

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_ENABLED=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    @patch("razorpay.Client")
    def test_razorpay_order_created_on_checkout_with_verified_totals(self, mock_client_cls):
        """
        Tests that selecting Razorpay creates the Razorpay order on the server
        with the exact verified amount in paise and redirects to payment page.
        """
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.order.create.return_value = {"id": "order_mock_rp_12345"}

        self._add_to_cart(quantity=2)  # 2 * 1500 = 3000 >= 499 => Free shipping => 3000.00 => 300000 paise

        response = self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.address.pk,
            "payment_method": "razorpay",
            "idempotency_token": "token_rp_checkout_01",
        })

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(idempotency_key="token_rp_checkout_01")
        self.assertRedirects(response, reverse("core:razorpay_payment_page", kwargs={"order_number": order.order_number}))

        # Verify server verified amount passed to Razorpay in paise
        mock_instance.order.create.assert_called_once_with({
            "amount": 300000,
            "currency": "INR",
            "receipt": order.order_number,
            "notes": {
                "order_number": order.order_number,
                "user_id": str(self.user.id),
                "user_email": self.user.email,
            }
        })

        self.assertEqual(order.payment_method, Order.PAYMENT_METHOD_RAZORPAY)
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)
        self.assertEqual(order.razorpay_order_id, "order_mock_rp_12345")
        self.assertEqual(order.total_amount, Decimal("3000.00"))

        # Verify order confirmation email is not dispatched prematurely while order is unpaid
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(RAZORPAY_ENABLED=False)
    def test_online_payment_rejected_when_gateway_disabled(self):
        """
        Tests that selecting online payment is safely rejected when credentials are not configured.
        """
        self._add_to_cart(quantity=1)
        response = self.client.post(reverse("core:checkout"), {
            "address_mode": "existing",
            "address_id": self.address.pk,
            "payment_method": "razorpay",
        })
        self.assertRedirects(response, reverse("core:checkout"))
        self.assertEqual(Order.objects.count(), 0)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_ENABLED=True,
    )
    def test_razorpay_payment_page_renders_for_unpaid_order(self):
        """
        Tests that payment page displays correct order details, amount in paise, and brand info.
        """
        order = Order.objects.create(
            order_number="FM-20261010-PAY01",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            razorpay_order_id="order_rp_render_test",
        )
        response = self.client.get(reverse("core:razorpay_payment_page", kwargs={"order_number": order.order_number}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "order_rp_render_test")
        self.assertEqual(response.context["amount_in_paise"], 150000)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_ENABLED=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    @patch("razorpay.Client")
    def test_payment_signature_verification_success_marks_paid_and_emails(self, mock_client_cls):
        """
        Tests that a valid cryptographic signature transitions order to Paid,
        records paid_at and razorpay_payment_id, and dispatches payment success email.
        """
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        # verify_payment_signature returns None / doesn't raise error on valid signature
        mock_instance.utility.verify_payment_signature.return_value = None

        order = Order.objects.create(
            order_number="FM-20261010-VER01",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            razorpay_order_id="order_rp_ver_123",
        )

        response = self.client.post(reverse("core:razorpay_payment_verify"), {
            "order_number": order.order_number,
            "razorpay_order_id": "order_rp_ver_123",
            "razorpay_payment_id": "pay_mock_settled_777",
            "razorpay_signature": "mock_crypto_signature_abc",
        })

        self.assertRedirects(response, reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.razorpay_payment_id, "pay_mock_settled_777")
        self.assertEqual(order.razorpay_signature, "mock_crypto_signature_abc")
        self.assertIsNotNone(order.paid_at)

        # Verify payment success email was sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Payment Received", mail.outbox[0].subject)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_ENABLED=True,
    )
    @patch("razorpay.Client")
    def test_invalid_payment_signature_fails_and_does_not_mark_paid(self, mock_client_cls):
        """
        Tests that an invalid or tampered signature is rejected and order remains Unpaid.
        """
        import razorpay.errors
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_payment_signature.side_effect = razorpay.errors.SignatureVerificationError("Signature mismatch")

        order = Order.objects.create(
            order_number="FM-20261010-BADSIG",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            razorpay_order_id="order_rp_badsig",
        )

        response = self.client.post(reverse("core:razorpay_payment_verify"), {
            "order_number": order.order_number,
            "razorpay_order_id": "order_rp_badsig",
            "razorpay_payment_id": "pay_tampered_999",
            "razorpay_signature": "fake_tampered_signature",
        })

        self.assertRedirects(response, reverse("core:order_detail", kwargs={"order_number": order.order_number}))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)
        self.assertEqual(order.razorpay_payment_id, "")
        self.assertIsNone(order.paid_at)

    def test_duplicate_payment_callback_handling(self):
        """
        Tests that duplicate verification callbacks are idempotent and do not error.
        """
        order = Order.objects.create(
            order_number="FM-20261010-DUP01",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            razorpay_payment_id="pay_already_settled",
        )

        response = self.client.post(reverse("core:razorpay_payment_verify"), {
            "order_number": order.order_number,
            "razorpay_order_id": "order_rp_dup",
            "razorpay_payment_id": "pay_already_settled",
            "razorpay_signature": "sig_dup",
        })
        self.assertRedirects(response, reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_payment_failed_callback_restores_inventory(self):
        """
        Tests that failed or cancelled online payment marks order failed,
        cancels fulfillment, restores product inventory, and sends failure email.
        """
        initial_variant_stock = self.variant.stock_quantity  # 10

        order = Order.objects.create(
            order_number="FM-20261010-FAIL01",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("3000.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("3000.00"),
            status=Order.STATUS_PENDING,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            variant=self.variant,
            product_name=self.product.name,
            variant_label="Color: Obsidian Black",
            unit_price=Decimal("1500.00"),
            quantity=3,
            line_total=Decimal("4500.00"),
        )
        # Deduct 3 units as would happen during checkout creation
        self.variant.stock_quantity -= 3
        self.variant.save()

        response = self.client.post(reverse("core:razorpay_payment_failed"), {
            "order_number": order.order_number,
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Payment was cancelled by the customer.",
        })

        self.assertRedirects(response, reverse("core:order_detail", kwargs={"order_number": order.order_number}))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_FAILED)
        self.assertEqual(order.status, Order.STATUS_CANCELLED)

        # Inventory restored
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, initial_variant_stock)

        # Failure email sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Payment Unsuccessful", mail.outbox[0].subject)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    @patch("razorpay.Client")
    def test_webhook_order_paid_event_marks_order_as_paid(self, mock_client_cls):
        """
        Tests that an asynchronous Razorpay webhook with valid signature
        marks the order as paid and triggers notification.
        """
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.return_value = None

        order = Order.objects.create(
            order_number="FM-20261010-WH01",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            razorpay_order_id="order_rp_webhook_tgt",
        )

        import json
        payload = json.dumps({
            "event": "order.paid",
            "payload": {
                "payment": {
                    "entity": {
                        "order_id": "order_rp_webhook_tgt",
                        "id": "pay_webhook_captured_99",
                    }
                }
            }
        })

        # Client unauthenticated for webhook
        anon_client = APIClient()
        response = anon_client.post(
            reverse("core:razorpay_webhook"),
            data=payload,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid_wh_sig",
        )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)
        self.assertEqual(order.razorpay_payment_id, "pay_webhook_captured_99")
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_mock_key",
        RAZORPAY_KEY_SECRET="mock_secret_key",
        RAZORPAY_WEBHOOK_SECRET="mock_wh_secret",
    )
    @patch("razorpay.Client")
    def test_webhook_invalid_signature_returns_403(self, mock_client_cls):
        """
        Tests that an unauthorized webhook payload with invalid signature is rejected with 403.
        """
        import razorpay.errors
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.utility.verify_webhook_signature.side_effect = razorpay.errors.SignatureVerificationError("Invalid webhook signature")

        anon_client = APIClient()
        response = anon_client.post(
            reverse("core:razorpay_webhook"),
            data="{}",
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="tampered_wh_sig",
        )
        self.assertEqual(response.status_code, 403)

    def test_customer_cannot_access_order_admin(self):
        """
        Tests that a regular customer (is_staff=False) cannot access Django admin.
        """
        # User is regular customer
        response = self.client.get("/admin/core/order/")
        # Django admin redirects non-staff to admin login or returns 302
        self.assertIn(response.status_code, (302, 403))

    def test_admin_cannot_mark_online_order_paid_without_payment_id(self):
        """
        Tests that staff cannot mark an online order Paid without a verified Razorpay payment ID.
        """
        order = Order.objects.create(
            order_number="FM-20261010-ADMIN01",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
            razorpay_payment_id="",  # Missing payment ID
        )

        from django.test import Client as DjangoTestClient
        admin_client = DjangoTestClient()
        admin_client.force_login(self.staff_user)

        response = admin_client.post(reverse("admin:core_order_changelist"), {
            "action": "mark_payment_paid",
            "_selected_action": [str(order.pk)],
            "index": "0",
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        # Order should NOT have been updated to paid
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_UNPAID)

    def test_admin_can_mark_cod_order_paid_on_collection(self):
        """
        Tests that staff can mark a COD order as Paid upon documented doorstep collection.
        """
        order = Order.objects.create(
            order_number="FM-20261010-ADMINCOD",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )

        from django.test import Client as DjangoTestClient
        admin_client = DjangoTestClient()
        admin_client.force_login(self.staff_user)

        response = admin_client.post(reverse("admin:core_order_changelist"), {
            "action": "mark_payment_paid",
            "_selected_action": [str(order.pk)],
            "index": "0",
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_PAID)

    def test_order_model_clean_prevents_online_order_paid_without_payment_id(self):
        """
        Tests model-level clean() validation preventing online order from having payment_status='paid' without razorpay_payment_id.
        """
        order = Order(
            order_number="FM-20261010-CLEAN01",
            user=self.user,
            shipping_full_name="Online Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="404 Tech Park",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            razorpay_payment_id="",  # Invalid: no payment id
        )
        with self.assertRaises(ValidationError):
            order.clean()












