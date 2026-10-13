"""
Regression tests for Fitmeezo Django Admin Seller Creation & User Validation.

Verifies:
1. Eligible non-staff, active users can be selected and Sellers created successfully.
2. Form accepts selection by numeric ID, username, or email.
3. Staff users are rejected with clear, descriptive validation messages.
4. Superusers are rejected with clear, descriptive validation messages.
5. Staff and superuser permissions are strictly preserved (no downgrades/upgrades).
6. Inactive users are rejected with clear validation messages.
7. Duplicate seller assignment is rejected with clear validation messages.
8. Updating an existing seller maintains consistent querysets without choice errors.
9. Model-level clean() enforces the same security rules.
"""

from decimal import Decimal
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from core.admin import SellerAdmin, SellerAdminForm
from core.models import Seller

User = get_user_model()


class MockSuperUserSite(AdminSite):
    pass


class SellerAdminValidationRegressionTests(TestCase):
    """
    Regression test suite proving the Django Admin Seller creation validation fix.
    """

    def setUp(self):
        # Platform Superuser / Admin
        self.admin_user = User.objects.create_superuser(
            username="platform_admin",
            email="platform_admin@fitmeezo.in",
            password="AdminPassword123!",
        )

        # Staff User (not superuser)
        self.staff_user = User.objects.create_user(
            username="staff_member",
            email="staff@fitmeezo.in",
            password="StaffPassword123!",
            is_staff=True,
            is_superuser=False,
        )

        # Eligible non-staff active customers
        self.eligible_user1 = User.objects.create_user(
            username="vendor_alice",
            email="alice@vendor.in",
            password="UserPassword123!",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )
        self.eligible_user2 = User.objects.create_user(
            username="vendor_bob",
            email="bob@vendor.in",
            password="UserPassword123!",
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )

        # Inactive user
        self.inactive_user = User.objects.create_user(
            username="inactive_vendor",
            email="inactive@vendor.in",
            password="UserPassword123!",
            is_staff=False,
            is_superuser=False,
            is_active=False,
        )

        self.site = AdminSite()
        self.seller_admin = SellerAdmin(Seller, self.site)
        self.factory = RequestFactory()

        self.client = Client()
        self.client.force_login(self.admin_user)

    def _get_base_seller_post_data(self, user_val):
        return {
            "user": user_val,
            "store_name": "Apex Footwear Co",
            "slug": "apex-footwear-co",
            "legal_business_name": "Apex Footwear India Private Limited",
            "business_type": Seller.BUSINESS_TYPE_PVT_LTD,
            "support_email": "support@apexfootwear.in",
            "support_phone": "9876543210",
            "registered_address": "42 MG Road, Sector 3",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560001",
            "pickup_address": "Warehouse 12, Hosur Road",
            "pickup_city": "Bengaluru",
            "pickup_state": "Karnataka",
            "pickup_postal_code": "560100",
            "gstin": "29ABCDE1234F1Z5",
            "pan_number": "ABCDE1234F",
            "status": Seller.STATUS_PENDING,
            "commission_rate": "10.00",
            "is_active": "on",
        }

    def test_eligible_non_staff_user_creates_seller_via_admin_post_by_id(self):
        """
        Submitting an eligible non-staff user's ID via Django Admin /admin/core/seller/add/
        creates the Seller successfully and redirects.
        """
        post_data = self._get_base_seller_post_data(self.eligible_user1.id)
        response = self.client.post(reverse("admin:core_seller_add"), post_data)

        # Django Admin redirects on success
        self.assertEqual(response.status_code, 302)
        seller = Seller.objects.filter(store_name="Apex Footwear Co").first()
        self.assertIsNotNone(seller)
        self.assertEqual(seller.user, self.eligible_user1)
        self.assertEqual(seller.status, Seller.STATUS_PENDING)
        self.assertEqual(seller.commission_rate, Decimal("10.00"))

        # Verify security: user status untouched
        self.eligible_user1.refresh_from_db()
        self.assertFalse(self.eligible_user1.is_staff)
        self.assertFalse(self.eligible_user1.is_superuser)

    def test_eligible_user_creates_seller_via_admin_post_by_username(self):
        """
        Submitting an eligible non-staff user by username string succeeds.
        """
        post_data = self._get_base_seller_post_data("vendor_bob")
        post_data["store_name"] = "Bob Sports Gear"
        post_data["slug"] = "bob-sports-gear"
        post_data["gstin"] = "29AAAAA0000A1Z5"
        post_data["pan_number"] = "AAAAA0000A"

        response = self.client.post(reverse("admin:core_seller_add"), post_data)
        self.assertEqual(response.status_code, 302)
        seller = Seller.objects.filter(store_name="Bob Sports Gear").first()
        self.assertIsNotNone(seller)
        self.assertEqual(seller.user, self.eligible_user2)

    def test_staff_user_rejection_in_admin(self):
        """
        Selecting a staff user must be rejected with a clear validation message,
        and must NOT alter staff privileges.
        """
        post_data = self._get_base_seller_post_data(self.staff_user.id)
        response = self.client.post(reverse("admin:core_seller_add"), post_data)

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertIn("user", form.errors)
        self.assertIn("Staff members and superusers cannot be assigned as sellers", form.errors["user"][0])

        # Security check: staff status untouched
        self.staff_user.refresh_from_db()
        self.assertTrue(self.staff_user.is_staff)
        self.assertFalse(self.staff_user.is_superuser)
        self.assertFalse(hasattr(self.staff_user, "seller_profile"))

    def test_superuser_rejection_in_admin(self):
        """
        Selecting a superuser must be rejected with a clear validation message,
        and must NEVER downgrade or alter the superuser account.
        """
        post_data = self._get_base_seller_post_data(self.admin_user.id)
        response = self.client.post(reverse("admin:core_seller_add"), post_data)

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertIn("user", form.errors)
        self.assertIn("Staff members and superusers cannot be assigned as sellers", form.errors["user"][0])

        # Security check: superuser status untouched
        self.admin_user.refresh_from_db()
        self.assertTrue(self.admin_user.is_superuser)
        self.assertTrue(self.admin_user.is_staff)
        self.assertFalse(hasattr(self.admin_user, "seller_profile"))

    def test_duplicate_seller_user_rejection_in_admin(self):
        """
        Selecting a user that already has a linked Seller profile must be rejected
        with a clear error explaining duplicate assignment.
        """
        # Create initial seller for eligible_user1
        Seller.objects.create(
            user=self.eligible_user1,
            store_name="Initial Store",
            legal_business_name="Initial Store Legal",
            support_email="init@store.in",
            support_phone="9876543210",
            registered_address="123 Street",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="123 Street",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
        )

        post_data = self._get_base_seller_post_data(self.eligible_user1.id)
        post_data["store_name"] = "Second Store"
        post_data["slug"] = "second-store"
        response = self.client.post(reverse("admin:core_seller_add"), post_data)

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertIn("user", form.errors)
        self.assertIn("already linked to another seller store", form.errors["user"][0])

    def test_inactive_user_rejection_in_admin(self):
        """
        Selecting an inactive user must be rejected with a clear validation message.
        """
        post_data = self._get_base_seller_post_data(self.inactive_user.id)
        response = self.client.post(reverse("admin:core_seller_add"), post_data)

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertIn("user", form.errors)
        self.assertIn("Inactive users cannot be assigned as sellers", form.errors["user"][0])

    def test_unknown_user_rejection_in_admin(self):
        """
        Submitting a non-existent username or ID gives a descriptive error message.
        """
        post_data = self._get_base_seller_post_data("non_existent_user_999")
        response = self.client.post(reverse("admin:core_seller_add"), post_data)

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertIn("user", form.errors)
        self.assertIn("does not exist", form.errors["user"][0])

    def test_admin_get_form_queryset_scoping(self):
        """
        On GET /admin/core/seller/add/, the displayed user choices:
        - Include eligible active non-staff users without sellers.
        - Exclude staff, superusers, inactive users, and users with existing sellers.
        """
        # Create seller for eligible_user2 so bob has a seller
        Seller.objects.create(
            user=self.eligible_user2,
            store_name="Bob Store",
            legal_business_name="Bob Legal",
            support_email="bob@store.in",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Addr",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
        )

        response = self.client.get(reverse("admin:core_seller_add"))
        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        user_queryset = form.fields["user"].queryset

        # Eligible user1 is present
        self.assertIn(self.eligible_user1, user_queryset)
        # Staff user is excluded
        self.assertNotIn(self.staff_user, user_queryset)
        # Superuser is excluded
        self.assertNotIn(self.admin_user, user_queryset)
        # Inactive user is excluded
        self.assertNotIn(self.inactive_user, user_queryset)
        # User with existing seller (Bob) is excluded
        self.assertNotIn(self.eligible_user2, user_queryset)

    def test_existing_seller_update_preserves_user_in_queryset(self):
        """
        Editing an existing Seller preserves the linked user in the form's queryset,
        allowing successful update without invalid choice errors.
        """
        seller = Seller.objects.create(
            user=self.eligible_user1,
            store_name="Existing Store",
            legal_business_name="Existing Store Legal",
            support_email="exist@store.in",
            support_phone="9876543210",
            registered_address="123 St",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="123 St",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
        )

        # GET change page
        change_url = reverse("admin:core_seller_change", args=[seller.pk])
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        # Linked user MUST be in queryset
        self.assertIn(self.eligible_user1, form.fields["user"].queryset)

        # POST update to change store description
        post_data = self._get_base_seller_post_data(self.eligible_user1.id)
        post_data["store_name"] = "Existing Store"
        post_data["slug"] = "existing-store"
        post_data["store_description"] = "Updated store description"
        response = self.client.post(change_url, post_data)
        self.assertEqual(response.status_code, 302)

        seller.refresh_from_db()
        self.assertEqual(seller.store_description, "Updated store description")
        self.assertEqual(seller.user, self.eligible_user1)

    def test_model_clean_rejects_staff_and_superuser(self):
        """
        Seller.clean() model-level validation rejects staff or superuser assignments.
        """
        seller_staff = Seller(
            user=self.staff_user,
            store_name="Staff Store",
            legal_business_name="Staff Legal",
            support_email="s@s.com",
            support_phone="1234567890",
            registered_address="Addr",
            city="C",
            state="S",
            postal_code="123456",
            pickup_address="Addr",
            pickup_city="C",
            pickup_state="S",
            pickup_postal_code="123456",
        )
        with self.assertRaises(ValidationError) as ctx:
            seller_staff.clean()
        self.assertIn("user", ctx.exception.message_dict)
        self.assertIn("Staff members and superusers cannot be assigned as sellers", ctx.exception.message_dict["user"][0])

        seller_super = Seller(
            user=self.admin_user,
            store_name="Super Store",
            legal_business_name="Super Legal",
            support_email="s@s.com",
            support_phone="1234567890",
            registered_address="Addr",
            city="C",
            state="S",
            postal_code="123456",
            pickup_address="Addr",
            pickup_city="C",
            pickup_state="S",
            pickup_postal_code="123456",
        )
        with self.assertRaises(ValidationError) as ctx:
            seller_super.clean()
        self.assertIn("user", ctx.exception.message_dict)

    def test_model_clean_rejects_duplicate_user(self):
        """
        Seller.clean() model-level validation rejects duplicate seller user assignments.
        """
        Seller.objects.create(
            user=self.eligible_user1,
            store_name="First Store",
            legal_business_name="First Legal",
            support_email="f@f.com",
            support_phone="1234567890",
            registered_address="Addr",
            city="C",
            state="S",
            postal_code="123456",
            pickup_address="Addr",
            pickup_city="C",
            pickup_state="S",
            pickup_postal_code="123456",
        )

        seller_dup = Seller(
            user=self.eligible_user1,
            store_name="Duplicate Store",
            legal_business_name="Dup Legal",
            support_email="d@d.com",
            support_phone="1234567890",
            registered_address="Addr",
            city="C",
            state="S",
            postal_code="123456",
            pickup_address="Addr",
            pickup_city="C",
            pickup_state="S",
            pickup_postal_code="123456",
        )
        with self.assertRaises(ValidationError) as ctx:
            seller_dup.clean()
        self.assertIn("user", ctx.exception.message_dict)
        self.assertIn("already linked to another seller store", ctx.exception.message_dict["user"][0])

    def test_seller_creation_auto_populates_pickup_address_from_registered_address(self):
        """
        When courier dispatch pickup facility details are omitted in Django admin,
        the form gracefully inherits registered business address fields and saves.
        """
        post_data = self._get_base_seller_post_data(self.eligible_user1.id)
        post_data["store_name"] = "Registered Only Store"
        post_data["slug"] = "registered-only-store"
        post_data["pickup_address"] = ""
        post_data["pickup_city"] = ""
        post_data["pickup_state"] = ""
        post_data["pickup_postal_code"] = ""

        response = self.client.post(reverse("admin:core_seller_add"), post_data)
        self.assertEqual(response.status_code, 302)

        seller = Seller.objects.filter(store_name="Registered Only Store").first()
        self.assertIsNotNone(seller)
        self.assertEqual(seller.pickup_address, "42 MG Road, Sector 3")
        self.assertEqual(seller.pickup_city, "Bengaluru")
        self.assertEqual(seller.pickup_state, "Karnataka")
        self.assertEqual(seller.pickup_postal_code, "560001")

    def test_seller_creation_auto_slug_when_blank(self):
        """
        When store slug is left blank in Django admin, it auto-generates from store name.
        """
        post_data = self._get_base_seller_post_data(self.eligible_user1.id)
        post_data["store_name"] = "Auto Slug Footwear Store"
        post_data["slug"] = ""

        response = self.client.post(reverse("admin:core_seller_add"), post_data)
        self.assertEqual(response.status_code, 302)

        seller = Seller.objects.filter(store_name="Auto Slug Footwear Store").first()
        self.assertIsNotNone(seller)
        self.assertEqual(seller.slug, "auto-slug-footwear-store")

    def test_admin_form_rejects_approved_status_without_valid_gstin(self):
        """
        Attempting to create an Approved seller without a valid 15-character GSTIN
        is rejected with actionable error guidance on the status and gstin fields.
        """
        post_data = self._get_base_seller_post_data(self.eligible_user1.id)
        post_data["store_name"] = "Instant Approval Store"
        post_data["status"] = Seller.STATUS_APPROVED
        post_data["gstin"] = ""
        post_data["pan_number"] = ""

        response = self.client.post(reverse("admin:core_seller_add"), post_data)
        self.assertEqual(response.status_code, 200)

        form = response.context["adminform"].form
        self.assertIn("status", form.errors)
        self.assertIn("A seller cannot be approved without a valid 15-character GSTIN", form.errors["status"][0])

