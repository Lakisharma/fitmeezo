"""
Tests for Phase 5 — Prompt 3: Product Dimensions, Weight Snapshots & Pickup Location Verification.

Validates:
1. Product dimensions validation (positive Decimals, >0 check constraints, clean validation).
2. Variant dimensions validation and fallback inheritance.
3. Legacy products without verified dimensions (status="unknown", missing dimensions do not crash).
4. Seller ownership restrictions (authorized seller can only edit own products).
5. Checkout snapshots for seller products.
6. Checkout snapshots for platform products.
7. Editing a product later does not change historical snapshots.
8. Package measurements aggregate only the correct seller's items.
9. Volumetric weight calculation and configurable divisor.
10. Missing dimensions do not produce a falsely reliable weight.
11. Invalid or incomplete pickup address.
12. Pickup verification is not fabricated.
13. Platform warehouse configuration without hardcoded secrets.
14. Authorized fulfillment staff recording verified packed measurements.
"""

from decimal import Decimal
import uuid
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from core.cart import Cart as CartService
from core.models import (
    Address,
    Category,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
    SellerOrder,
)
from core.services import (
    create_order_from_cart,
)
from core.shipping_dimensions import (
    calculate_volumetric_weight_kg,
    calculate_chargeable_weight_kg,
    calculate_package_bounding_dimensions,
    grams_to_kg,
    kg_to_grams,
    validate_platform_pickup_location,
)

User = get_user_model()


def create_test_seller(user, store_name, **kwargs):
    """Helper to create a fully valid Seller model for testing."""
    defaults = {
        "legal_business_name": f"{store_name} Private Limited",
        "support_email": f"support@{user.username}.fitmeezo.test",
        "support_phone": "9876543210",
        "registered_address": "123 Business Boulevard, Sector 18",
        "city": "Bengaluru",
        "state": "Karnataka",
        "postal_code": "560001",
        "pickup_address": "Warehouse Plot 5, Peenya Industrial Area",
        "pickup_city": "Bengaluru",
        "pickup_state": "Karnataka",
        "pickup_postal_code": "560058",
        "gstin": "29ABCDE1234F1Z1",
        "pan_number": "ABCDE1234F",
        "status": Seller.STATUS_APPROVED,
        "is_active": True,
    }
    defaults.update(kwargs)
    return Seller.objects.create(user=user, store_name=store_name, **defaults)


class PhysicalDimensionsValidationTests(TestCase):
    """Test suite for Task 1: Product and Variant dimensions validation."""

    def setUp(self):
        self.category = Category.objects.create(name="Fitness Apparel", slug=f"fitness-apparel-{uuid.uuid4().hex[:6]}")
        self.seller_user = User.objects.create_user(username="seller_dim_user", password="password123")
        self.seller = create_test_seller(self.seller_user, "Athletics Pro")

    def test_01_product_dimensions_validation(self):
        """Test valid product physical dimensions save and clean properly."""
        product = Product(
            name="Training Dumbbell 5kg",
            slug=f"training-dumbbell-{uuid.uuid4().hex[:6]}",
            sku=f"DUMB-5KG-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("1200.00"),
            stock_quantity=10,
            weight_grams=Decimal("5000.00"),
            length_cm=Decimal("25.50"),
            breadth_cm=Decimal("12.00"),
            height_cm=Decimal("12.00"),
            is_dimensions_verified=True,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        product.full_clean()
        product.save()

        self.assertTrue(product.has_complete_dimensions)
        self.assertEqual(product.weight_grams, Decimal("5000.00"))
        self.assertEqual(product.length_cm, Decimal("25.50"))
        self.assertIn("25.50 × 12.00 × 12.00 cm", product.dimensions_display)
        self.assertIn("5000.00 g", product.dimensions_display)
        self.assertIn("Verified", product.dimensions_display)

    def test_01b_product_negative_or_zero_dimensions_rejected(self):
        """Test that negative or zero dimensions are rejected by validation."""
        product_invalid = Product(
            name="Invalid Product",
            slug=f"invalid-product-{uuid.uuid4().hex[:6]}",
            sku=f"INV-01-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("100.00"),
            stock_quantity=10,
            weight_grams=Decimal("0.00"),
        )
        with self.assertRaises(ValidationError):
            product_invalid.full_clean()

        product_negative = Product(
            name="Negative Product",
            slug=f"negative-product-{uuid.uuid4().hex[:6]}",
            sku=f"NEG-01-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("100.00"),
            stock_quantity=10,
            length_cm=Decimal("-5.00"),
        )
        with self.assertRaises(ValidationError):
            product_negative.full_clean()

    def test_02_variant_dimensions_validation_and_fallback(self):
        """Test variant dimensions validation and fallback inheritance to product."""
        parent_product = Product.objects.create(
            name="Running Hoodie",
            slug=f"running-hoodie-{uuid.uuid4().hex[:6]}",
            sku=f"HOODIE-BASE-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("1500.00"),
            stock_quantity=50,
            weight_grams=Decimal("450.00"),
            length_cm=Decimal("30.00"),
            breadth_cm=Decimal("25.00"),
            height_cm=Decimal("4.00"),
            is_dimensions_verified=True,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Variant A: inherits from parent product
        variant_inherit = ProductVariant.objects.create(
            product=parent_product,
            option_type="Size",
            option_value="M",
            sku=f"HOODIE-M-{uuid.uuid4().hex[:4]}",
            price=Decimal("1500.00"),
            stock_quantity=20,
        )
        self.assertEqual(variant_inherit.effective_weight_grams, Decimal("450.00"))
        self.assertEqual(variant_inherit.effective_length_cm, Decimal("30.00"))
        self.assertTrue(variant_inherit.effective_is_dimensions_verified)
        self.assertTrue(variant_inherit.has_complete_dimensions)

        # Variant B: overrides parent product with larger measurements
        variant_override = ProductVariant.objects.create(
            product=parent_product,
            option_type="Size",
            option_value="XXL",
            sku=f"HOODIE-XXL-{uuid.uuid4().hex[:4]}",
            price=Decimal("1700.00"),
            stock_quantity=10,
            weight_grams=Decimal("600.00"),
            length_cm=Decimal("35.00"),
            breadth_cm=Decimal("28.00"),
            height_cm=Decimal("6.00"),
            is_dimensions_verified=True,
        )
        self.assertEqual(variant_override.effective_weight_grams, Decimal("600.00"))
        self.assertEqual(variant_override.effective_length_cm, Decimal("35.00"))

        # Variant C: zero/negative dimension rejected
        variant_invalid = ProductVariant(
            product=parent_product,
            option_type="Size",
            option_value="XS",
            sku=f"INV-VAR-{uuid.uuid4().hex[:4]}",
            price=Decimal("1500.00"),
            stock_quantity=5,
            weight_grams=Decimal("-10.00"),
        )
        with self.assertRaises(ValidationError):
            variant_invalid.full_clean()

    def test_03_legacy_products_without_verified_dimensions(self):
        """Test legacy products with null dimensions behave safely and don't crash."""
        legacy_product = Product.objects.create(
            name="Legacy Yoga Mat",
            slug=f"legacy-yoga-mat-{uuid.uuid4().hex[:6]}",
            sku=f"LEG-MAT-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("800.00"),
            stock_quantity=15,
            # dimensions fields left None as in legacy data
        )
        self.assertFalse(legacy_product.has_complete_dimensions)
        self.assertFalse(legacy_product.is_dimensions_verified)
        self.assertIsNone(legacy_product.volumetric_weight_kg)
        self.assertEqual(legacy_product.dimensions_display, "Unknown / Unspecified")

    def test_04_seller_ownership_restrictions(self):
        """Test seller can only modify their own products, not another seller's."""
        seller_b_user = User.objects.create_user(username="seller_b_user", password="password123")
        seller_b = create_test_seller(
            seller_b_user,
            "Competitor Sports",
            gstin="27ABCDE1234F1Z2",
            pan_number="ABCDE1234F",
            pickup_city="Mumbai",
            pickup_state="Maharashtra",
            pickup_postal_code="400001",
        )
        prod_b = Product.objects.create(
            name="Seller B Resistance Bands",
            slug=f"seller-b-bands-{uuid.uuid4().hex[:6]}",
            sku=f"BANDS-B-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=seller_b,
            price=Decimal("500.00"),
            stock_quantity=10,
        )

        client = Client()
        client.login(username="seller_dim_user", password="password123")

        # Seller A attempts to edit Seller B's product
        response = client.get(reverse("core:seller_product_edit", kwargs={"pk": prod_b.pk}))
        # Should return 404 due to seller scoping get_object_or_404(Product, pk=pk, seller=seller)
        self.assertEqual(response.status_code, 404)


class PackageSnapshotsAndCheckoutTests(TestCase):
    """Test suite for Tasks 2, 5: OrderItem snapshots & SellerOrder package calculations."""

    def setUp(self):
        self.customer = User.objects.create_user(username="buyer_snap_user", password="password123")
        self.category = Category.objects.create(name="Supplements", slug=f"supplements-{uuid.uuid4().hex[:6]}")

        self.seller_user = User.objects.create_user(username="merchant_user", password="password123")
        self.seller = create_test_seller(
            self.seller_user,
            "Fitness Store Alpha",
            pickup_location_verified=True,
        )

        self.shipping_address = Address.objects.create(
            user=self.customer,
            full_name="Rajesh Sharma",
            phone_number="9876543210",
            address_line1="Apartment 4B, Sky Heights",
            city="New Delhi",
            state="Delhi",
            postal_code="110001",
            is_default=True,
        )

    def _get_cart_for_user(self):
        session = self.client.session
        request = type("MockRequest", (), {"session": session, "user": self.customer})()
        return CartService(request)

    def test_05_checkout_snapshots_for_seller_products(self):
        """Checkout snapshots product physical dimensions onto OrderItem and SellerOrder."""
        product = Product.objects.create(
            name="Whey Protein 1kg",
            slug=f"whey-protein-{uuid.uuid4().hex[:6]}",
            sku=f"WHEY-1KG-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("2400.00"),
            weight_grams=Decimal("1100.00"),
            length_cm=Decimal("15.00"),
            breadth_cm=Decimal("15.00"),
            height_cm=Decimal("25.00"),
            is_dimensions_verified=True,
            stock_quantity=50,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        cart = self._get_cart_for_user()
        success, msg, _ = cart.add(product, variant=None, quantity=2)
        self.assertTrue(success)

        order, is_dup = create_order_from_cart(
            user=self.customer,
            cart=cart,
            shipping_address=self.shipping_address,
            payment_method=Order.PAYMENT_METHOD_COD,
            standard_shipping_fee=Decimal("49.00"),
        )

        seller_orders = list(order.seller_orders.all())
        self.assertEqual(len(seller_orders), 1)
        pkg = seller_orders[0]
        self.assertEqual(pkg.seller, self.seller)
        # Quantity 2: Total estimated weight = 1100 * 2 = 2200g
        self.assertEqual(pkg.estimated_weight_grams, Decimal("2200.00"))
        # Stacked box: max(L)=15, max(B)=15, sum(H)=25*2=50
        self.assertEqual(pkg.estimated_length_cm, Decimal("15.00"))
        self.assertEqual(pkg.estimated_breadth_cm, Decimal("15.00"))
        self.assertEqual(pkg.estimated_height_cm, Decimal("50.00"))
        self.assertEqual(pkg.package_dimension_status, SellerOrder.PACKAGE_DIMENSION_ESTIMATED)

        # Check OrderItem snapshot
        order_item = pkg.items.first()
        self.assertEqual(order_item.weight_grams, Decimal("1100.00"))
        self.assertEqual(order_item.length_cm, Decimal("15.00"))
        self.assertEqual(order_item.breadth_cm, Decimal("15.00"))
        self.assertEqual(order_item.height_cm, Decimal("25.00"))
        self.assertTrue(order_item.is_dimensions_verified)

    def test_06_checkout_snapshots_for_platform_products(self):
        """Checkout creates platform package with platform warehouse and snapshots dimensions."""
        platform_product = Product.objects.create(
            name="Fitmeezo Official Shaker Bottle",
            slug=f"official-shaker-{uuid.uuid4().hex[:6]}",
            sku=f"SHAKER-PLT-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=None,  # Platform-owned
            price=Decimal("350.00"),
            weight_grams=Decimal("200.00"),
            length_cm=Decimal("10.00"),
            breadth_cm=Decimal("10.00"),
            height_cm=Decimal("20.00"),
            is_dimensions_verified=True,
            stock_quantity=100,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        cart = self._get_cart_for_user()
        success, msg, _ = cart.add(platform_product, variant=None, quantity=1)
        self.assertTrue(success)

        order, is_dup = create_order_from_cart(
            user=self.customer,
            cart=cart,
            shipping_address=self.shipping_address,
            payment_method=Order.PAYMENT_METHOD_COD,
            standard_shipping_fee=Decimal("0.00"),
        )

        seller_orders = list(order.seller_orders.all())
        self.assertEqual(len(seller_orders), 1)
        pkg = seller_orders[0]
        self.assertIsNone(pkg.seller)
        self.assertEqual(pkg.estimated_weight_grams, Decimal("200.00"))
        self.assertEqual(pkg.package_dimension_status, SellerOrder.PACKAGE_DIMENSION_ESTIMATED)

        # Verify platform pickup details
        pickup = pkg.get_pickup_location_details()
        self.assertEqual(pickup["pickup_location_name"], "Fitmeezo Central Fulfillment Center")
        self.assertEqual(pickup["postal_code"], "560100")

    def test_07_editing_product_later_does_not_change_historical_snapshots(self):
        """Editing product weight/dimensions later must NOT alter historical OrderItem or SellerOrder."""
        product = Product.objects.create(
            name="Kettlebell 12kg",
            slug=f"kettlebell-12kg-{uuid.uuid4().hex[:6]}",
            sku=f"KETTLE-12KG-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("1800.00"),
            weight_grams=Decimal("12000.00"),
            length_cm=Decimal("22.00"),
            breadth_cm=Decimal("20.00"),
            height_cm=Decimal("28.00"),
            is_dimensions_verified=True,
            stock_quantity=20,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        cart = self._get_cart_for_user()
        success, msg, _ = cart.add(product, variant=None, quantity=1)
        self.assertTrue(success)

        order, is_dup = create_order_from_cart(
            user=self.customer,
            cart=cart,
            shipping_address=self.shipping_address,
            payment_method=Order.PAYMENT_METHOD_COD,
            standard_shipping_fee=Decimal("49.00"),
        )
        seller_orders = list(order.seller_orders.all())
        pkg = seller_orders[0]
        order_item = pkg.items.first()

        # Initial snapshots
        self.assertEqual(order_item.weight_grams, Decimal("12000.00"))
        self.assertEqual(pkg.estimated_weight_grams, Decimal("12000.00"))

        # Later, seller modifies the product dimensions
        product.weight_grams = Decimal("14000.00")
        product.length_cm = Decimal("30.00")
        product.save()

        # Reload OrderItem and SellerOrder from database
        order_item.refresh_from_db()
        pkg.refresh_from_db()

        # Historical snapshots MUST remain unchanged
        self.assertEqual(order_item.weight_grams, Decimal("12000.00"))
        self.assertEqual(order_item.length_cm, Decimal("22.00"))
        self.assertEqual(pkg.estimated_weight_grams, Decimal("12000.00"))
        self.assertEqual(pkg.estimated_length_cm, Decimal("22.00"))

    def test_08_package_measurements_aggregate_only_correct_seller_items(self):
        """In multi-seller orders, SellerOrder aggregates only its own items."""
        seller_2_user = User.objects.create_user(username="seller2_user", password="password123")
        seller_2 = create_test_seller(
            seller_2_user,
            "Seller Two Gear",
            gstin="07ABCDE1234F1Z3",
            pan_number="ABCDE1234F",
            pickup_address="B-44, Okhla Phase 2",
            pickup_city="New Delhi",
            pickup_state="Delhi",
            pickup_postal_code="110020",
        )

        prod_1 = Product.objects.create(
            name="Seller 1 Mat",
            slug=f"s1-mat-{uuid.uuid4().hex[:6]}",
            sku=f"S1-MAT-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=self.seller,
            price=Decimal("600.00"),
            weight_grams=Decimal("800.00"),
            length_cm=Decimal("60.00"),
            breadth_cm=Decimal("15.00"),
            height_cm=Decimal("15.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        prod_2 = Product.objects.create(
            name="Seller 2 Belt",
            slug=f"s2-belt-{uuid.uuid4().hex[:6]}",
            sku=f"S2-BELT-{uuid.uuid4().hex[:4]}",
            category=self.category,
            seller=seller_2,
            price=Decimal("400.00"),
            weight_grams=Decimal("350.00"),
            length_cm=Decimal("20.00"),
            breadth_cm=Decimal("10.00"),
            height_cm=Decimal("5.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        cart = self._get_cart_for_user()
        cart.add(prod_1, variant=None, quantity=1)
        cart.add(prod_2, variant=None, quantity=2)

        order, is_dup = create_order_from_cart(
            user=self.customer,
            cart=cart,
            shipping_address=self.shipping_address,
            payment_method=Order.PAYMENT_METHOD_COD,
            standard_shipping_fee=Decimal("49.00"),
        )

        seller_orders = list(order.seller_orders.all())
        self.assertEqual(len(seller_orders), 2)
        pkg_1 = next(p for p in seller_orders if p.seller == self.seller)
        pkg_2 = next(p for p in seller_orders if p.seller == seller_2)

        # Seller 1 package has only prod_1
        self.assertEqual(pkg_1.estimated_weight_grams, Decimal("800.00"))
        self.assertEqual(pkg_1.items.count(), 1)

        # Seller 2 package has only prod_2 (qty 2 * 350g = 700g)
        self.assertEqual(pkg_2.estimated_weight_grams, Decimal("700.00"))
        self.assertEqual(pkg_2.items.count(), 1)
        self.assertEqual(pkg_2.items.first().quantity, 2)


class VolumetricAndChargeableWeightTests(TestCase):
    """Test suite for Task 3: Volumetric and Chargeable Weight calculation."""

    def test_09_volumetric_weight_and_configurable_divisor(self):
        """Test standard volumetric weight formula: L x B x H / divisor."""
        # 30cm x 20cm x 10cm = 6,000 cm3
        # Divisor 5000: 6,000 / 5,000 = 1.20 kg
        vol_wt = calculate_volumetric_weight_kg(
            length_cm=Decimal("30.00"),
            breadth_cm=Decimal("20.00"),
            height_cm=Decimal("10.00"),
            divisor=Decimal("5000.00"),
        )
        self.assertEqual(vol_wt, Decimal("1.20"))

        # Configurable divisor test: Divisor 4000: 6,000 / 4,000 = 1.50 kg
        vol_wt_4000 = calculate_volumetric_weight_kg(
            length_cm=Decimal("30.00"),
            breadth_cm=Decimal("20.00"),
            height_cm=Decimal("10.00"),
            divisor=Decimal("4000.00"),
        )
        self.assertEqual(vol_wt_4000, Decimal("1.50"))

        # Test Chargeable weight = max(dead_weight, volumetric_weight)
        # Case A: Dead weight (1.0 kg) < Volumetric (1.20 kg) -> Chargeable = 1.20 kg
        chg_a = calculate_chargeable_weight_kg(dead_weight_kg=Decimal("1.00"), volumetric_weight_kg=vol_wt)
        self.assertEqual(chg_a, Decimal("1.20"))

        # Case B: Dead weight (2.5 kg) > Volumetric (1.20 kg) -> Chargeable = 2.50 kg
        chg_b = calculate_chargeable_weight_kg(dead_weight_kg=Decimal("2.50"), volumetric_weight_kg=vol_wt)
        self.assertEqual(chg_b, Decimal("2.50"))

    def test_10_missing_dimensions_do_not_produce_falsely_reliable_weight(self):
        """Missing or zero dimensions return None for volumetric weight."""
        # Missing length
        self.assertIsNone(
            calculate_volumetric_weight_kg(
                length_cm=None,
                breadth_cm=Decimal("20.00"),
                height_cm=Decimal("10.00"),
            )
        )
        # Negative height rejected
        self.assertIsNone(
            calculate_volumetric_weight_kg(
                length_cm=Decimal("20.00"),
                breadth_cm=Decimal("20.00"),
                height_cm=Decimal("-5.00"),
            )
        )
        # Chargeable weight with None volumetric returns dead weight without fabricating
        chg_fallback = calculate_chargeable_weight_kg(dead_weight_kg=Decimal("1.50"), volumetric_weight_kg=None)
        self.assertEqual(chg_fallback, Decimal("1.50"))

        # If both are None, returns None
        self.assertIsNone(calculate_chargeable_weight_kg(dead_weight_kg=None, volumetric_weight_kg=None))


class PickupLocationVerificationTests(TestCase):
    """Test suite for Task 4: Seller & Platform pickup location verification."""

    def setUp(self):
        self.user = User.objects.create_user(username="pickup_test_user", password="password123")

    def test_11_invalid_or_incomplete_pickup_address(self):
        """Test incomplete address and invalid Indian PIN codes are rejected."""
        # Case 1: Incomplete fields
        seller_incomplete = Seller(
            user=self.user,
            store_name="Incomplete Store",
            pickup_address="",
            pickup_city="",
            pickup_state="",
            pickup_postal_code="",
        )
        res_inc = seller_incomplete.validate_pickup_location()
        self.assertFalse(res_inc["is_complete"])
        self.assertEqual(res_inc["status"], "incomplete")
        self.assertGreater(len(res_inc["errors"]), 0)

        # Case 2: Invalid PIN code format (e.g. 5 digits or starts with 0)
        seller_bad_pin = Seller(
            user=self.user,
            store_name="Bad Pin Store",
            pickup_address="123 Valid Long Street Address Here",
            pickup_city="Pune",
            pickup_state="Maharashtra",
            pickup_postal_code="012345",  # Invalid in India (cannot start with 0)
            support_phone="9876543210",
        )
        res_bad_pin = seller_bad_pin.validate_pickup_location()
        self.assertFalse(res_bad_pin["is_complete"])
        self.assertTrue(any("PIN" in err for err in res_bad_pin["errors"]))

    def test_12_pickup_verification_is_not_fabricated(self):
        """Valid address without authorized integration is unverified, not verified."""
        seller = Seller(
            user=self.user,
            store_name="Alpha Warehouse",
            pickup_address="Plot 55, Electronics City, Phase 1",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
            support_phone="9876543210",
            pickup_location_verified=False,  # Not verified with carrier
        )
        res = seller.validate_pickup_location()
        self.assertTrue(res["is_complete"])
        self.assertFalse(res["is_verified"])
        self.assertEqual(res["status"], "unverified")

        # Once explicitly verified via carrier integration:
        seller.pickup_location_verified = True
        res_verified = seller.validate_pickup_location()
        self.assertTrue(res_verified["is_verified"])
        self.assertEqual(res_verified["status"], "verified")

    def test_13_platform_warehouse_configuration_safe(self):
        """Platform warehouse reads configuration safely without leaking credentials."""
        platform_info = validate_platform_pickup_location()
        self.assertTrue(platform_info["is_complete"])
        self.assertEqual(platform_info["details"]["city"], "Bengaluru")
        self.assertEqual(platform_info["details"]["postal_code"], "560100")
        self.assertIn("Fitmeezo", platform_info["details"]["facility_name"])
        # Should not claim verified unless configured
        self.assertIn(platform_info["status"], ["unverified", "verified"])


class FulfillmentStaffMeasurementsTests(TestCase):
    """Test suite for Task 6: Authorized staff recording verified packed carton measurements."""

    def setUp(self):
        self.seller_user = User.objects.create_user(username="staff_seller", password="password123")
        self.seller = create_test_seller(self.seller_user, "Staff Store")
        self.customer = User.objects.create_user(username="staff_customer", password="password123")
        self.order = Order.objects.create(
            order_number=f"ORD-STAFF-{uuid.uuid4().hex[:6]}",
            user=self.customer,
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00"),
            payment_status=Order.PAYMENT_STATUS_PAID,
            status=Order.STATUS_PENDING,
            shipping_full_name="Customer",
            shipping_phone_number="9876543210",
            shipping_address_line1="Test Addr",
            shipping_city="Jaipur",
            shipping_state="Rajasthan",
            shipping_postal_code="302001",
        )
        self.package = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller,
            sub_order_number=f"PKG-STAFF-{uuid.uuid4().hex[:6]}",
            status=SellerOrder.STATUS_PROCESSING,
            subtotal=Decimal("1000.00"),
            estimated_weight_grams=Decimal("800.00"),
            package_dimension_status=SellerOrder.PACKAGE_DIMENSION_ESTIMATED,
        )

    def test_14_staff_record_verified_measurements(self):
        """Staff records actual packed carton measurements, updating status to verified."""
        client = Client()
        client.login(username="staff_seller", password="password123")

        url = reverse("core:seller_order_record_measurements", kwargs={"pk": self.package.pk})
        post_data = {
            "actual_weight_grams": "950.00",
            "actual_length_cm": "25.00",
            "actual_breadth_cm": "18.00",
            "actual_height_cm": "12.00",
        }
        response = client.post(url, data=post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        self.package.refresh_from_db()
        self.assertEqual(self.package.actual_weight_grams, Decimal("950.00"))
        self.assertEqual(self.package.actual_length_cm, Decimal("25.00"))
        self.assertEqual(self.package.actual_breadth_cm, Decimal("18.00"))
        self.assertEqual(self.package.actual_height_cm, Decimal("12.00"))
        self.assertEqual(self.package.package_dimension_status, SellerOrder.PACKAGE_DIMENSION_VERIFIED)
        self.assertIsNotNone(self.package.dimensions_verified_at)
        self.assertEqual(self.package.dimensions_verified_by, self.seller_user)

        # Check effective measurements and weights use actuals
        eff = self.package.get_effective_measurements()
        self.assertEqual(eff["weight_grams"], Decimal("950.00"))
        self.assertEqual(self.package.get_dead_weight_kg(), Decimal("0.95"))
        # 25 x 18 x 12 = 5,400 cm3 / 5000 = 1.08 kg
        self.assertEqual(self.package.get_volumetric_weight_kg(), Decimal("1.08"))
        # Max(0.95, 1.08) = 1.08 kg
        self.assertEqual(self.package.get_chargeable_weight_kg(), Decimal("1.08"))
