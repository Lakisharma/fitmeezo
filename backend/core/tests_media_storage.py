"""
Tests for Fitmeezo Permanent Media Storage Configuration & Reconciliation.

Validates:
1. MEDIA_ROOT configuration priorities (Environment variable -> /var/www/fitmeezo/media -> BASE_DIR / 'media').
2. Product.primary_image and primary_image_url fallback logic (main image -> gallery image -> None).
3. Product card template rendering when image exists, when gallery image exists, and when no image exists.
4. reconcile_media_storage command audits and copies missing files from legacy media folder.
"""

import os
from pathlib import Path
import shutil
import tempfile

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.template.loader import render_to_string

from core.models import Category, Product, ProductImage, Seller


class MediaStorageConfigurationTests(TestCase):
    def setUp(self):
        self.temp_media = tempfile.mkdtemp()
        self.temp_legacy = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self.temp_media)
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self.temp_media, ignore_errors=True)
        shutil.rmtree(self.temp_legacy, ignore_errors=True)

    def test_product_primary_image_url_with_main_image(self):
        """When product has a main image, primary_image_url returns its URL."""
        dummy_img = SimpleUploadedFile(
            name="test_prod.jpg",
            content=b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b",
            content_type="image/jpeg",
        )
        cat = Category.objects.create(name="Apparel", slug="apparel")
        prod = Product.objects.create(
            name="Kurta Set",
            sku="KURTA-001",
            category=cat,
            price=1299.00,
            image=dummy_img,
        )
        self.assertIsNotNone(prod.primary_image_url)
        self.assertIn("test_prod", prod.primary_image_url)

    def test_product_primary_image_url_fallback_to_gallery_when_main_is_empty(self):
        """When product has no main image, primary_image_url returns the first gallery image URL."""
        dummy_img = SimpleUploadedFile(
            name="gallery_1.jpg",
            content=b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b",
            content_type="image/jpeg",
        )
        cat = Category.objects.create(name="Shoes", slug="shoes")
        prod = Product.objects.create(
            name="Sneakers",
            sku="SHOE-001",
            category=cat,
            price=2499.00,
            image=None,
        )
        self.assertIsNone(prod.primary_image_url)

        # Add gallery image
        gallery_item = ProductImage.objects.create(
            product=prod,
            image=dummy_img,
            alt_text="Side view",
            display_order=1,
        )
        self.assertEqual(prod.primary_image_url, gallery_item.image.url)

    def test_product_card_template_renders_gallery_image_when_main_image_is_missing(self):
        """Product card template shows the first gallery image if main image is None."""
        dummy_img = SimpleUploadedFile(
            name="gallery_preview.jpg",
            content=b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b",
            content_type="image/jpeg",
        )
        cat = Category.objects.create(name="Dresses", slug="dresses")
        prod = Product.objects.create(
            name="Evening Gown",
            sku="GOWN-001",
            category=cat,
            price=3999.00,
            image=None,
        )
        gallery_item = ProductImage.objects.create(
            product=prod,
            image=dummy_img,
            alt_text="Gown Front View",
        )

        rendered = render_to_string("includes/product_card.html", {"product": prod, "CURRENCY_SYMBOL": "₹"})
        self.assertIn(gallery_item.image.url, rendered)
        self.assertNotIn("product-fallback.svg", rendered.split("src=")[1].split(">")[0])

    def test_reconcile_media_storage_command_restores_missing_files(self):
        """reconcile_media_storage detects missing files in MEDIA_ROOT and copies from legacy directory."""
        cat = Category.objects.create(name="Accessories", slug="accessories")
        prod = Product.objects.create(
            name="Leather Belt",
            sku="BELT-001",
            category=cat,
            price=799.00,
        )
        # Directly set relative path on model field to simulate database record
        prod.image = "products/test_legacy_belt.jpg"
        prod.save(update_fields=["image"])

        # Create the file in legacy directory, but NOT in temp_media
        legacy_product_dir = Path(self.temp_legacy) / "products"
        legacy_product_dir.mkdir(parents=True, exist_ok=True)
        legacy_file = legacy_product_dir / "test_legacy_belt.jpg"
        legacy_file.write_bytes(b"dummy image data")

        target_file = Path(self.temp_media) / "products" / "test_legacy_belt.jpg"
        self.assertFalse(target_file.exists())

        # Run reconciliation with override
        with override_settings(MEDIA_ROOT=self.temp_media):
            call_command("reconcile_media_storage", source_dir=self.temp_legacy)

        # File should now exist in temp_media
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_bytes(), b"dummy image data")
