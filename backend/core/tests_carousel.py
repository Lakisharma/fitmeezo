"""
Unit and integration tests for Fitmeezo Storefront Hero Image Carousel.
Verifies model validation, active limit constraints (max 4), admin operations,
and homepage rendering across 0, 1, 2, 3, and 4 images.
"""

import shutil
import tempfile
from io import BytesIO
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from core.models import CarouselImage

User = get_user_model()
TEST_MEDIA_DIR = tempfile.mkdtemp()


def create_test_image(name="test_slide.jpg", width=800, height=500, color="orange"):
    """Helper to generate a lightweight valid image in memory for testing."""
    file_obj = BytesIO()
    image = Image.new("RGB", (width, height), color=color)
    image.save(file_obj, format="JPEG")
    file_obj.seek(0)
    return SimpleUploadedFile(name, file_obj.read(), content_type="image/jpeg")


@override_settings(MEDIA_ROOT=TEST_MEDIA_DIR)
class CarouselImageModelTest(TestCase):
    """Verifies CarouselImage model fields, ordering, and max-4 active validation."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_DIR, ignore_errors=True)


    def test_create_carousel_image(self):
        """Verify successful creation of a carousel image."""
        img = create_test_image("slide1.jpg")
        slide = CarouselImage.objects.create(
            title="Diwali Mega Sale",
            image=img,
            alt_text="Festival special discounts across electronics and fashion",
            link_url="/categories/electronics/",
            display_order=1,
            is_active=True,
        )
        self.assertEqual(slide.title, "Diwali Mega Sale")
        self.assertEqual(slide.alt_text, "Festival special discounts across electronics and fashion")
        self.assertTrue(slide.is_active)
        self.assertEqual(slide.display_order, 1)
        self.assertIn("Diwali Mega Sale", str(slide))

    def test_max_4_active_images_validation(self):
        """Enforces that attempting to activate a 5th slide raises a ValidationError."""
        # Create 4 active slides
        for i in range(4):
            slide = CarouselImage(
                title=f"Active Slide {i}",
                image=create_test_image(f"slide_{i}.jpg"),
                display_order=i,
                is_active=True,
            )
            slide.full_clean()
            slide.save()

        self.assertEqual(CarouselImage.objects.filter(is_active=True).count(), 4)

        # Attempt to create and clean a 5th active slide
        fifth_slide = CarouselImage(
            title="Fifth Slide Attempt",
            image=create_test_image("slide_5.jpg"),
            display_order=5,
            is_active=True,
        )
        with self.assertRaises(ValidationError) as ctx:
            fifth_slide.full_clean()
        self.assertIn("is_active", ctx.exception.message_dict)

        # However, saving as INACTIVE should succeed with no errors
        fifth_slide.is_active = False
        fifth_slide.full_clean()
        fifth_slide.save()
        self.assertEqual(CarouselImage.objects.count(), 5)
        self.assertEqual(CarouselImage.objects.filter(is_active=True).count(), 4)

    def test_slide_reordering_and_updating(self):
        """Verify updating display_order and replacing image."""
        img1 = create_test_image("slide_a.jpg")
        slide = CarouselImage.objects.create(
            title="Slide A",
            image=img1,
            display_order=10,
            is_active=True,
        )
        # Update display order
        slide.display_order = 1
        slide.save()
        slide.refresh_from_db()
        self.assertEqual(slide.display_order, 1)

        # Replace image
        img2 = create_test_image("slide_replaced.jpg", color="blue")
        slide.image = img2
        slide.save()
        slide.refresh_from_db()
        self.assertIn("slide_replaced", slide.image.name)


@override_settings(MEDIA_ROOT=TEST_MEDIA_DIR)
class HomepageCarouselStorefrontTest(TestCase):
    """
    Verifies storefront hero section behavior with 0, 1, 2, 3, and 4 images.
    """

    def setUp(self):
        self.client = Client()

    def test_homepage_with_zero_carousel_images_renders_fallback(self):
        """When 0 carousel images are uploaded, fallback logo/image must be shown."""
        self.assertEqual(CarouselImage.objects.count(), 0)
        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        # Fallback image must be present
        self.assertIn("product-fallback.svg", content)
        self.assertIn("fm-carousel-fallback", content)
        # Interactive carousel track should NOT be rendered
        self.assertNotIn('id="fmHeroCarousel"', content)
        self.assertNotIn('id="fmCarouselPrevBtn"', content)

    def test_homepage_with_one_carousel_image_renders_single_slide(self):
        """With 1 image, display that slide without arrows or pagination dots."""
        CarouselImage.objects.create(
            title="Solo Banner",
            image=create_test_image("solo.jpg"),
            alt_text="Solo Hero Spotlight Banner",
            link_url="/categories/fashion/",
            display_order=0,
            is_active=True,
        )

        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn('id="fmHeroCarousel"', content)
        self.assertIn("Solo Hero Spotlight Banner", content)
        self.assertIn("fm-carousel-slide", content)
        # 1 image does not need arrows or dots
        self.assertNotIn('id="fmCarouselPrevBtn"', content)
        self.assertNotIn('id="fmCarouselDots"', content)

    def test_homepage_with_two_carousel_images(self):
        """With 2 images, render full carousel track, 2 slides, arrows, and 2 dots."""
        for i in range(2):
            CarouselImage.objects.create(
                title=f"Promo Slide {i + 1}",
                image=create_test_image(f"promo_{i}.jpg"),
                alt_text=f"Promo Alt Text {i + 1}",
                display_order=i,
                is_active=True,
            )

        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn('id="fmHeroCarousel"', content)
        self.assertIn('id="fmCarouselTrack"', content)
        self.assertIn('id="fmCarouselPrevBtn"', content)
        self.assertIn('id="fmCarouselNextBtn"', content)
        self.assertIn('id="fmCarouselDots"', content)
        self.assertIn("Promo Alt Text 1", content)
        self.assertIn("Promo Alt Text 2", content)
        self.assertEqual(content.count("fm-carousel-slide"), 2)
        self.assertEqual(content.count("data-slide-to="), 2)

    def test_homepage_with_three_carousel_images(self):
        """With 3 images, render 3 slides, arrows, and 3 dots."""
        for i in range(3):
            CarouselImage.objects.create(
                title=f"Triple Slide {i + 1}",
                image=create_test_image(f"triple_{i}.jpg"),
                alt_text=f"Triple Alt {i + 1}",
                display_order=i,
                is_active=True,
            )

        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn('id="fmHeroCarousel"', content)
        self.assertEqual(content.count("fm-carousel-slide"), 3)
        self.assertEqual(content.count("data-slide-to="), 3)

    def test_homepage_with_four_carousel_images(self):
        """With 4 images (maximum capacity), all 4 are rendered in proper order."""
        # Create in reverse display_order to test sorting
        CarouselImage.objects.create(title="Slide 4", image=create_test_image("s4.jpg"), alt_text="Alt 4", display_order=40, is_active=True)
        CarouselImage.objects.create(title="Slide 1", image=create_test_image("s1.jpg"), alt_text="Alt 1", display_order=10, is_active=True)
        CarouselImage.objects.create(title="Slide 3", image=create_test_image("s3.jpg"), alt_text="Alt 3", display_order=30, is_active=True)
        CarouselImage.objects.create(title="Slide 2", image=create_test_image("s2.jpg"), alt_text="Alt 2", display_order=20, is_active=True)

        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertEqual(content.count("fm-carousel-slide"), 4)
        self.assertEqual(content.count("data-slide-to="), 4)
        # Verify ordering: Alt 1 should appear before Alt 2, Alt 3, Alt 4
        pos1 = content.find("Alt 1")
        pos2 = content.find("Alt 2")
        pos3 = content.find("Alt 3")
        pos4 = content.find("Alt 4")
        self.assertTrue(pos1 < pos2 < pos3 < pos4)

    def test_inactive_slides_are_excluded_from_homepage(self):
        """Inactive slides must not be rendered on the storefront."""
        CarouselImage.objects.create(title="Active Slide", image=create_test_image("act.jpg"), alt_text="Visible Slide", display_order=1, is_active=True)
        CarouselImage.objects.create(title="Draft Slide", image=create_test_image("draft.jpg"), alt_text="Hidden Slide", display_order=2, is_active=False)

        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        self.assertIn("Visible Slide", content)
        self.assertNotIn("Hidden Slide", content)


@override_settings(MEDIA_ROOT=TEST_MEDIA_DIR)
class CustomAdminCarouselViewsTest(TestCase):
    """Verifies Custom Admin carousel management: list, add, edit, toggle, delete."""

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="carousel_admin",
            email="admin@fitmeezo.com",
            password="AdminPassword123!",
        )
        self.client.force_login(self.admin_user)

    def test_custom_admin_carousel_list_view(self):
        """Verify custom admin carousel list page loads with metrics."""
        CarouselImage.objects.create(
            title="Slide Alpha",
            image=create_test_image("alpha.jpg"),
            display_order=0,
            is_active=True,
        )
        response = self.client.get(reverse("custom_admin:carousel_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Storefront Hero Carousel")
        self.assertContains(response, "Slide Alpha")
        self.assertContains(response, "1 / 4")

    def test_custom_admin_carousel_add_view_get(self):
        """Verify custom admin add slide page renders form."""
        response = self.client.get(reverse("custom_admin:carousel_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add New Carousel Slide")

    def test_custom_admin_carousel_add_view_post(self):
        """Verify uploading a new slide via custom admin."""
        img = create_test_image("uploaded_slide.jpg")
        response = self.client.post(
            reverse("custom_admin:carousel_add"),
            {
                "title": "New Uploaded Slide",
                "image": img,
                "alt_text": "Special Offer Banner",
                "link_url": "/categories/electronics/",
                "display_order": 0,
                "is_active": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CarouselImage.objects.filter(title="New Uploaded Slide").exists())

    def test_custom_admin_carousel_toggle_status_view(self):
        """Verify toggling active status on a slide."""
        slide = CarouselImage.objects.create(
            title="Toggle Slide",
            image=create_test_image("toggle.jpg"),
            display_order=0,
            is_active=True,
        )
        # Deactivate
        response = self.client.post(reverse("custom_admin:carousel_toggle_status", kwargs={"pk": slide.pk}), follow=True)
        self.assertEqual(response.status_code, 200)
        slide.refresh_from_db()
        self.assertFalse(slide.is_active)

        # Reactivate
        response = self.client.post(reverse("custom_admin:carousel_toggle_status", kwargs={"pk": slide.pk}), follow=True)
        self.assertEqual(response.status_code, 200)
        slide.refresh_from_db()
        self.assertTrue(slide.is_active)

    def test_custom_admin_carousel_delete_view(self):
        """Verify deleting a carousel slide."""
        slide = CarouselImage.objects.create(
            title="Delete Me",
            image=create_test_image("del.jpg"),
            display_order=0,
            is_active=False,
        )
        slide_pk = slide.pk
        response = self.client.post(reverse("custom_admin:carousel_delete", kwargs={"pk": slide_pk}), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CarouselImage.objects.filter(pk=slide_pk).exists())


@override_settings(MEDIA_ROOT=TEST_MEDIA_DIR)
class StandardDjangoAdminCarouselTest(TestCase):
    """Verifies built-in Django Admin (/admin/core/carouselimage/) operations."""

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="django_admin_user",
            email="superadmin@fitmeezo.com",
            password="SuperPassword123!",
        )
        self.client.force_login(self.admin_user)

    def test_django_admin_changelist_view(self):
        """Verify changelist renders with thumbnail and slide data."""
        slide = CarouselImage.objects.create(
            title="Changelist Slide",
            image=create_test_image("cl_slide.jpg"),
            alt_text="Changelist Alt",
            display_order=1,
            is_active=True,
        )
        response = self.client.get(reverse("admin:core_carouselimage_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Changelist Slide")
        self.assertContains(response, "Changelist Alt")

    def test_django_admin_add_slide(self):
        """Verify uploading slide through Django Admin."""
        img = create_test_image("admin_add.jpg")
        response = self.client.post(
            reverse("admin:core_carouselimage_add"),
            {
                "title": "Created via Django Admin",
                "image": img,
                "alt_text": "Admin Alt Text",
                "link_url": "/categories/sports/",
                "display_order": 0,
                "is_active": True,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(CarouselImage.objects.filter(title="Created via Django Admin").exists())

    def test_django_admin_actions_activate_and_deactivate(self):
        """Verify admin actions activate_slides and deactivate_slides."""
        slide = CarouselImage.objects.create(
            title="Action Slide",
            image=create_test_image("action.jpg"),
            display_order=0,
            is_active=True,
        )
        # Deactivate action
        self.client.post(
            reverse("admin:core_carouselimage_changelist"),
            {
                "action": "deactivate_slides",
                "_selected_action": [str(slide.pk)],
            },
            follow=True,
        )
        slide.refresh_from_db()
        self.assertFalse(slide.is_active)

        # Activate action
        self.client.post(
            reverse("admin:core_carouselimage_changelist"),
            {
                "action": "activate_slides",
                "_selected_action": [str(slide.pk)],
            },
            follow=True,
        )
        slide.refresh_from_db()
        self.assertTrue(slide.is_active)

