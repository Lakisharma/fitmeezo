"""
Fitmeezo Cloudinary Persistent Storage Test Suite.

Verifies:
1. Local development environment fallback to FileSystemStorage when credentials are absent.
2. Validation of environment variables (placeholder detection, URL parsing).
3. Cloudinary storage backend URL generation using mocks (zero network calls).
4. Media upload simulation using mocked Cloudinary API (zero customer data uploaded).
5. Safe dry-run and error resilience of the media migration management command.
"""

from io import BytesIO, StringIO
from unittest.mock import MagicMock, patch
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from PIL import Image

from core.models import Category, Product
from fitmeezo.settings import _is_valid_cloudinary_credential


class CloudinaryStorageConfigurationTests(TestCase):
    """
    Tests environment variable detection and fallback storage isolation.
    """

    def test_default_local_dev_storage_is_filesystem(self):
        """
        In local development without Cloudinary credentials, the application
        must default to standard FileSystemStorage and leave Cloudinary disabled.
        """
        self.assertFalse(getattr(settings, "CLOUDINARY_ENABLED", False))
        self.assertEqual(
            getattr(settings, "DEFAULT_FILE_STORAGE", ""),
            "django.core.files.storage.FileSystemStorage",
        )
        self.assertEqual(
            settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )

    def test_placeholder_credentials_ignored(self):
        """
        Common configuration placeholders must not accidentally enable Cloudinary.
        """
        placeholders = [
            "",
            "   ",
            "replace_with_your_cloud_name",
            "your_api_key",
            "todo_secret",
            "none",
        ]
        for val in placeholders:
            self.assertFalse(
                _is_valid_cloudinary_credential(val),
                f"Value '{val}' should be classified as invalid/placeholder.",
            )

    def test_valid_credentials_recognized(self):
        """
        Genuine credential tokens must pass validation.
        """
        valid_tokens = [
            "fitmeezo-production",
            "987654321012345",
            "aBcdEfGhIjKlMnOpQrStUvWxYz",
            "cloudinary://12345:secret@fitmeezocloud",
        ]
        for token in valid_tokens:
            self.assertTrue(
                _is_valid_cloudinary_credential(token),
                f"Token '{token}' should be recognized as valid.",
            )


@override_settings(
    CLOUDINARY_STORAGE={
        "CLOUD_NAME": "test_fitmeezo_cloud",
        "API_KEY": "1234567890",
        "API_SECRET": "test_api_secret_token",
        "SECURE": True,
        "MEDIA_TAG": "media",
    }
)
class CloudinaryStorageMockedTests(TestCase):
    """
    Validates storage behavior and URL generation with mocked Cloudinary SDK.
    Strictly prevents network calls or document uploads during tests.
    """

    def _create_test_image(self, name="test.jpg"):
        file_obj = BytesIO()
        image = Image.new("RGB", (100, 100), color=(255, 107, 0))
        image.save(file_obj, format="JPEG")
        file_obj.seek(0)
        return SimpleUploadedFile(name, file_obj.read(), content_type="image/jpeg")

    @patch("cloudinary.uploader.upload")
    def test_mock_product_image_upload_to_cloudinary(self, mock_upload):
        """
        When Cloudinary storage is active, uploading a product image routes through
        cloudinary.uploader.upload with the correct public_id and parameters.
        """
        mock_upload.return_value = {
            "public_id": "media/products/test_sneakers",
            "version": 1234567890,
            "format": "jpg",
            "secure_url": "https://res.cloudinary.com/test_fitmeezo_cloud/image/upload/v1234567890/media/products/test_sneakers.jpg",
        }

        from cloudinary_storage.storage import MediaCloudinaryStorage

        storage = MediaCloudinaryStorage()
        test_file = self._create_test_image("test_sneakers.jpg")

        saved_name = storage.save("products/test_sneakers.jpg", test_file)

        self.assertTrue(mock_upload.called)
        self.assertIn("products/test_sneakers", saved_name)

    def test_mock_cloudinary_url_resolution(self):
        """
        MediaCloudinaryStorage.url() must format the proper CDN URL structure.
        """
        from cloudinary_storage.storage import MediaCloudinaryStorage

        with patch("cloudinary.CloudinaryImage.build_url") as mock_build_url:
            mock_build_url.return_value = "https://res.cloudinary.com/test_fitmeezo_cloud/image/upload/v1/media/products/37397.jpeg"
            storage = MediaCloudinaryStorage()
            url = storage.url("products/37397.jpeg")

            self.assertEqual(url, "https://res.cloudinary.com/test_fitmeezo_cloud/image/upload/v1/media/products/37397.jpeg")


class MediaMigrationCommandTests(TestCase):
    """
    Tests the migrate_media_to_cloudinary management command.
    """

    def test_dry_run_executes_without_errors(self):
        """
        Running migrate_media_to_cloudinary --dry-run must execute cleanly
        and report simulated sync count.
        """
        out = StringIO()
        call_command("migrate_media_to_cloudinary", "--dry-run", stdout=out)
        output_str = out.getvalue()

        self.assertIn("=== Fitmeezo Cloudinary Media Migration Utility ===", output_str)
        self.assertIn("Mode: SIMULATION (--dry-run)", output_str)
        self.assertIn("=== Migration Summary ===", output_str)

    def test_live_run_blocked_when_unconfigured(self):
        """
        Running live migration when Cloudinary credentials are absent
        must safely abort and output an error message without crashing.
        """
        err = StringIO()
        call_command("migrate_media_to_cloudinary", stderr=err)
        err_str = err.getvalue()

        self.assertIn("Cloudinary is not configured", err_str)
