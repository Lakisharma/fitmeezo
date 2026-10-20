"""
Fitmeezo — Product Size Selection Feature Unit Tests.
Verifies Step 1 model properties and synchronization logic.
"""

from decimal import Decimal
import uuid
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from core.models import (
    Address,
    Cart as CartModel,
    CartItem as CartItemModel,
    Category,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Seller,
    SellerOrder,
)
from core.services import create_order_from_cart, CheckoutValidationError


class ProductSizeSelectionModelTests(TestCase):
    """
    Test suite for size selection model properties and safe synchronization.
    """

    def setUp(self):
        self.category = Category.objects.create(name="Apparel", slug="apparel")
        self.product = Product.objects.create(
            name="Fitmeezo Athletic Performance T-Shirt",
            sku="FTM-TSH-001",
            category=self.category,
            price=Decimal("799.00"),
            stock_quantity=50,
            is_active=True,
        )

    def test_non_size_product_behavior(self):
        """Products without size variants should report has_size_variants as False."""
        self.assertFalse(self.product.has_size_variants)
        self.assertFalse(self.product.is_size_based)
        self.assertEqual(self.product.available_sizes_list, [])
        self.assertEqual(self.product.available_sizes_display, "")

    def test_sync_size_variants_creates_variants(self):
        """sync_size_variants should create ProductVariant records with option_type='Size'."""
        sizes = ["M", "L"]
        stock_map = {"M": 20, "L": 30}
        variants = self.product.sync_size_variants(sizes, stock_map=stock_map)

        self.assertEqual(len(variants), 2)
        self.assertTrue(self.product.has_size_variants)
        self.assertTrue(self.product.is_size_based)
        self.assertEqual(self.product.available_sizes_list, ["M", "L"])
        self.assertEqual(self.product.available_sizes_display, "M, L")

        v_m = ProductVariant.objects.get(product=self.product, option_value="M")
        self.assertEqual(v_m.option_type, "Size")
        self.assertEqual(v_m.stock_quantity, 20)
        self.assertTrue(v_m.is_active)
        self.assertTrue(v_m.sku.startswith("FTM-TSH-001-M"))

        v_l = ProductVariant.objects.get(product=self.product, option_value="L")
        self.assertEqual(v_l.stock_quantity, 30)

    def test_sync_size_variants_deactivates_unselected_without_deleting(self):
        """Removing a size should set is_active=False, preserving database integrity for orders."""
        # Step 1: Create S, M, L
        self.product.sync_size_variants(["S", "M", "L"])
        self.assertEqual(self.product.available_sizes_list, ["S", "M", "L"])

        v_s = ProductVariant.objects.get(product=self.product, option_value="S")
        self.assertTrue(v_s.is_active)

        # Step 2: Update to only M, L
        self.product.sync_size_variants(["M", "L"])
        self.assertEqual(self.product.available_sizes_list, ["M", "L"])

        # S still exists in DB (for order history preservation) but is marked inactive
        v_s.refresh_from_db()
        self.assertFalse(v_s.is_active)

        # Re-adding S reactivates it
        self.product.sync_size_variants(["S", "M", "L"])
        v_s.refresh_from_db()
        self.assertTrue(v_s.is_active)


class AdminPanelSizeManagementTests(TestCase):
    """
    Test suite for Step 2: Admin Panel Size Management.
    Verifies AdminProductForm, size synchronization, individual stock management,
    safety deactivation/reactivation, and custom admin views integration.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from core.admin_forms import AdminProductForm
        User = get_user_model()

        self.category = Category.objects.create(name="Activewear", slug="activewear")
        self.staff_user = User.objects.create_user(
            username="admin_manager",
            email="manager@fitmeezo.in",
            password="StrongPassword123!",
            is_staff=True,
            is_superuser=True,
        )
        self.client = Client()
        self.client.login(username="admin_manager", password="StrongPassword123!")

    def test_condition_1_admin_saves_product_with_sizes(self):
        """1. Admin product ko S, M, L sizes ke saath save kar sakta hai."""
        from core.admin_forms import AdminProductForm

        form_data = {
            "name": "Men Running Tee",
            "slug": "men-running-tee",
            "sku": "RUN-TEE-001",
            "category": self.category.pk,
            "price": "599.00",
            "stock_quantity": "30",
            "has_size_variants": True,
            "available_sizes": "S, M, L",
            "is_active": True,
        }
        form = AdminProductForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()

        self.assertTrue(product.has_size_variants)
        self.assertEqual(product.available_sizes_list, ["S", "M", "L"])
        self.assertEqual(product.available_sizes_display, "S, M, L")
        self.assertEqual(product.variants.filter(option_type__iexact="Size", is_active=True).count(), 3)

    def test_condition_2_stock_per_size_saved_correctly(self):
        """2. Har size ka stock sahi save hota hai."""
        from core.admin_forms import AdminProductForm

        form_data = {
            "name": "Pro Gym Shorts",
            "slug": "pro-gym-shorts",
            "sku": "GYM-SH-001",
            "category": self.category.pk,
            "price": "899.00",
            "stock_quantity": "0",
            "has_size_variants": True,
            "available_sizes": "S, M, L",
            "size_stock_S": "15",
            "size_stock_M": "25",
            "size_stock_L": "35",
            "is_active": True,
        }
        form = AdminProductForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()

        v_s = ProductVariant.objects.get(product=product, option_value="S")
        v_m = ProductVariant.objects.get(product=product, option_value="M")
        v_l = ProductVariant.objects.get(product=product, option_value="L")

        self.assertEqual(v_s.stock_quantity, 15)
        self.assertEqual(v_m.stock_quantity, 25)
        self.assertEqual(v_l.stock_quantity, 35)

        # Total available inventory is sum of variants
        self.assertEqual(product.available_inventory, 75)
        self.assertEqual(product.stock_quantity, 75)

    def test_condition_3_removing_size_sets_variant_inactive_not_deleted(self):
        """3. Size hatane par purana variant inactive hota hai, delete nahi."""
        from core.admin_forms import AdminProductForm

        product = Product.objects.create(
            name="Jogger Pants",
            sku="JOG-001",
            category=self.category,
            price=Decimal("999.00"),
            stock_quantity=60,
            is_active=True,
        )
        product.sync_size_variants(["S", "M", "L"], stock_map={"S": 20, "M": 20, "L": 20})
        self.assertEqual(product.available_sizes_list, ["S", "M", "L"])

        v_s = ProductVariant.objects.get(product=product, option_value="S")
        self.assertTrue(v_s.is_active)

        # Admin edits and submits only M, L
        edit_data = {
            "name": product.name,
            "slug": product.slug,
            "sku": product.sku,
            "category": self.category.pk,
            "price": "999.00",
            "stock_quantity": "40",
            "has_size_variants": True,
            "available_sizes": "M, L",
            "size_stock_M": "20",
            "size_stock_L": "20",
            "is_active": True,
        }
        form = AdminProductForm(data=edit_data, instance=product)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.assertEqual(product.available_sizes_list, ["M", "L"])
        # S still exists in DB (never hard deleted to preserve relational integrity)
        self.assertTrue(ProductVariant.objects.filter(product=product, option_value="S").exists())
        v_s.refresh_from_db()
        self.assertFalse(v_s.is_active)

    def test_condition_4_re_adding_size_reactivates_variant(self):
        """4. Size dobara jodne par variant reactivate hota hai bina duplicate SKU create kiye."""
        from core.admin_forms import AdminProductForm

        product = Product.objects.create(
            name="Compression Top",
            sku="CMP-001",
            category=self.category,
            price=Decimal("1199.00"),
            stock_quantity=50,
            is_active=True,
        )
        product.sync_size_variants(["S", "M"])
        v_s = ProductVariant.objects.get(product=product, option_value="S")
        original_sku = v_s.sku

        # Deactivate S by saving with only M
        edit_data1 = {
            "name": product.name,
            "slug": product.slug,
            "sku": product.sku,
            "category": self.category.pk,
            "price": "1199.00",
            "stock_quantity": "25",
            "has_size_variants": True,
            "available_sizes": "M",
            "size_stock_M": "25",
            "is_active": True,
        }
        form1 = AdminProductForm(data=edit_data1, instance=product)
        self.assertTrue(form1.is_valid())
        form1.save()
        v_s.refresh_from_db()
        self.assertFalse(v_s.is_active)

        # Re-add S
        edit_data2 = {
            "name": product.name,
            "slug": product.slug,
            "sku": product.sku,
            "category": self.category.pk,
            "price": "1199.00",
            "stock_quantity": "55",
            "has_size_variants": True,
            "available_sizes": "S, M",
            "size_stock_S": "30",
            "size_stock_M": "25",
            "is_active": True,
        }
        form2 = AdminProductForm(data=edit_data2, instance=product)
        self.assertTrue(form2.is_valid())
        form2.save()

        v_s.refresh_from_db()
        self.assertTrue(v_s.is_active)
        self.assertEqual(v_s.stock_quantity, 30)
        self.assertEqual(v_s.sku, original_sku)
        # Total S variants is still exactly 1
        self.assertEqual(ProductVariant.objects.filter(product=product, option_value="S").count(), 1)

    def test_condition_5_normal_product_without_size_saves_normally(self):
        """5. Samanya product bina size ke pehle ki tarah save hota hai."""
        from core.admin_forms import AdminProductForm

        form_data = {
            "name": "Standard Water Bottle",
            "slug": "standard-water-bottle",
            "sku": "BOT-001",
            "category": self.category.pk,
            "price": "299.00",
            "stock_quantity": "100",
            "has_size_variants": False,
            "available_sizes": "",
            "is_active": True,
        }
        form = AdminProductForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()

        self.assertFalse(product.has_size_variants)
        self.assertEqual(product.available_sizes_list, [])
        self.assertEqual(product.variants.count(), 0)
        self.assertEqual(product.stock_quantity, 100)
        self.assertEqual(product.available_inventory, 100)

    def test_condition_6_invalid_and_duplicate_sizes_handled(self):
        """6. Invalid ya duplicate sizes sahi tarike se handle hoti hain."""
        from core.admin_forms import AdminProductForm

        # 6a: Duplicate sizes (with spaces and case differences) should be cleanly deduplicated
        form_data_dup = {
            "name": "Training Hoodie",
            "slug": "training-hoodie",
            "sku": "TRN-HD-001",
            "category": self.category.pk,
            "price": "1499.00",
            "stock_quantity": "40",
            "has_size_variants": True,
            "available_sizes": "  S , M , s , M  ,  L  ",
            "size_stock_S": "10",
            "size_stock_M": "15",
            "size_stock_L": "20",
            "is_active": True,
        }
        form_dup = AdminProductForm(data=form_data_dup)
        self.assertTrue(form_dup.is_valid(), form_dup.errors)
        product = form_dup.save()
        self.assertEqual(product.available_sizes_list, ["S", "M", "L"])
        self.assertEqual(product.variants.filter(is_active=True).count(), 3)

        # 6b: Size variants enabled but no size provided -> validation error
        form_data_empty = {
            "name": "Empty Sizes Item",
            "slug": "empty-sizes-item",
            "sku": "EMP-001",
            "category": self.category.pk,
            "price": "499.00",
            "stock_quantity": "10",
            "has_size_variants": True,
            "available_sizes": "   ,   ,  ",
            "is_active": True,
        }
        form_empty = AdminProductForm(data=form_data_empty)
        self.assertFalse(form_empty.is_valid())
        self.assertIn("available_sizes", form_empty.errors)

        # 6c: Negative size stock quantity -> validation error
        form_data_neg = {
            "name": "Negative Stock Item",
            "slug": "neg-stock-item",
            "sku": "NEG-001",
            "category": self.category.pk,
            "price": "499.00",
            "stock_quantity": "10",
            "has_size_variants": True,
            "available_sizes": "S, M",
            "size_stock_S": "-5",
            "is_active": True,
        }
        form_neg = AdminProductForm(data=form_data_neg)
        self.assertFalse(form_neg.is_valid())
        self.assertIn("available_sizes", form_neg.errors)

    def test_custom_admin_views_add_and_edit_integration(self):
        """Integration test: Admin views create and update products with size variants via HTTP POST."""
        from django.urls import reverse

        # POST to add view
        add_url = reverse("custom_admin:product_add")
        resp = self.client.post(add_url, {
            "name": "Yoga Stretch Leggings",
            "slug": "yoga-stretch-leggings",
            "sku": "YOG-LEG-001",
            "category": self.category.pk,
            "price": "1299.00",
            "stock_quantity": "60",
            "has_size_variants": "on",
            "available_sizes": "S, M, L",
            "size_stock_S": "20",
            "size_stock_M": "20",
            "size_stock_L": "20",
            "is_active": "on",
        })
        self.assertEqual(resp.status_code, 302)
        product = Product.objects.get(sku="YOG-LEG-001")
        self.assertTrue(product.has_size_variants)
        self.assertEqual(product.available_sizes_list, ["S", "M", "L"])

        # POST to edit view: reduce to S, M
        edit_url = reverse("custom_admin:product_edit", kwargs={"pk": product.pk})
        resp = self.client.post(edit_url, {
            "name": "Yoga Stretch Leggings Updated",
            "slug": "yoga-stretch-leggings",
            "sku": "YOG-LEG-001",
            "category": self.category.pk,
            "price": "1399.00",
            "stock_quantity": "40",
            "has_size_variants": "on",
            "available_sizes": "S, M",
            "size_stock_S": "25",
            "size_stock_M": "25",
            "is_active": "on",
        })
        self.assertEqual(resp.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.available_sizes_list, ["S", "M"])
        # L was deactivated
        v_l = ProductVariant.objects.get(product=product, option_value="L")
        self.assertFalse(v_l.is_active)


class SellerPanelSizeManagementTests(TestCase):
    """
    Test suite for Step 3: Seller Panel Size Management.
    Verifies SellerProductForm, size synchronization, individual stock management,
    safety deactivation/reactivation, server-side seller ownership verification,
    and HTTP views integration for seller Add/Edit flows.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        User = get_user_model()

        self.category = Category.objects.create(name="Sportswear", slug="sportswear")

        # Seller 1 (Owner)
        self.seller_user1 = User.objects.create_user(
            username="seller_raj",
            email="raj@fitmeezoseller.com",
            password="SecurePass123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.seller_user1,
            store_name="Raj Activewear Store",
            legal_business_name="Raj Activewear Pvt Ltd",
            gstin="27ABCDE1234F1Z5",
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Seller 2 (Other Seller)
        self.seller_user2 = User.objects.create_user(
            username="seller_anita",
            email="anita@fitmeezoseller.com",
            password="SecurePass123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.seller_user2,
            store_name="Anita Fitness Gear",
            legal_business_name="Anita Fitness LLP",
            gstin="29ABCDE1234F1Z6",
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        self.client = Client()

    def test_seller_saves_product_with_sizes(self):
        """Seller can save product with S, M, L sizes."""
        from core.forms import SellerProductForm

        form_data = {
            "name": "DryFit Gym T-Shirt",
            "category": self.category.pk,
            "sku": "SLR-DRY-001",
            "price": "649.00",
            "stock_quantity": "30",
            "has_size_variants": True,
            "available_sizes": "S, M, L",
            "is_active": True,
        }
        form = SellerProductForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save(commit=False)
        product.seller = self.seller1
        product.save()
        form.save_m2m()

        self.assertTrue(product.has_size_variants)
        self.assertEqual(product.available_sizes_list, ["S", "M", "L"])
        self.assertEqual(product.available_sizes_display, "S, M, L")
        self.assertEqual(product.variants.filter(option_type__iexact="Size", is_active=True).count(), 3)
        self.assertEqual(product.seller, self.seller1)

    def test_seller_stock_per_size_saved_correctly(self):
        """Seller enters individual stock per size and total is updated correctly."""
        from core.forms import SellerProductForm

        form_data = {
            "name": "Athletic Track Pants",
            "category": self.category.pk,
            "sku": "SLR-TRK-001",
            "price": "1199.00",
            "stock_quantity": "0",
            "has_size_variants": True,
            "available_sizes": "M, L, XL",
            "size_stock_M": "15",
            "size_stock_L": "25",
            "size_stock_XL": "35",
            "is_active": True,
        }
        form = SellerProductForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save(commit=False)
        product.seller = self.seller1
        product.save()
        form.save_m2m()

        v_m = ProductVariant.objects.get(product=product, option_value="M")
        v_l = ProductVariant.objects.get(product=product, option_value="L")
        v_xl = ProductVariant.objects.get(product=product, option_value="XL")

        self.assertEqual(v_m.stock_quantity, 15)
        self.assertEqual(v_l.stock_quantity, 25)
        self.assertEqual(v_xl.stock_quantity, 35)

        self.assertEqual(product.available_inventory, 75)
        self.assertEqual(product.stock_quantity, 75)

    def test_seller_size_removal_deactivates_and_readdition_reactivates(self):
        """Removing size in seller edit deactivates variant (preserving DB record/order history); re-adding reactivates."""
        from core.forms import SellerProductForm

        product = Product.objects.create(
            name="Thermal Base Layer",
            sku="SLR-THM-001",
            category=self.category,
            seller=self.seller1,
            price=Decimal("899.00"),
            stock_quantity=60,
            is_active=True,
        )
        product.sync_size_variants(["S", "M", "L"], stock_map={"S": 20, "M": 20, "L": 20})
        v_s = ProductVariant.objects.get(product=product, option_value="S")
        original_sku = v_s.sku

        # Edit 1: Remove S
        edit_data1 = {
            "name": product.name,
            "category": self.category.pk,
            "sku": product.sku,
            "price": "899.00",
            "stock_quantity": "40",
            "has_size_variants": True,
            "available_sizes": "M, L",
            "size_stock_M": "20",
            "size_stock_L": "20",
            "is_active": True,
        }
        form1 = SellerProductForm(data=edit_data1, instance=product)
        self.assertTrue(form1.is_valid(), form1.errors)
        p1 = form1.save(commit=False)
        p1.seller = self.seller1
        p1.save()
        form1.save_m2m()

        self.assertEqual(product.available_sizes_list, ["M", "L"])
        v_s.refresh_from_db()
        self.assertFalse(v_s.is_active)
        self.assertTrue(ProductVariant.objects.filter(product=product, option_value="S").exists())

        # Edit 2: Re-add S
        edit_data2 = {
            "name": product.name,
            "category": self.category.pk,
            "sku": product.sku,
            "price": "899.00",
            "stock_quantity": "50",
            "has_size_variants": True,
            "available_sizes": "S, M, L",
            "size_stock_S": "10",
            "size_stock_M": "20",
            "size_stock_L": "20",
            "is_active": True,
        }
        form2 = SellerProductForm(data=edit_data2, instance=product)
        self.assertTrue(form2.is_valid(), form2.errors)
        p2 = form2.save(commit=False)
        p2.seller = self.seller1
        p2.save()
        form2.save_m2m()

        v_s.refresh_from_db()
        self.assertTrue(v_s.is_active)
        self.assertEqual(v_s.stock_quantity, 10)
        self.assertEqual(v_s.sku, original_sku)
        self.assertEqual(ProductVariant.objects.filter(product=product, option_value="S").count(), 1)

    def test_seller_normal_product_without_size_saves_normally(self):
        """Normal product without sizes behaves normally and preserves standalone stock."""
        from core.forms import SellerProductForm

        form_data = {
            "name": "Stainless Steel Shaker",
            "category": self.category.pk,
            "sku": "SLR-SHK-001",
            "price": "499.00",
            "stock_quantity": "120",
            "has_size_variants": False,
            "available_sizes": "",
            "is_active": True,
        }
        form = SellerProductForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save(commit=False)
        product.seller = self.seller1
        product.save()
        form.save_m2m()

        self.assertFalse(product.has_size_variants)
        self.assertEqual(product.available_sizes_list, [])
        self.assertEqual(product.variants.count(), 0)
        self.assertEqual(product.stock_quantity, 120)
        self.assertEqual(product.available_inventory, 120)

    def test_seller_invalid_and_duplicate_sizes_handled(self):
        """Invalid sizes (empty when enabled, negative stock) rejected; duplicates deduplicated."""
        from core.forms import SellerProductForm

        # Duplicate sizes
        dup_data = {
            "name": "Compression Sleeves",
            "category": self.category.pk,
            "sku": "SLR-SLV-001",
            "price": "399.00",
            "stock_quantity": "30",
            "has_size_variants": True,
            "available_sizes": " S , M , s , m , L ",
            "size_stock_S": "10",
            "size_stock_M": "10",
            "size_stock_L": "10",
            "is_active": True,
        }
        form_dup = SellerProductForm(data=dup_data)
        self.assertTrue(form_dup.is_valid(), form_dup.errors)
        p = form_dup.save(commit=False)
        p.seller = self.seller1
        p.save()
        form_dup.save_m2m()
        self.assertEqual(p.available_sizes_list, ["S", "M", "L"])

        # Empty sizes
        empty_data = {
            "name": "Empty Size Item",
            "category": self.category.pk,
            "sku": "SLR-EMP-001",
            "price": "399.00",
            "stock_quantity": "10",
            "has_size_variants": True,
            "available_sizes": "",
            "is_active": True,
        }
        form_empty = SellerProductForm(data=empty_data)
        self.assertFalse(form_empty.is_valid())
        self.assertIn("available_sizes", form_empty.errors)

        # Negative stock
        neg_data = {
            "name": "Neg Stock Item",
            "category": self.category.pk,
            "sku": "SLR-NEG-001",
            "price": "399.00",
            "stock_quantity": "10",
            "has_size_variants": True,
            "available_sizes": "S, M",
            "size_stock_S": "-10",
            "is_active": True,
        }
        form_neg = SellerProductForm(data=neg_data)
        self.assertFalse(form_neg.is_valid())
        self.assertIn("available_sizes", form_neg.errors)

    def test_seller_ownership_enforcement_cross_seller_access_blocked(self):
        """Server-side seller ownership: Seller 2 cannot access or edit Seller 1's product/variants (returns 404)."""
        product1 = Product.objects.create(
            name="Raj Exclusive Hoodie",
            sku="RAJ-HD-001",
            category=self.category,
            seller=self.seller1,
            price=Decimal("1999.00"),
            stock_quantity=50,
            is_active=True,
        )
        product1.sync_size_variants(["M", "L"])

        # Login as Seller 2
        self.client.login(username="seller_anita", password="SecurePass123!")

        # Seller 2 attempts GET on Seller 1's product edit view
        edit_url = reverse("core:seller_product_edit", kwargs={"pk": product1.pk})
        resp = self.client.get(edit_url)
        self.assertEqual(resp.status_code, 404)

        # Seller 2 attempts POST to alter Seller 1's product and size variants
        post_resp = self.client.post(edit_url, {
            "name": "Hacked Hoodie",
            "category": self.category.pk,
            "sku": "RAJ-HD-001",
            "price": "100.00",
            "stock_quantity": "10",
            "has_size_variants": "on",
            "available_sizes": "XXL",
            "size_stock_XXL": "5",
            "is_active": "on",
        })
        self.assertEqual(post_resp.status_code, 404)

        # Verify Seller 1's product and variants are completely untouched
        product1.refresh_from_db()
        self.assertEqual(product1.name, "Raj Exclusive Hoodie")
        self.assertEqual(product1.price, Decimal("1999.00"))
        self.assertEqual(product1.available_sizes_list, ["M", "L"])
        self.assertEqual(product1.seller, self.seller1)

    def test_seller_product_add_and_edit_views_http_integration(self):
        """Integration test: Authenticated seller adds and edits size variants through seller panel HTTP views."""
        self.client.login(username="seller_raj", password="SecurePass123!")

        # 1. Seller adds product with sizes via seller_product_add view
        add_url = reverse("core:seller_product_add")
        resp = self.client.post(add_url, {
            "name": "Seller Windbreaker Jacket",
            "category": self.category.pk,
            "sku": "SLR-WND-001",
            "price": "2499.00",
            "stock_quantity": "60",
            "has_size_variants": "on",
            "available_sizes": "M, L, XL",
            "size_stock_M": "20",
            "size_stock_L": "20",
            "size_stock_XL": "20",
            "is_active": "on",
        })
        self.assertEqual(resp.status_code, 302)
        product = Product.objects.get(sku="SLR-WND-001")
        self.assertEqual(product.seller, self.seller1)
        self.assertTrue(product.has_size_variants)
        self.assertEqual(product.available_sizes_list, ["M", "L", "XL"])

        # 2. Seller edits product via seller_product_edit view: change sizes to L, XL, XXL
        edit_url = reverse("core:seller_product_edit", kwargs={"pk": product.pk})
        resp = self.client.post(edit_url, {
            "name": "Seller Windbreaker Jacket V2",
            "category": self.category.pk,
            "sku": "SLR-WND-001",
            "price": "2499.00",
            "stock_quantity": "75",
            "has_size_variants": "on",
            "available_sizes": "L, XL, XXL",
            "size_stock_L": "25",
            "size_stock_XL": "25",
            "size_stock_XXL": "25",
            "is_active": "on",
        })
        self.assertEqual(resp.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.available_sizes_list, ["L", "XL", "XXL"])
        # M is now inactive
        v_m = ProductVariant.objects.get(product=product, option_value="M")
        self.assertFalse(v_m.is_active)
        # XXL is newly active
        v_xxl = ProductVariant.objects.get(product=product, option_value="XXL")
        self.assertTrue(v_xxl.is_active)
        self.assertEqual(v_xxl.stock_quantity, 25)


class CustomerStorefrontSizeSelectionTests(TestCase):
    """
    Test suite for Step 4: Customer Storefront Size Selection.
    Verifies:
    1. Available sizes display correctly as selectable buttons/pills.
    2. Out-of-stock sizes are disabled and cannot be purchased.
    3. Selecting size submits correct variant ID and adds it to cart.
    4. Size-based product cannot be submitted without selecting a size.
    5. Invalid, inactive, and other product's variants are strictly rejected.
    6. Normal products work as before without size selection.
    """

    def setUp(self):
        from django.test import Client
        self.client = Client()
        self.category = Category.objects.create(name="Footwear", slug="footwear")

        # Size-based product
        self.size_product = Product.objects.create(
            name="Fitmeezo Aero Runner Shoes",
            slug="fitmeezo-aero-runner-shoes",
            sku="AERO-SHOE-001",
            category=self.category,
            price=Decimal("2999.00"),
            stock_quantity=25,
            is_active=True,
            is_approved=True,
        )
        self.size_product.sync_size_variants(
            ["S", "M", "L"],
            stock_map={"S": 10, "M": 15, "L": 0}  # L is out of stock
        )

        # Inactive variant for the size product (e.g. XL was deactivated)
        self.v_xl_inactive = ProductVariant.objects.create(
            product=self.size_product,
            option_type="Size",
            option_value="XL",
            sku="AERO-SHOE-001-XL",
            stock_quantity=10,
            is_active=False,
        )

        # Another product with its own variant
        self.other_product = Product.objects.create(
            name="Fitmeezo Classic Cap",
            slug="fitmeezo-classic-cap",
            sku="CAP-001",
            category=self.category,
            price=Decimal("499.00"),
            stock_quantity=30,
            is_active=True,
            is_approved=True,
        )
        self.other_variant = ProductVariant.objects.create(
            product=self.other_product,
            option_type="Size",
            option_value="Free-Size",
            sku="CAP-001-FS",
            stock_quantity=30,
            is_active=True,
        )

        # Normal product without size variants
        self.normal_product = Product.objects.create(
            name="Fitmeezo Stainless Steel Water Bottle",
            slug="fitmeezo-steel-water-bottle",
            sku="BTL-001",
            category=self.category,
            price=Decimal("699.00"),
            stock_quantity=50,
            is_active=True,
            is_approved=True,
        )

    def test_condition_1_available_sizes_display_properly(self):
        """1. Available sizes sahi dikhai deti hain: Active sizes (S, M, L) dikhti hain, inactive (XL) nahi."""
        detail_url = reverse("core:product_detail", kwargs={"slug": self.size_product.slug})
        resp = self.client.get(detail_url)

        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        # Active sizes S, M, L must be present in HTML
        self.assertIn("Select Size:", content)
        self.assertIn(">S<", content)
        self.assertIn(">M<", content)
        self.assertIn(">L<", content)

        # Inactive variant XL must NOT be in the rendered HTML variant pills
        self.assertNotIn("var_opt_" + str(self.v_xl_inactive.id), content)

        # Verify context properties
        self.assertTrue(resp.context["product"].has_size_variants)
        self.assertEqual(resp.context["product"].available_sizes_list, ["S", "M", "L"])
        self.assertTrue(resp.context["has_in_stock_size"])

    def test_condition_2_out_of_stock_sizes_disabled(self):
        """2. Out-of-stock sizes disabled hain: Stock 0 wali size (L) disabled aur unclickable dikhti hai."""
        detail_url = reverse("core:product_detail", kwargs={"slug": self.size_product.slug})
        resp = self.client.get(detail_url)
        content = resp.content.decode("utf-8")

        v_l = ProductVariant.objects.get(product=self.size_product, option_value="L")
        self.assertEqual(v_l.stock_quantity, 0)
        self.assertFalse(v_l.is_in_stock)

        # Label for variant L has is-out-of-stock and is-disabled classes
        expected_label_snippet = f'id="pill_label_{v_l.id}"'
        self.assertIn(expected_label_snippet, content)
        self.assertIn("is-out-of-stock is-disabled", content)
        self.assertIn("Out of stock", content)

        # Radio input for variant L is disabled
        input_snippet = f'id="var_opt_{v_l.id}"'
        self.assertIn(input_snippet, content)

        # Server-side validation: Out-of-stock variant cannot be submitted via cart_add
        cart_add_url = reverse("core:cart_add")
        ajax_resp = self.client.post(
            cart_add_url,
            {"product_id": self.size_product.id, "variant_id": v_l.id, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(ajax_resp.status_code, 400)
        data = ajax_resp.json()
        self.assertFalse(data["success"])
        self.assertIn("out of stock", data["message"].lower())

    def test_condition_3_selecting_size_submits_correct_variant_id(self):
        """3. Size chunne par sahi variant ID submit hoti hai aur cart me add hoti hai."""
        v_m = ProductVariant.objects.get(product=self.size_product, option_value="M")
        self.assertEqual(v_m.stock_quantity, 15)

        cart_add_url = reverse("core:cart_add")
        resp = self.client.post(
            cart_add_url,
            {"product_id": self.size_product.id, "variant_id": v_m.id, "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])

        # Check cart in session contains this exact variant
        session = self.client.session
        from core.cart import SESSION_CART_KEY, make_cart_key
        cart_key = make_cart_key(self.size_product.id, v_m.id)
        cart_data = session.get(SESSION_CART_KEY, {})
        self.assertIn(cart_key, cart_data)
        self.assertEqual(cart_data[cart_key]["quantity"], 2)
        self.assertEqual(cart_data[cart_key]["variant_id"], v_m.id)

    def test_condition_4_size_based_product_cannot_be_submitted_without_selecting_size(self):
        """4. Size chune bina size-based product submit nahi hota (rejects empty variant_id)."""
        cart_add_url = reverse("core:cart_add")

        # 4a: AJAX POST without variant_id
        ajax_resp = self.client.post(
            cart_add_url,
            {"product_id": self.size_product.id, "variant_id": "", "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(ajax_resp.status_code, 400)
        data = ajax_resp.json()
        self.assertFalse(data["success"])
        self.assertIn("select a size", data["message"].lower())

        # 4b: Standard Form POST without variant_id redirects with error
        form_resp = self.client.post(
            cart_add_url,
            {"product_id": self.size_product.id, "variant_id": "", "quantity": 1},
        )
        self.assertEqual(form_resp.status_code, 302)
        self.assertRedirects(form_resp, self.size_product.get_absolute_url())

    def test_condition_5_invalid_inactive_and_other_product_variant_rejected(self):
        """5. Invalid, inactive aur doosre product ki variant reject hoti hain."""
        cart_add_url = reverse("core:cart_add")

        # 5a: Variant belonging to another product
        resp_other = self.client.post(
            cart_add_url,
            {"product_id": self.size_product.id, "variant_id": self.other_variant.id, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_other.status_code, 400)
        data_other = resp_other.json()
        self.assertFalse(data_other["success"])
        self.assertIn("invalid", data_other["message"].lower())

        # 5b: Inactive variant of the same product
        resp_inactive = self.client.post(
            cart_add_url,
            {"product_id": self.size_product.id, "variant_id": self.v_xl_inactive.id, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_inactive.status_code, 400)
        data_inactive = resp_inactive.json()
        self.assertFalse(data_inactive["success"])
        self.assertIn("inactive", data_inactive["message"].lower())

        # 5c: Non-existent / invalid variant ID
        resp_bad_id = self.client.post(
            cart_add_url,
            {"product_id": self.size_product.id, "variant_id": 999999, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_bad_id.status_code, 400)
        data_bad = resp_bad_id.json()
        self.assertFalse(data_bad["success"])
        self.assertIn("invalid", data_bad["message"].lower())

    def test_condition_6_normal_product_works_as_before(self):
        """6. Samanya product pehle ki tarah bina variant ke kaam karta hai."""
        # 6a: Detail page does NOT render size variant selector
        detail_url = reverse("core:product_detail", kwargs={"slug": self.normal_product.slug})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        self.assertNotIn("Select Size:", content)
        self.assertNotIn("fmVariantSelectors", content)
        self.assertIn("In Stock (50 units available)", content)

        # 6b: Adding normal product to cart without variant_id succeeds seamlessly
        cart_add_url = reverse("core:cart_add")
        add_resp = self.client.post(
            cart_add_url,
            {"product_id": self.normal_product.id, "variant_id": "", "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(add_resp.status_code, 200)
        data = add_resp.json()
        self.assertTrue(data["success"])

        # Check cart item has variant_id as None
        session = self.client.session
        from core.cart import SESSION_CART_KEY, make_cart_key
        cart_key = make_cart_key(self.normal_product.id, None)
        cart_data = session.get(SESSION_CART_KEY, {})
        self.assertIn(cart_key, cart_data)
        self.assertIsNone(cart_data[cart_key]["variant_id"])
        self.assertEqual(cart_data[cart_key]["quantity"], 1)

    def test_size_based_product_all_sizes_out_of_stock_display(self):
        """Size-based product with all sizes having 0 stock shows Out of Stock badge and disabled Add to Cart."""
        sold_out_product = Product.objects.create(
            name="Fitmeezo Limited Edition Jacket",
            slug="fitmeezo-limited-edition-jacket",
            sku="LTD-JKT-001",
            category=self.category,
            price=Decimal("4999.00"),
            stock_quantity=0,
            is_active=True,
            is_approved=True,
        )
        sold_out_product.sync_size_variants(["M", "L"], stock_map={"M": 0, "L": 0})

        detail_url = reverse("core:product_detail", kwargs={"slug": sold_out_product.slug})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        self.assertIn("Currently Out of Stock", content)
        self.assertIn("Out of Stock", content)
        self.assertFalse(resp.context["has_in_stock_size"])

    def test_size_based_product_no_active_sizes_shows_unavailable_notice(self):
        """Size-based product where all size variants are deactivated shows clear unavailable notice."""
        no_active_sizes_product = Product.objects.create(
            name="Fitmeezo Archived Hoodie",
            slug="fitmeezo-archived-hoodie",
            sku="ARC-HD-001",
            category=self.category,
            price=Decimal("1599.00"),
            stock_quantity=0,
            is_active=True,
            is_approved=True,
        )
        # Create inactive size variants
        ProductVariant.objects.create(
            product=no_active_sizes_product,
            option_type="Size",
            option_value="M",
            sku="ARC-HD-001-M",
            stock_quantity=10,
            is_active=False,
        )

        detail_url = reverse("core:product_detail", kwargs={"slug": no_active_sizes_product.slug})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        self.assertIn("Sizes are currently unavailable for this product.", content)
        self.assertTrue(resp.context["has_configured_sizes"])
        self.assertFalse(resp.context["product"].has_size_variants)


class CartCheckoutOrderSizeVariantIntegrationTests(TestCase):
    """
    Test suite for Step 5 & 6: Size Variant Integration in Cart, Checkout, and Orders.
    Validates:
    1. Size M and Size L of the same product become distinct cart items.
    2. Cart renders correct size label (Size: M) and quantity.
    3. Quantity exceeding variant stock limit is rejected (add and update).
    4. Checkout creates order with correct variant, size, and deducts variant stock.
    5. Order item size, label, and SKU snapshots are preserved immutably.
    6. Order details and Order history clearly display purchased size.
    7. Legacy non-variant orders render cleanly without error.
    8. Inactive or invalid variants are rejected during checkout.
    9. Normal non-size products and payment flows work regression-free.
    10. Cross-customer cart and order access is strictly isolated.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from core.models import Address, Order, OrderItem, Cart as CartModel, CartItem as CartItemModel
        from core.services import create_order_from_cart, CheckoutValidationError
        User = get_user_model()

        self.category = Category.objects.create(name="Apparel", slug="apparel")

        # Customer A
        self.user_a = User.objects.create_user(
            username="customer_a",
            email="customera@fitmeezo.in",
            password="StrongPassword123!",
        )
        self.addr_a = Address.objects.create(
            user=self.user_a,
            full_name="Customer A Sharma",
            phone_number="9876543210",
            address_line1="123 Alpha Road, Sector 5",
            city="Jaipur",
            state="Rajasthan",
            postal_code="302001",
            is_default=True,
        )

        # Customer B
        self.user_b = User.objects.create_user(
            username="customer_b",
            email="customerb@fitmeezo.in",
            password="StrongPassword123!",
        )
        self.addr_b = Address.objects.create(
            user=self.user_b,
            full_name="Customer B Verma",
            phone_number="9876543211",
            address_line1="456 Beta Street, Gomti Nagar",
            city="Lucknow",
            state="Uttar Pradesh",
            postal_code="226010",
            is_default=True,
        )

        # Size-based product
        self.tshirt = Product.objects.create(
            name="Fitmeezo Pure Cotton Crewneck T-Shirt",
            slug="fitmeezo-cotton-crewneck-tshirt",
            sku="FTM-TSH-COT",
            category=self.category,
            price=Decimal("899.00"),
            stock_quantity=15,
            is_active=True,
            is_approved=True,
        )
        self.tshirt.sync_size_variants(["M", "L"], stock_map={"M": 5, "L": 10})
        self.var_m = ProductVariant.objects.get(product=self.tshirt, option_value="M")
        self.var_l = ProductVariant.objects.get(product=self.tshirt, option_value="L")

        # Normal non-size product
        self.mug = Product.objects.create(
            name="Fitmeezo Insulated Travel Mug",
            slug="fitmeezo-travel-mug",
            sku="FTM-MUG-001",
            category=self.category,
            price=Decimal("499.00"),
            stock_quantity=50,
            is_active=True,
            is_approved=True,
        )

        self.client_a = Client()
        self.client_b = Client()

    def test_01_distinct_cart_items_for_sizes(self):
        """1. Size M aur Size L ek hi product ke alag cart items bante hain."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")

        # Add Size M (qty: 2)
        resp_m = self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_m.status_code, 200)

        # Add Size L (qty: 3)
        resp_l = self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_l.id, "quantity": 3},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_l.status_code, 200)

        # Verify DB Cart has 2 distinct CartItem rows for this user
        cart = CartModel.objects.get(user=self.user_a)
        items = list(cart.items.order_by("id"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].product_id, self.tshirt.id)
        self.assertEqual(items[0].variant_id, self.var_m.id)
        self.assertEqual(items[0].quantity, 2)

        self.assertEqual(items[1].product_id, self.tshirt.id)
        self.assertEqual(items[1].variant_id, self.var_l.id)
        self.assertEqual(items[1].quantity, 3)

        # Re-adding Size M (qty: 1) increments existing row to 3
        resp_m2 = self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_m2.status_code, 200)
        items_after = list(cart.items.order_by("id"))
        self.assertEqual(len(items_after), 2)
        self.assertEqual(items_after[0].quantity, 3)

    def test_02_cart_renders_correct_size_and_quantity(self):
        """2. Cart me sahi size aur quantity dikhayi deti hai."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")
        self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        resp = self.client_a.get(reverse("core:cart_detail"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        self.assertIn("Fitmeezo Pure Cotton Crewneck T-Shirt", content)
        self.assertIn("Size:", content)
        self.assertIn("M", content)
        self.assertIn('value="2"', content)

    def test_03_stock_limit_exceeded_rejected(self):
        """3. Stock limit se adhik quantity reject hoti hai."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")

        # Variant M has stock 5. Attempt adding 6 -> rejected
        resp_exceed = self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 6},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_exceed.status_code, 400)
        data = resp_exceed.json()
        self.assertFalse(data["success"])
        self.assertIn("exceeds available stock", data["message"].lower())

        # Adding 4 units succeeds
        resp_ok = self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 4},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_ok.status_code, 200)

        # Adding 2 more (total 6 > 5) -> rejected
        resp_more = self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_more.status_code, 400)

        # Updating cart item beyond stock limit -> rejected
        item_key = f"{self.tshirt.id}_{self.var_m.id}"
        resp_update = self.client_a.post(
            reverse("core:cart_update"),
            {"item_key": item_key, "quantity": 6},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp_update.status_code, 400)
        data_up = resp_update.json()
        self.assertFalse(data_up["success"])
        self.assertIn("cannot update quantity", data_up["message"].lower())

    def test_04_checkout_creates_order_with_variant_and_deducts_stock(self):
        """4. Checkout sahi variant aur size ke sath order banata hai aur inventory deduct karta hai."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")
        self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        from core.cart import Cart
        from core.services import create_order_from_cart

        # Initial variant stock = 5, total product stock = 15
        self.assertEqual(self.var_m.stock_quantity, 5)

        # Execute checkout
        resp = self.client_a.post(reverse("core:checkout"), {
            "address_id": self.addr_a.id,
            "payment_method": "cod",
            "customer_notes": "Please deliver in afternoon",
        })
        self.assertEqual(resp.status_code, 302)

        order = Order.objects.filter(user=self.user_a).latest("id")
        self.assertEqual(order.items.count(), 1)
        item = order.items.first()
        self.assertEqual(item.product, self.tshirt)
        self.assertEqual(item.variant, self.var_m)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.variant_label, "Size: M")
        self.assertEqual(item.sku, self.var_m.sku)

        # Verify stock was deducted safely
        self.var_m.refresh_from_db()
        self.assertEqual(self.var_m.stock_quantity, 3)

        self.tshirt.refresh_from_db()
        self.assertEqual(self.tshirt.stock_quantity, 13)

    def test_05_order_item_snapshots_preserved(self):
        """5. Order item me size/SKU snapshot surakshit rehta hai."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")
        self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.client_a.post(reverse("core:checkout"), {
            "address_id": self.addr_a.id,
            "payment_method": "cod",
        })

        order = Order.objects.filter(user=self.user_a).latest("id")
        item = order.items.first()
        orig_name = item.product_name
        orig_sku = item.sku
        orig_label = item.variant_label
        self.assertEqual(orig_label, "Size: M")
        self.assertEqual(orig_sku, self.var_m.sku)

        # Simulate seller modifying product name and SKU later
        self.tshirt.name = "Modified T-Shirt Name 2027"
        self.tshirt.sku = "NEW-TSH-SKU"
        self.tshirt.save()

        self.var_m.sku = "NEW-VARIANT-SKU"
        self.var_m.save()

        # OrderItem snapshot remains unmodified
        item.refresh_from_db()
        self.assertEqual(item.product_name, orig_name)
        self.assertEqual(item.sku, orig_sku)
        self.assertEqual(item.variant_label, "Size: M")

    def test_06_order_details_and_history_show_size(self):
        """6. Order details aur history me size dikhayi deti hai."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")
        self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.client_a.post(reverse("core:checkout"), {
            "address_id": self.addr_a.id,
            "payment_method": "cod",
        })
        order = Order.objects.filter(user=self.user_a).latest("id")

        # 6a: Order confirmation page
        resp_confirm = self.client_a.get(reverse("core:order_confirmation", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp_confirm.status_code, 200)
        self.assertIn("Size: M", resp_confirm.content.decode("utf-8"))

        # 6b: Order detail page
        resp_detail = self.client_a.get(reverse("core:order_detail", kwargs={"order_number": order.order_number}))
        self.assertEqual(resp_detail.status_code, 200)
        self.assertIn("Size: M", resp_detail.content.decode("utf-8"))

        # 6c: Account order history page
        resp_orders = self.client_a.get(reverse("core:account_orders"))
        self.assertEqual(resp_orders.status_code, 200)
        self.assertIn("Size: M", resp_orders.content.decode("utf-8"))

    def test_07_legacy_non_variant_orders_render_cleanly(self):
        """7. Purane non-variant orders bina error ke render hote hain."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")

        # Create a legacy order directly with empty variant_label and None variant
        import uuid
        legacy_order = Order.objects.create(
            order_number=f"FM-LEGACY-{uuid.uuid4().hex[:6].upper()}",
            user=self.user_a,
            shipping_full_name="Customer A Sharma",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Alpha Road",
            shipping_city="Jaipur",
            shipping_state="Rajasthan",
            shipping_postal_code="302001",
            subtotal=Decimal("499.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("499.00"),
            currency="INR",
            status=Order.STATUS_CONFIRMED,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )
        OrderItem.objects.create(
            order=legacy_order,
            product=self.mug,
            variant=None,
            product_name="Legacy Fitmeezo Item",
            variant_label="",
            sku="LEGACY-SKU-001",
            unit_price=Decimal("499.00"),
            quantity=1,
            line_total=Decimal("499.00"),
        )

        resp_detail = self.client_a.get(reverse("core:order_detail", kwargs={"order_number": legacy_order.order_number}))
        self.assertEqual(resp_detail.status_code, 200)
        content_detail = resp_detail.content.decode("utf-8")
        self.assertIn("Legacy Fitmeezo Item", content_detail)

        resp_orders = self.client_a.get(reverse("core:account_orders"))
        self.assertEqual(resp_orders.status_code, 200)
        content_orders = resp_orders.content.decode("utf-8")
        self.assertIn("Legacy Fitmeezo Item", content_orders)

    def test_08_inactive_or_invalid_variants_rejected_in_checkout(self):
        """8. Inactive ya invalid variants checkout me reject hoti hain."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")
        self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # Deactivate variant M
        self.var_m.is_active = False
        self.var_m.save()

        # Checkout attempt should fail and redirect to cart
        resp = self.client_a.post(reverse("core:checkout"), {
            "address_id": self.addr_a.id,
            "payment_method": "cod",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("core:cart_detail"), resp.url)

        # Verify no order was created
        self.assertFalse(Order.objects.filter(user=self.user_a).exists())

    def test_09_payment_flow_and_normal_products_regression_free(self):
        """9. Payment flow aur normal non-size products regression-free hain."""
        self.client_a.login(username="customer_a", password="StrongPassword123!")

        # Normal product checkout
        self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.mug.id, "variant_id": "", "quantity": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        resp = self.client_a.post(reverse("core:checkout"), {
            "address_id": self.addr_a.id,
            "payment_method": "cod",
        })
        self.assertEqual(resp.status_code, 302)

        order = Order.objects.filter(user=self.user_a).latest("id")
        item = order.items.first()
        self.assertEqual(item.product, self.mug)
        self.assertIsNone(item.variant)
        self.assertEqual(item.variant_label, "")
        self.assertEqual(item.quantity, 2)

        # Mug stock deducted from 50 to 48
        self.mug.refresh_from_db()
        self.assertEqual(self.mug.stock_quantity, 48)

    def test_10_cross_customer_isolation(self):
        """10. Dusre grahak ka cart/order access blocked hai."""
        # Customer A places an order
        self.client_a.login(username="customer_a", password="StrongPassword123!")
        self.client_a.post(
            reverse("core:cart_add"),
            {"product_id": self.tshirt.id, "variant_id": self.var_m.id, "quantity": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.client_a.post(reverse("core:checkout"), {
            "address_id": self.addr_a.id,
            "payment_method": "cod",
        })
        order_a = Order.objects.filter(user=self.user_a).latest("id")

        # Customer B logs in and tries to access Customer A's order detail
        self.client_b.login(username="customer_b", password="StrongPassword123!")
        resp_unauth = self.client_b.get(reverse("core:order_detail", kwargs={"order_number": order_a.order_number}))
        self.assertEqual(resp_unauth.status_code, 404)

        # Customer B's cart does not have Customer A's items
        resp_cart_b = self.client_b.get(reverse("core:cart_detail"))
        self.assertEqual(resp_cart_b.status_code, 200)
        self.assertIn("Your Shopping Cart is Empty", resp_cart_b.content.decode("utf-8"))



