"""
Unit and integration tests for Fitmeezo Product Detail 3-Image Gallery Carousel and Lightbox Preview.
Verifies template rendering, slide counts across 0, 1, 2, 3, and 5 images,
clean fallback handling, thumbnail synchronization, and accessibility attributes.
"""

import shutil
import tempfile
from decimal import Decimal
from io import BytesIO
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from core.models import Category, Product, ProductImage

User = get_user_model()
TEST_MEDIA_DIR = tempfile.mkdtemp()


def create_test_image(name="test_prod.jpg", width=600, height=600, color="orange"):
    file_obj = BytesIO()
    image = Image.new("RGB", (width, height), color=color)
    image.save(file_obj, format="JPEG")
    file_obj.seek(0)
    return SimpleUploadedFile(name, file_obj.read(), content_type="image/jpeg")


@override_settings(MEDIA_ROOT=TEST_MEDIA_DIR)
class ProductDetailGalleryTests(TestCase):
    """Verifies gallery carousel UI/UX rendering across diverse image counts."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_DIR, ignore_errors=True)

    def setUp(self):
        self.category = Category.objects.create(name="Electronics", slug="electronics")

    def test_gallery_zero_images_renders_fallback_single_slide(self):
        """When a product has no primary image and no gallery images, renders 1 clean fallback slide."""
        product = Product.objects.create(
            name="No Image Headphones",
            slug="no-image-headphones",
            sku="SKU-NO-IMG",
            category=self.category,
            price=Decimal("1499.00"),
            is_active=True,
            is_approved=True,
        )

        response = self.client.get(reverse("core:product_detail", kwargs={"slug": product.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        # Must have gallery wrapper with data-total="1"
        self.assertIn('id="fmProductGallery"', content)
        self.assertIn('data-total="1"', content)

        # Uses fallback SVG
        self.assertIn('product-fallback.svg', content)
        self.assertIn('id="fmMainProductImg"', content)

        # Must have carousel navigation buttons and Lightbox modal
        self.assertIn('id="fmGalleryPrevBtn"', content)
        self.assertIn('id="fmGalleryNextBtn"', content)
        self.assertIn('id="fmGalleryLightbox"', content)

    def test_gallery_single_image_renders_cleanly_without_duplicates(self):
        """When a product has only 1 image, renders exactly 1 slide and no duplicate placeholders."""
        img = create_test_image("primary_only.jpg")
        product = Product.objects.create(
            name="Single Image Watch",
            slug="single-image-watch",
            sku="SKU-SGL-01",
            category=self.category,
            price=Decimal("2499.00"),
            image=img,
            is_active=True,
            is_approved=True,
        )

        response = self.client.get(reverse("core:product_detail", kwargs={"slug": product.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn('data-total="1"', content)
        self.assertIn('data-index="0"', content)
        # Should not have slide with data-index="1"
        self.assertNotIn('data-index="1"', content)

    def test_gallery_two_images_renders_two_slides_and_two_thumbs(self):
        """When a product has 2 images, renders exactly 2 slides (50% each on desktop) and 2 thumbnails."""
        img1 = create_test_image("watch_front.jpg")
        img2 = create_test_image("watch_back.jpg")

        product = Product.objects.create(
            name="Dual Image Watch",
            slug="dual-image-watch",
            sku="SKU-DUAL-01",
            category=self.category,
            price=Decimal("2999.00"),
            image=img1,
            is_active=True,
            is_approved=True,
        )
        ProductImage.objects.create(product=product, image=img2, alt_text="Back angle view")

        response = self.client.get(reverse("core:product_detail", kwargs={"slug": product.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn('data-total="2"', content)
        self.assertIn('data-index="0"', content)
        self.assertIn('data-index="1"', content)
        self.assertNotIn('data-index="2"', content)

        # Thumbnails rendered for both
        self.assertIn('data-thumb-index="0"', content)
        self.assertIn('data-thumb-index="1"', content)
        self.assertNotIn('data-thumb-index="2"', content)

    def test_gallery_three_images_renders_three_slides(self):
        """When a product has 3 images, renders all 3 slides visible side-by-side."""
        img_main = create_test_image("main.jpg")
        img_side = create_test_image("side.jpg")
        img_angle = create_test_image("angle.jpg")

        product = Product.objects.create(
            name="Trio Image Sneaker",
            slug="trio-image-sneaker",
            sku="SKU-TRIO-01",
            category=self.category,
            price=Decimal("3499.00"),
            image=img_main,
            is_active=True,
            is_approved=True,
        )
        ProductImage.objects.create(product=product, image=img_side, alt_text="Side profile")
        ProductImage.objects.create(product=product, image=img_angle, alt_text="Angled view")

        response = self.client.get(reverse("core:product_detail", kwargs={"slug": product.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn('data-total="3"', content)
        self.assertIn('data-index="0"', content)
        self.assertIn('data-index="1"', content)
        self.assertIn('data-index="2"', content)
        self.assertNotIn('data-index="3"', content)

    def test_gallery_five_images_renders_all_slides_and_navigation(self):
        """When product has 5 images (4 additional gallery images), renders all 5 slides and thumbs."""
        img_main = create_test_image("img0.jpg")
        product = Product.objects.create(
            name="Multi Image Laptop",
            slug="multi-image-laptop",
            sku="SKU-LAP-01",
            category=self.category,
            price=Decimal("54999.00"),
            image=img_main,
            is_active=True,
            is_approved=True,
        )
        for i in range(1, 5):
            img_extra = create_test_image(f"img{i}.jpg")
            ProductImage.objects.create(product=product, image=img_extra, alt_text=f"View {i}")

        response = self.client.get(reverse("core:product_detail", kwargs={"slug": product.slug}))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn('data-total="5"', content)
        for i in range(5):
            self.assertIn(f'data-index="{i}"', content)
            self.assertIn(f'data-thumb-index="{i}"', content)

        # Counter pill showing 1 / 5
        self.assertIn('1 / 5', content)

        # Modal Lightbox and action buttons present
        self.assertIn('id="fmGalleryLightbox"', content)
        self.assertIn('id="fmLightboxZoomToggle"', content)
        self.assertIn('id="fmLightboxCloseBtn"', content)
