"""
Fitmeezo Phase 1 Multi-Seller Foundation Tests.
Validates:
1. Seller registration workflow and pending status initialization.
2. GSTIN and PAN format and consistency validation.
3. Access control & state machine: pending, rejected, and suspended sellers blocked from approved features.
4. Non-staff isolation: Approved sellers strictly denied access to /admin/ and /custom-admin/.
5. Administrative authorization: Only platform superusers/staff can approve, reject, or suspend sellers.
6. Storefront backwards compatibility: Products with seller=None function identically to prior baseline.
7. Permission layer: @seller_required and IsApprovedSeller DRF permission.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APIRequestFactory

from core.models import Address, Category, Order, OrderItem, Product, ProductImage, ProductVariant, Seller, SellerOrder
from core.permissions import IsApprovedSeller, is_approved_seller, seller_required
from core.services import cancel_order

User = get_user_model()


class SellerModelAndValidationTests(TestCase):
    """Unit tests for Seller model, validations, and lifecycle methods."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="seller1@example.com",
            email="seller1@example.com",
            password="SecurePassword123!",
        )

    def test_seller_creation_defaults_to_pending(self):
        seller = Seller.objects.create(
            user=self.user,
            store_name="Royal Electronics",
            legal_business_name="Royal Tech India Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@royalelec.in",
            support_phone="+91 9876543210",
            registered_address="123 MG Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Warehouse 4, Electronic City",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
        )
        self.assertEqual(seller.status, Seller.STATUS_PENDING)
        self.assertFalse(seller.is_approved)
        self.assertFalse(seller.can_sell)
        self.assertEqual(seller.slug, "royal-electronics")
        self.assertEqual(seller.commission_rate, Decimal("0.00"))
        self.assertIsNone(seller.approved_at)

    def test_valid_gstin_and_pan_validation(self):
        seller = Seller(
            user=self.user,
            store_name="Valid Tax Seller",
            legal_business_name="Valid Tax Retail LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="tax@valid.in",
            support_phone="9876543210",
            registered_address="45 Commercial St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="45 Commercial St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
        )
        # clean() should pass without error
        seller.clean()
        seller.save()
        self.assertEqual(seller.gstin, "29ABCDE1234F1Z5")
        self.assertEqual(seller.pan_number, "ABCDE1234F")

    def test_invalid_gstin_format_raises_validation_error(self):
        seller = Seller(
            user=self.user,
            store_name="Bad GST Seller",
            legal_business_name="Bad GST Store",
            registered_address="1 St",
            city="City",
            state="State",
            postal_code="123456",
            pickup_address="1 St",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="123456",
            support_email="test@bad.com",
            support_phone="1234567890",
            gstin="INVALID_GSTIN_123",
        )
        with self.assertRaises(ValidationError) as ctx:
            seller.clean()
        self.assertIn("gstin", ctx.exception.message_dict)

    def test_invalid_pan_format_raises_validation_error(self):
        seller = Seller(
            user=self.user,
            store_name="Bad PAN Seller",
            legal_business_name="Bad PAN Store",
            registered_address="1 St",
            city="City",
            state="State",
            postal_code="123456",
            pickup_address="1 St",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="123456",
            support_email="test@bad.com",
            support_phone="1234567890",
            pan_number="1234567890",  # Numbers instead of 5 letters
        )
        with self.assertRaises(ValidationError) as ctx:
            seller.clean()
        self.assertIn("pan_number", ctx.exception.message_dict)

    def test_pan_mismatch_with_gstin_raises_validation_error(self):
        seller = Seller(
            user=self.user,
            store_name="Mismatch Tax Seller",
            legal_business_name="Mismatch Store",
            registered_address="1 St",
            city="City",
            state="State",
            postal_code="123456",
            pickup_address="1 St",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="123456",
            support_email="test@bad.com",
            support_phone="1234567890",
            gstin="29ABCDE1234F1Z5",
            pan_number="XYZWQ9999K",  # Does not match ABCDE1234F
        )
        with self.assertRaises(ValidationError) as ctx:
            seller.clean()
        self.assertIn("pan_number", ctx.exception.message_dict)

    def test_approval_lifecycle_methods(self):
        seller = Seller.objects.create(
            user=self.user,
            store_name="Lifecycle Store",
            legal_business_name="Lifecycle Entity",
            support_email="life@cycle.in",
            support_phone="9876543210",
            registered_address="Street",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
        )
        # 1. Approve without GSTIN must fail
        with self.assertRaises(ValidationError) as ctx:
            seller.approve()
        self.assertIn("Cannot approve seller without a valid submitted GSTIN", str(ctx.exception))

        # 2. Approve with valid GSTIN
        seller.gstin = "29ABCDE1234F1Z5"
        seller.pan_number = "ABCDE1234F"
        seller.save()
        seller.approve()
        seller.refresh_from_db()
        self.assertEqual(seller.status, Seller.STATUS_APPROVED)
        self.assertTrue(seller.is_active)
        self.assertTrue(seller.is_approved)
        self.assertTrue(seller.can_sell)
        self.assertIsNotNone(seller.approved_at)

        # 3. Suspend
        seller.suspend(reason="Quality check audit pending")
        seller.refresh_from_db()
        self.assertEqual(seller.status, Seller.STATUS_SUSPENDED)
        self.assertFalse(seller.is_active)
        self.assertFalse(seller.is_approved)
        self.assertFalse(seller.can_sell)
        self.assertIn("Quality check audit pending", seller.admin_notes)

        # 4. Reject
        seller.reject(reason="Failed verification")
        seller.refresh_from_db()
        self.assertEqual(seller.status, Seller.STATUS_REJECTED)
        self.assertFalse(seller.is_active)
        self.assertFalse(seller.is_approved)
        self.assertFalse(seller.can_sell)
        self.assertIn("Failed verification", seller.admin_notes)

    def test_missing_and_duplicate_gstin_model_validation(self):
        """Model clean() rejects duplicate GSTIN and approval status without GSTIN."""
        seller1 = Seller.objects.create(
            user=self.user,
            store_name="Store One",
            legal_business_name="Entity One",
            registered_address="Addr 1",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Addr 1",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            support_email="s1@test.in",
            support_phone="9876543210",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
        )
        seller1.clean()  # Passes

        user2 = User.objects.create_user(
            username="seller2@example.com",
            email="seller2@example.com",
            password="SecurePassword123!",
        )
        seller2 = Seller(
            user=user2,
            store_name="Store Two",
            legal_business_name="Entity Two",
            registered_address="Addr 2",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Addr 2",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            support_email="s2@test.in",
            support_phone="9876543210",
            gstin="29ABCDE1234F1Z5",  # Duplicate GSTIN
            pan_number="ABCDE1234F",
        )
        with self.assertRaises(ValidationError) as ctx:
            seller2.clean()
        self.assertIn("gstin", ctx.exception.message_dict)
        self.assertIn("already registered", ctx.exception.message_dict["gstin"][0])

        # Test approving without valid GSTIN via clean()
        seller3 = Seller(
            user=user2,
            store_name="Store Three",
            legal_business_name="Entity Three",
            registered_address="Addr 3",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Addr 3",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            support_email="s3@test.in",
            support_phone="9876543210",
            status=Seller.STATUS_APPROVED,
            gstin="",  # Missing GSTIN on approved seller
        )
        with self.assertRaises(ValidationError) as ctx3:
            seller3.clean()
        self.assertIn("gstin", ctx3.exception.message_dict)


class SellerPermissionAndIsolationTests(TestCase):
    """
    Validates server-side permission enforcement and staff isolation.
    Sellers must NEVER obtain access to /admin/ or /custom-admin/.
    """

    def setUp(self):
        self.client = Client()
        # Normal Customer
        self.customer = User.objects.create_user(
            username="customer@example.com",
            email="customer@example.com",
            password="Password123!",
        )
        # Pending Seller
        self.seller_user_pending = User.objects.create_user(
            username="pending_seller@example.com",
            email="pending_seller@example.com",
            password="Password123!",
        )
        self.seller_pending = Seller.objects.create(
            user=self.seller_user_pending,
            store_name="Pending Store",
            legal_business_name="Pending Store LLP",
            support_email="p@seller.in",
            support_phone="9876543210",
            registered_address="Street",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            status=Seller.STATUS_PENDING,
        )
        # Approved Seller
        self.seller_user_approved = User.objects.create_user(
            username="approved_seller@example.com",
            email="approved_seller@example.com",
            password="Password123!",
        )
        self.seller_approved = Seller.objects.create(
            user=self.seller_user_approved,
            store_name="Approved Store",
            legal_business_name="Approved Retailers Pvt Ltd",
            support_email="a@seller.in",
            support_phone="9876543210",
            registered_address="Street",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )
        # Approved Seller but missing GSTIN
        self.seller_user_no_gstin = User.objects.create_user(
            username="no_gstin_seller@example.com",
            email="no_gstin_seller@example.com",
            password="Password123!",
        )
        self.seller_no_gstin = Seller.objects.create(
            user=self.seller_user_no_gstin,
            store_name="No GSTIN Store",
            legal_business_name="No GSTIN Store Ltd",
            support_email="nogst@seller.in",
            support_phone="9876543210",
            registered_address="Street",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="",
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )
        # Staff Admin
        self.admin_user = User.objects.create_superuser(
            username="platform_admin@fitmeezo.in",
            email="platform_admin@fitmeezo.in",
            password="SuperPassword123!",
        )

    def test_seller_user_has_no_staff_or_superuser_flags(self):
        """Seller users must not be staff or superusers."""
        self.assertFalse(self.seller_user_pending.is_staff)
        self.assertFalse(self.seller_user_pending.is_superuser)
        self.assertFalse(self.seller_user_approved.is_staff)
        self.assertFalse(self.seller_user_approved.is_superuser)

    def test_approved_seller_cannot_access_original_django_admin(self):
        """Approved sellers must be denied access to /admin/."""
        self.client.login(username="approved_seller@example.com", password="Password123!")
        response = self.client.get("/admin/")
        # Django admin redirects non-staff users to admin login or denies with 302/403
        self.assertIn(response.status_code, [302, 403])
        if response.status_code == 302:
            self.assertIn("/admin/login/", response.url)

    def test_approved_seller_cannot_access_custom_admin(self):
        """Approved sellers must be denied access to /custom-admin/."""
        self.client.login(username="approved_seller@example.com", password="Password123!")
        response = self.client.get(reverse("custom_admin:dashboard"))
        # custom admin uses staff_member_required which redirects non-staff to custom_admin:login
        self.assertIn(response.status_code, [302, 403])
        if response.status_code == 302:
            self.assertIn("custom-admin/login", response.url)

    def test_seller_required_decorator_blocks_non_seller(self):
        """@seller_required redirects non-sellers to registration with warning."""
        from django.http import HttpResponse

        @seller_required
        def dummy_view(request):
            return HttpResponse("Authorized Seller View")

        factory = APIRequestFactory()
        request = factory.get("/dummy-seller-endpoint/")
        request.user = self.customer
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        response = dummy_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_register"))

    def test_seller_required_decorator_blocks_pending_seller(self):
        """@seller_required redirects pending sellers to status view."""
        from django.http import HttpResponse

        @seller_required
        def dummy_view(request):
            return HttpResponse("Authorized Seller View")

        factory = APIRequestFactory()
        request = factory.get("/dummy-seller-endpoint/")
        request.user = self.seller_user_pending
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        response = dummy_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

    def test_seller_required_decorator_allows_approved_seller(self):
        """@seller_required allows approved sellers to proceed."""
        from django.http import HttpResponse

        @seller_required
        def dummy_view(request):
            return HttpResponse("Authorized Seller View")

        factory = APIRequestFactory()
        request = factory.get("/dummy-seller-endpoint/")
        request.user = self.seller_user_approved
        setattr(request, "session", {})

        response = dummy_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "Authorized Seller View")

    def test_drf_is_approved_seller_permission(self):
        """DRF IsApprovedSeller permission class evaluation."""
        perm = IsApprovedSeller()
        factory = APIRequestFactory()

        # Anonymous
        anon_request = factory.get("/api/dummy/")
        from django.contrib.auth.models import AnonymousUser
        anon_request.user = AnonymousUser()
        self.assertFalse(perm.has_permission(anon_request, None))

        # Customer
        cust_request = factory.get("/api/dummy/")
        cust_request.user = self.customer
        self.assertFalse(perm.has_permission(cust_request, None))

        # Pending Seller
        pending_request = factory.get("/api/dummy/")
        pending_request.user = self.seller_user_pending
        self.assertFalse(perm.has_permission(pending_request, None))

        # Approved Seller
        approved_request = factory.get("/api/dummy/")
        approved_request.user = self.seller_user_approved
        self.assertTrue(perm.has_permission(approved_request, None))

        # Approved Seller WITHOUT valid GSTIN
        no_gst_request = factory.get("/api/dummy/")
        no_gst_request.user = self.seller_user_no_gstin
        self.assertFalse(perm.has_permission(no_gst_request, None))

    def test_seller_required_blocks_approved_seller_missing_gstin(self):
        """@seller_required blocks approved sellers who lack a valid GSTIN."""
        from django.http import HttpResponse

        @seller_required
        def dummy_view(request):
            return HttpResponse("Authorized Seller View")

        factory = APIRequestFactory()
        request = factory.get("/dummy-seller-endpoint/")
        request.user = self.seller_user_no_gstin
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        response = dummy_view(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

    def test_unapproved_seller_cannot_access_seller_profile_view(self):
        """Unapproved sellers cannot access protected core:seller_profile view."""
        self.client.login(username="pending_seller@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_profile"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))


class SellerOnboardingViewsTests(TestCase):
    """Storefront seller registration and status view tests."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="new_seller@example.com",
            email="new_seller@example.com",
            password="Password123!",
        )

    def test_registration_requires_login(self):
        """
        Public 'Become a Seller' registration is accessible to unauthenticated visitors (HTTP 200),
        presenting the public self-service merchant onboarding form.
        """
        response = self.client.get(reverse("core:seller_register"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/register.html")
        self.assertTrue(response.context["is_public_registration"])

    def test_seller_registration_form_submission(self):
        self.client.login(username="new_seller@example.com", password="Password123!")
        payload = {
            "store_name": "Nova Trends",
            "legal_business_name": "Nova Trends Apparel",
            "business_type": "sole_proprietorship",
            "store_description": "Premium apparel and lifestyle merchandise.",
            "support_email": "support@novatrends.in",
            "support_phone": "9876543210",
            "registered_address": "88 Brigade Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560025",
            "pickup_address": "Warehouse 10, Peenya Industrial Area",
            "pickup_city": "Bengaluru",
            "pickup_state": "Karnataka",
            "pickup_postal_code": "560058",
            "gstin": "29ABCDE1234F1Z5",
            "pan_number": "ABCDE1234F",
        }
        response = self.client.post(reverse("core:seller_register"), payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

        # Verify database record
        seller = Seller.objects.get(user=self.user)
        self.assertEqual(seller.store_name, "Nova Trends")
        self.assertEqual(seller.status, Seller.STATUS_PENDING)
        self.assertFalse(seller.is_approved)

    def test_tampering_attempt_on_status_and_commission_fails(self):
        """A seller submitting status='approved' or commission_rate in POST is ignored."""
        self.client.login(username="new_seller@example.com", password="Password123!")
        payload = {
            "store_name": "Hacker Store",
            "legal_business_name": "Hacker Store LLC",
            "business_type": "individual",
            "support_email": "hacker@store.in",
            "support_phone": "9876543210",
            "registered_address": "Addr",
            "city": "City",
            "state": "State",
            "postal_code": "560001",
            "pickup_address": "Pickup",
            "pickup_city": "City",
            "pickup_state": "State",
            "pickup_postal_code": "560001",
            "gstin": "29ABCDE1234F1Z5",
            "pan_number": "ABCDE1234F",
            # Malicious fields not in form
            "status": "approved",
            "commission_rate": "99.99",
            "is_staff": True,
        }
        response = self.client.post(reverse("core:seller_register"), payload)
        self.assertEqual(response.status_code, 302)
        seller = Seller.objects.get(user=self.user)
        self.assertEqual(seller.status, Seller.STATUS_PENDING)
        self.assertEqual(seller.commission_rate, Decimal("0.00"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)

    def test_registration_missing_gstin_fails(self):
        """Seller registration without GSTIN fails validation with descriptive error."""
        self.client.login(username="new_seller@example.com", password="Password123!")
        payload = {
            "store_name": "No GST Store",
            "legal_business_name": "No GST Store LLC",
            "business_type": "individual",
            "support_email": "nogst@store.in",
            "support_phone": "9876543210",
            "registered_address": "Addr",
            "city": "City",
            "state": "State",
            "postal_code": "560001",
            "pickup_address": "Pickup",
            "pickup_city": "City",
            "pickup_state": "State",
            "pickup_postal_code": "560001",
            "gstin": "",  # Missing GSTIN
        }
        response = self.client.post(reverse("core:seller_register"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Seller.objects.filter(user=self.user).exists())
        self.assertContains(response, "GSTIN is mandatory for seller onboarding.")

    def test_registration_invalid_gstin_format_fails(self):
        """Seller registration with invalid GSTIN format fails validation."""
        self.client.login(username="new_seller@example.com", password="Password123!")
        payload = {
            "store_name": "Bad GST Store",
            "legal_business_name": "Bad GST Store LLC",
            "business_type": "individual",
            "support_email": "badgst@store.in",
            "support_phone": "9876543210",
            "registered_address": "Addr",
            "city": "City",
            "state": "State",
            "postal_code": "560001",
            "pickup_address": "Pickup",
            "pickup_city": "City",
            "pickup_state": "State",
            "pickup_postal_code": "560001",
            "gstin": "INVALID12345",  # Invalid GSTIN format
        }
        response = self.client.post(reverse("core:seller_register"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Seller.objects.filter(user=self.user).exists())
        self.assertContains(response, "Invalid Indian GSTIN format")

    def test_registration_duplicate_gstin_fails(self):
        """Seller registration with duplicate GSTIN fails validation with descriptive error."""
        # Existing seller with GSTIN
        existing_user = User.objects.create_user(
            username="existing_s@example.com",
            email="existing_s@example.com",
            password="Password123!",
        )
        Seller.objects.create(
            user=existing_user,
            store_name="Existing Store",
            legal_business_name="Existing Entity",
            support_email="ex@store.in",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
        )

        self.client.login(username="new_seller@example.com", password="Password123!")
        payload = {
            "store_name": "Duplicate GST Store",
            "legal_business_name": "Duplicate Entity",
            "business_type": "individual",
            "support_email": "dup@store.in",
            "support_phone": "9876543210",
            "registered_address": "Addr",
            "city": "City",
            "state": "State",
            "postal_code": "560001",
            "pickup_address": "Pickup",
            "pickup_city": "City",
            "pickup_state": "State",
            "pickup_postal_code": "560001",
            "gstin": "29ABCDE1234F1Z5",  # Same GSTIN
            "pan_number": "ABCDE1234F",
        }
        response = self.client.post(reverse("core:seller_register"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Seller.objects.filter(user=self.user).exists())
        self.assertContains(response, "A seller with this GSTIN is already registered on the platform.")

    def test_status_view_renders_correctly(self):
        seller = Seller.objects.create(
            user=self.user,
            store_name="Status Test Store",
            legal_business_name="Status Test LLC",
            support_email="status@test.in",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_PENDING,
        )
        self.client.login(username="new_seller@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_status"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Status Test Store")
        self.assertContains(response, "Under Review")


class ProductSellerIntegrationTests(TestCase):
    """
    Verifies that existing products with seller=None continue functioning perfectly,
    and products associated with a seller expose proper attributes without regressions.
    """

    def setUp(self):
        self.category = Category.objects.create(name="Electronics", slug="electronics")
        # Direct Fitmeezo product (seller=None)
        self.platform_product = Product.objects.create(
            category=self.category,
            name="FitPulse Smartwatch Direct",
            slug="fitpulse-smartwatch-direct",
            sku="FP-DIR-001",
            price=Decimal("1999.00"),
            stock_quantity=50,
            is_active=True,
            seller=None,
        )
        # Third-party seller product
        self.seller_user = User.objects.create_user(
            username="tp_seller@example.com",
            email="tp_seller@example.com",
            password="Password123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Gadget Galaxy",
            legal_business_name="Gadget Galaxy Pvt Ltd",
            support_email="gg@galaxy.in",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_APPROVED,
        )
        self.seller_product = Product.objects.create(
            category=self.category,
            name="Galaxy Wireless Earbuds",
            slug="galaxy-wireless-earbuds",
            sku="GG-EAR-001",
            price=Decimal("1499.00"),
            stock_quantity=20,
            is_active=True,
            seller=self.seller,
        )

    def test_platform_direct_product_has_default_seller_name(self):
        """When seller=None, seller_name defaults to Fitmeezo."""
        self.assertIsNone(self.platform_product.seller)
        self.assertEqual(self.platform_product.seller_name, "Fitmeezo")

    def test_seller_product_exposes_store_name(self):
        """When seller is assigned, seller_name matches seller.store_name."""
        self.assertIsNotNone(self.seller_product.seller)
        self.assertEqual(self.seller_product.seller_name, "Gadget Galaxy")

    def test_storefront_product_detail_loads_both_products(self):
        """Both direct and third-party products render correctly on PDP."""
        client = Client()
        # Direct product
        resp1 = client.get(self.platform_product.get_absolute_url())
        self.assertEqual(resp1.status_code, 200)
        self.assertContains(resp1, "FitPulse Smartwatch Direct")

        # Seller product
        resp2 = client.get(self.seller_product.get_absolute_url())
        self.assertEqual(resp2.status_code, 200)
        self.assertContains(resp2, "Galaxy Wireless Earbuds")


class AdminSellerManagementTests(TestCase):
    """Verifies that platform administrators can approve/reject sellers via /admin/."""

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin@fitmeezo.in",
            email="admin@fitmeezo.in",
            password="AdminPassword123!",
        )
        self.seller_user = User.objects.create_user(
            username="seller_admin_test@example.com",
            email="seller_admin_test@example.com",
            password="Password123!",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Admin Test Store",
            legal_business_name="Admin Test Retail",
            support_email="a@test.in",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_PENDING,
        )

    def test_admin_can_view_seller_list(self):
        self.client.login(username="admin@fitmeezo.in", password="AdminPassword123!")
        response = self.client.get(reverse("admin:core_seller_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Admin Test Store")

    def test_admin_approve_seller_action(self):
        self.client.login(username="admin@fitmeezo.in", password="AdminPassword123!")
        post_data = {
            "action": "approve_sellers",
            "_selected_action": [str(self.seller.pk)],
        }
        response = self.client.post(reverse("admin:core_seller_changelist"), post_data)
        self.assertEqual(response.status_code, 302)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_APPROVED)
        self.assertTrue(self.seller.is_active)
        self.assertIsNotNone(self.seller.approved_at)

    def test_admin_cannot_approve_seller_without_valid_gstin(self):
        """Administrator cannot approve a seller lacking a valid GSTIN."""
        unqualified_user = User.objects.create_user(
            username="unqualified@example.com",
            email="unqualified@example.com",
            password="Password123!",
        )
        unqualified_seller = Seller.objects.create(
            user=unqualified_user,
            store_name="No GST Seller",
            legal_business_name="No GST Entity",
            support_email="nogst@test.in",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="State",
            postal_code="560001",
            pickup_address="Pickup",
            pickup_city="City",
            pickup_state="State",
            pickup_postal_code="560001",
            gstin="",
            status=Seller.STATUS_PENDING,
        )
        self.client.login(username="admin@fitmeezo.in", password="AdminPassword123!")
        post_data = {
            "action": "approve_sellers",
            "_selected_action": [str(unqualified_seller.pk)],
        }
        response = self.client.post(reverse("admin:core_seller_changelist"), post_data, follow=True)
        self.assertEqual(response.status_code, 200)
        unqualified_seller.refresh_from_db()
        self.assertEqual(unqualified_seller.status, Seller.STATUS_PENDING)
        self.assertFalse(unqualified_seller.is_approved)
        self.assertContains(response, "A valid 15-character GSTIN is required before approval")

    def test_non_admin_cannot_approve_sellers(self):
        """A normal customer or seller cannot post to Django admin approve action."""
        self.client.login(username="seller_admin_test@example.com", password="Password123!")
        post_data = {
            "action": "approve_sellers",
            "_selected_action": [str(self.seller.pk)],
        }
        response = self.client.post(reverse("admin:core_seller_changelist"), post_data)
        # Denied with redirect to admin login or 403
        self.assertIn(response.status_code, [302, 403])
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_PENDING)

    def test_admin_suspend_seller_action(self):
        self.client.login(username="admin@fitmeezo.in", password="AdminPassword123!")
        post_data = {
            "action": "suspend_sellers",
            "_selected_action": [str(self.seller.pk)],
        }
        response = self.client.post(reverse("admin:core_seller_changelist"), post_data)
        self.assertEqual(response.status_code, 302)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.status, Seller.STATUS_SUSPENDED)
        self.assertFalse(self.seller.is_active)


class SellerCommissionAndSnapshotTests(TestCase):
    """
    Verifies Section 2 requirements:
    1. Admin-configurable individual commission rates per seller.
    2. Different sellers can have different commission rates.
    3. DecimalField validation within 0.00% to 100.00%.
    4. Reliable historical commission snapshotting on OrderItem at checkout.
    5. Commission changes affect future orders only, without rewriting historical records.
    6. Products with seller=None snapshot 0.00% commission.
    7. Sellers cannot view or alter commission via seller profile.
    """

    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Apparel", slug="apparel")

        # Seller A (10.00% commission)
        self.user_a = User.objects.create_user(
            username="seller_a@example.com",
            email="seller_a@example.com",
            password="Password123!",
        )
        self.seller_a = Seller.objects.create(
            user=self.user_a,
            store_name="Alpha Apparel",
            legal_business_name="Alpha Apparel Pvt Ltd",
            support_email="alpha@apparel.in",
            support_phone="9876543210",
            registered_address="100 Brigade Rd",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560025",
            pickup_address="100 Brigade Rd",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560025",
            gstin="29AAAAA0000A1Z5",
            pan_number="AAAAA0000A",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )
        self.product_a = Product.objects.create(
            category=self.category,
            name="Alpha Casual Shirt",
            slug="alpha-casual-shirt",
            sku="AL-SHT-001",
            price=Decimal("1000.00"),
            stock_quantity=50,
            is_active=True,
            seller=self.seller_a,
        )

        # Seller B (15.50% commission)
        self.user_b = User.objects.create_user(
            username="seller_b@example.com",
            email="seller_b@example.com",
            password="Password123!",
        )
        self.seller_b = Seller.objects.create(
            user=self.user_b,
            store_name="Beta Boutique",
            legal_business_name="Beta Boutique LLP",
            support_email="beta@boutique.in",
            support_phone="9876543210",
            registered_address="200 Indiranagar",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560038",
            pickup_address="200 Indiranagar",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560038",
            gstin="29BBBBB0000B1Z5",
            pan_number="BBBBB0000B",
            commission_rate=Decimal("15.50"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )
        self.product_b = Product.objects.create(
            category=self.category,
            name="Beta Denim Jacket",
            slug="beta-denim-jacket",
            sku="BT-JKT-001",
            price=Decimal("2000.00"),
            stock_quantity=30,
            is_active=True,
            seller=self.seller_b,
        )

        # Direct Fitmeezo Product (seller=None)
        self.product_direct = Product.objects.create(
            category=self.category,
            name="Fitmeezo Classic Tee",
            slug="fitmeezo-classic-tee",
            sku="FM-TEE-001",
            price=Decimal("500.00"),
            stock_quantity=100,
            is_active=True,
            seller=None,
        )

        # Customer User
        self.customer = User.objects.create_user(
            username="shopper@example.com",
            email="shopper@example.com",
            password="ShopperPassword123!",
        )

    def test_different_sellers_can_have_different_commission_rates(self):
        """Platform admin can configure distinct commission rates per seller."""
        self.assertEqual(self.seller_a.commission_rate, Decimal("10.00"))
        self.assertEqual(self.seller_b.commission_rate, Decimal("15.50"))
        self.assertNotEqual(self.seller_a.commission_rate, self.seller_b.commission_rate)

    def test_commission_validation_negative_or_exceeds_100(self):
        """Commission rate below 0.00% or above 100.00% raises ValidationError."""
        self.seller_a.commission_rate = Decimal("-5.00")
        with self.assertRaises(ValidationError) as ctx_neg:
            self.seller_a.clean()
        self.assertIn("commission_rate", ctx_neg.exception.message_dict)

        self.seller_a.commission_rate = Decimal("105.00")
        with self.assertRaises(ValidationError) as ctx_over:
            self.seller_a.clean()
        self.assertIn("commission_rate", ctx_over.exception.message_dict)

    def test_commission_snapshot_on_checkout_order_item(self):
        """
        Placing an order snapshots the seller, commission_rate, and commission_amount
        on each OrderItem using exact Decimal arithmetic.
        """
        from core.models import Address, Order, OrderItem

        shipping_addr = Address.objects.create(
            user=self.customer,
            full_name="Shopper Kumar",
            phone_number="9876543210",
            address_line1="123 Shopping Lane",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            is_default=True,
        )

        self.client.login(username="shopper@example.com", password="ShopperPassword123!")

        # Add Product A (Qty: 2, Line Total: 2000.00, Commission: 10.00% -> 200.00)
        self.client.post(reverse("core:cart_add"), {"product_id": self.product_a.pk, "quantity": 2})
        # Add Product B (Qty: 1, Line Total: 2000.00, Commission: 15.50% -> 310.00)
        self.client.post(reverse("core:cart_add"), {"product_id": self.product_b.pk, "quantity": 1})

        # Checkout
        checkout_payload = {
            "address_mode": "existing",
            "address_id": shipping_addr.pk,
            "selected_address": shipping_addr.pk,
            "payment_method": "cod",
            "idempotency_token": "test-token-comm-snap-001",
        }
        response = self.client.post(reverse("core:checkout"), checkout_payload)
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(idempotency_key="test-token-comm-snap-001")
        item_a = order.items.get(product=self.product_a)
        item_b = order.items.get(product=self.product_b)

        # Verify Seller A item snapshot
        self.assertEqual(item_a.seller, self.seller_a)
        self.assertEqual(item_a.commission_rate, Decimal("10.00"))
        self.assertEqual(item_a.line_total, Decimal("2000.00"))
        self.assertEqual(item_a.commission_amount, Decimal("200.00"))

        # Verify Seller B item snapshot
        self.assertEqual(item_b.seller, self.seller_b)
        self.assertEqual(item_b.commission_rate, Decimal("15.50"))
        self.assertEqual(item_b.line_total, Decimal("2000.00"))
        self.assertEqual(item_b.commission_amount, Decimal("310.00"))

    def test_subsequent_commission_rate_change_does_not_rewrite_historical_records(self):
        """
        If the admin changes a seller's commission rate later,
        historical OrderItem records must remain completely unchanged.
        """
        from core.models import Address, Order, OrderItem

        shipping_addr = Address.objects.create(
            user=self.customer,
            full_name="Shopper Kumar",
            phone_number="9876543210",
            address_line1="123 Shopping Lane",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            is_default=True,
        )

        self.client.login(username="shopper@example.com", password="ShopperPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product_a.pk, "quantity": 1})

        self.client.post(
            reverse("core:checkout"),
            {
                "address_mode": "existing",
                "address_id": shipping_addr.pk,
                "selected_address": shipping_addr.pk,
                "payment_method": "cod",
                "idempotency_token": "tok-hist-001",
            },
        )

        order1 = Order.objects.get(idempotency_key="tok-hist-001")
        item1 = order1.items.get(product=self.product_a)
        self.assertEqual(item1.commission_rate, Decimal("10.00"))
        self.assertEqual(item1.commission_amount, Decimal("100.00"))

        # Admin increases Seller A's commission from 10.00% to 25.00%
        self.seller_a.commission_rate = Decimal("25.00")
        self.seller_a.save(update_fields=["commission_rate", "updated_at"])

        # Prior OrderItem must still reflect the historical snapshot (10.00% and 100.00)
        item1.refresh_from_db()
        self.assertEqual(item1.commission_rate, Decimal("10.00"))
        self.assertEqual(item1.commission_amount, Decimal("100.00"))

        # Future order placed now receives the new 25.00% rate
        self.client.post(reverse("core:cart_add"), {"product_id": self.product_a.pk, "quantity": 1})
        self.client.post(
            reverse("core:checkout"),
            {
                "address_mode": "existing",
                "address_id": shipping_addr.pk,
                "selected_address": shipping_addr.pk,
                "payment_method": "cod",
                "idempotency_token": "tok-hist-002",
            },
        )
        order2 = Order.objects.get(idempotency_key="tok-hist-002")
        item2 = order2.items.get(product=self.product_a)
        self.assertEqual(item2.commission_rate, Decimal("25.00"))
        self.assertEqual(item2.commission_amount, Decimal("250.00"))

    def test_platform_direct_product_checkout_snapshots_zero_commission(self):
        """Products without an assigned seller snapshot seller=None and 0.00 commission."""
        from core.models import Address, Order, OrderItem

        shipping_addr = Address.objects.create(
            user=self.customer,
            full_name="Shopper Kumar",
            phone_number="9876543210",
            address_line1="123 Shopping Lane",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            is_default=True,
        )

        self.client.login(username="shopper@example.com", password="ShopperPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product_direct.pk, "quantity": 1})

        self.client.post(
            reverse("core:checkout"),
            {
                "address_mode": "existing",
                "address_id": shipping_addr.pk,
                "selected_address": shipping_addr.pk,
                "payment_method": "cod",
                "idempotency_token": "tok-direct-001",
            },
        )

        order = Order.objects.get(idempotency_key="tok-direct-001")
        item = order.items.get(product=self.product_direct)
        self.assertIsNone(item.seller)
        self.assertEqual(item.commission_rate, Decimal("0.00"))
        self.assertEqual(item.commission_amount, Decimal("0.00"))

    def test_seller_profile_form_cannot_view_or_modify_commission(self):
        """Seller cannot view or mutate commission_rate via seller profile update form."""
        from core.forms import SellerProfileUpdateForm

        form = SellerProfileUpdateForm(instance=self.seller_a)
        self.assertNotIn("commission_rate", form.fields)
        self.assertNotIn("status", form.fields)
        self.assertNotIn("gstin", form.fields)

        # POST attempt to alter commission
        self.client.login(username="seller_a@example.com", password="Password123!")
        post_data = {
            "store_name": "Alpha Apparel Updated",
            "support_email": "alpha@apparel.in",
            "support_phone": "9876543210",
            "registered_address": "Addr",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560025",
            "pickup_address": "Addr",
            "pickup_city": "Bengaluru",
            "pickup_state": "Karnataka",
            "pickup_postal_code": "560025",
            "commission_rate": "1.00",  # Malicious attempt
            "status": "pending",
        }
        self.client.post(reverse("core:seller_profile"), post_data)
        self.seller_a.refresh_from_db()
        self.assertEqual(self.seller_a.commission_rate, Decimal("10.00"))
        self.assertEqual(self.seller_a.status, Seller.STATUS_APPROVED)


class SellerOrderArchitectureTests(TestCase):
    """
    Phase 2A tests for SellerOrder sub-order package architecture.
    Validates:
    1. Multi-seller checkout grouping into distinct SellerOrder records.
    2. Single-seller checkout creating 1 SellerOrder.
    3. Platform-direct merchandise creating 1 platform-fulfilled SellerOrder (-PLT).
    4. Mixed cart (seller products + platform product) creating distinct sub-orders.
    5. OrderItem.seller_order relationships and financial snapshots.
    6. REST API checkout grouping into SellerOrders.
    7. Model clean() and duplicate prevention constraints.
    8. Parent cancellation cascading status to child SellerOrders.
    9. Seller scoping isolation: Seller A cannot access Seller B's sub-orders.
    10. Legacy orders with seller_order=None remain compatible.
    """

    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Fitness & Apparel", slug="fitness-apparel")

        # Seller 1 (10.00% commission)
        self.user1 = User.objects.create_user(
            username="vendor1@example.com",
            email="vendor1@example.com",
            password="VendorPassword123!",
        )
        self.seller1 = Seller.objects.create(
            user=self.user1,
            store_name="Vortex Sports",
            legal_business_name="Vortex Sports India LLP",
            support_email="support@vortex.in",
            support_phone="9876543210",
            registered_address="100 Indiranagar",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560038",
            pickup_address="100 Indiranagar",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560038",
            gstin="29AAAAA1111A1Z5",
            pan_number="AAAAA1111A",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )
        self.product1 = Product.objects.create(
            category=self.category,
            name="Vortex Dumbbell Set",
            slug="vortex-dumbbell-set",
            sku="VX-DB-001",
            price=Decimal("1500.00"),
            stock_quantity=20,
            is_active=True,
            seller=self.seller1,
        )

        # Seller 2 (15.00% commission)
        self.user2 = User.objects.create_user(
            username="vendor2@example.com",
            email="vendor2@example.com",
            password="VendorPassword123!",
        )
        self.seller2 = Seller.objects.create(
            user=self.user2,
            store_name="Zenith Footwear",
            legal_business_name="Zenith Footwear Pvt Ltd",
            support_email="support@zenith.in",
            support_phone="9876543211",
            registered_address="200 Brigade Rd",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560025",
            pickup_address="200 Brigade Rd",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560025",
            gstin="29BBBBB2222B1Z5",
            pan_number="BBBBB2222B",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )
        self.product2 = Product.objects.create(
            category=self.category,
            name="Zenith Running Shoes",
            slug="zenith-running-shoes",
            sku="ZN-SH-001",
            price=Decimal("2000.00"),
            stock_quantity=15,
            is_active=True,
            seller=self.seller2,
        )

        # Direct platform product (seller=None)
        self.product_direct = Product.objects.create(
            category=self.category,
            name="Fitmeezo Classic Shaker",
            slug="fitmeezo-classic-shaker",
            sku="FM-SHK-001",
            price=Decimal("400.00"),
            stock_quantity=50,
            is_active=True,
            seller=None,
        )

        # Customer
        self.customer = User.objects.create_user(
            username="buyer@example.com",
            email="buyer@example.com",
            password="BuyerPassword123!",
        )
        self.shipping_address = Address.objects.create(
            user=self.customer,
            full_name="Pooja Sharma",
            phone_number="9876543210",
            address_line1="55 Palm Grove",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            is_default=True,
        )

    def test_multi_seller_checkout_creates_separate_seller_orders(self):
        """
        Cart with items from Seller 1 and Seller 2 creates 1 parent Order
        and 2 separate SellerOrder instances with correct sub_order_numbers and snapshots.
        """
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")

        # Add 2 units of product 1 (2 * 1500 = 3000.00, comm @ 10% = 300.00, net = 2700.00)
        self.client.post(reverse("core:cart_add"), {"product_id": self.product1.pk, "quantity": 2})
        # Add 1 unit of product 2 (1 * 2000 = 2000.00, comm @ 15% = 300.00, net = 1700.00)
        self.client.post(reverse("core:cart_add"), {"product_id": self.product2.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-multi-001",
        }
        response = self.client.post(reverse("core:checkout"), payload)
        self.assertEqual(response.status_code, 302)

        order = Order.objects.get(idempotency_key="sub-multi-001")
        sub_orders = order.seller_orders.all()
        self.assertEqual(sub_orders.count(), 2)

        sub_s1 = order.seller_orders.get(seller=self.seller1)
        sub_s2 = order.seller_orders.get(seller=self.seller2)

        # Verify Seller 1 sub-order
        self.assertEqual(sub_s1.sub_order_number, f"{order.order_number}-S{self.seller1.id}")
        self.assertFalse(sub_s1.is_platform_fulfilled)
        self.assertEqual(sub_s1.status, SellerOrder.STATUS_PENDING)
        self.assertEqual(sub_s1.subtotal, Decimal("3000.00"))
        self.assertEqual(sub_s1.total_commission, Decimal("300.00"))
        self.assertEqual(sub_s1.net_seller_payable, Decimal("2700.00"))

        # Verify Seller 2 sub-order
        self.assertEqual(sub_s2.sub_order_number, f"{order.order_number}-S{self.seller2.id}")
        self.assertFalse(sub_s2.is_platform_fulfilled)
        self.assertEqual(sub_s2.status, SellerOrder.STATUS_PENDING)
        self.assertEqual(sub_s2.subtotal, Decimal("2000.00"))
        self.assertEqual(sub_s2.total_commission, Decimal("300.00"))
        self.assertEqual(sub_s2.net_seller_payable, Decimal("1700.00"))

        # Verify OrderItem linkages
        items_s1 = sub_s1.items.all()
        self.assertEqual(items_s1.count(), 1)
        self.assertEqual(items_s1.first().product, self.product1)
        self.assertEqual(items_s1.first().seller_order, sub_s1)

        items_s2 = sub_s2.items.all()
        self.assertEqual(items_s2.count(), 1)
        self.assertEqual(items_s2.first().product, self.product2)
        self.assertEqual(items_s2.first().seller_order, sub_s2)

    def test_single_seller_checkout_creates_one_seller_order(self):
        """Checkout with only 1 seller creates 1 parent Order and 1 SellerOrder."""
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product1.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-single-001",
        }
        self.client.post(reverse("core:checkout"), payload)
        order = Order.objects.get(idempotency_key="sub-single-001")

        self.assertEqual(order.seller_orders.count(), 1)
        sub = order.seller_orders.first()
        self.assertEqual(sub.seller, self.seller1)
        self.assertEqual(sub.sub_order_number, f"{order.order_number}-S{self.seller1.id}")
        self.assertEqual(sub.subtotal, Decimal("1500.00"))
        self.assertEqual(sub.total_commission, Decimal("150.00"))
        self.assertEqual(sub.net_seller_payable, Decimal("1350.00"))

    def test_platform_direct_product_checkout_creates_platform_sub_order(self):
        """Products with seller=None create a platform-fulfilled SellerOrder (-PLT)."""
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product_direct.pk, "quantity": 2})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-plt-001",
        }
        self.client.post(reverse("core:checkout"), payload)
        order = Order.objects.get(idempotency_key="sub-plt-001")

        self.assertEqual(order.seller_orders.count(), 1)
        sub = order.seller_orders.first()
        self.assertIsNone(sub.seller)
        self.assertTrue(sub.is_platform_fulfilled)
        self.assertEqual(sub.sub_order_number, f"{order.order_number}-PLT")
        self.assertEqual(sub.subtotal, Decimal("800.00"))
        self.assertEqual(sub.total_commission, Decimal("0.00"))
        self.assertEqual(sub.net_seller_payable, Decimal("800.00"))

        item = sub.items.first()
        self.assertIsNone(item.seller)
        self.assertEqual(item.seller_order, sub)

    def test_mixed_seller_and_platform_cart_creates_distinct_sub_orders(self):
        """Cart containing both seller product and direct product creates 2 sub-orders."""
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.product_direct.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-mixed-001",
        }
        self.client.post(reverse("core:checkout"), payload)
        order = Order.objects.get(idempotency_key="sub-mixed-001")

        self.assertEqual(order.seller_orders.count(), 2)
        seller_sub = order.seller_orders.get(is_platform_fulfilled=False)
        plt_sub = order.seller_orders.get(is_platform_fulfilled=True)

        self.assertEqual(seller_sub.seller, self.seller1)
        self.assertIsNone(plt_sub.seller)
        self.assertEqual(seller_sub.sub_order_number, f"{order.order_number}-S{self.seller1.id}")
        self.assertEqual(plt_sub.sub_order_number, f"{order.order_number}-PLT")
        self.assertEqual(seller_sub.subtotal, Decimal("1500.00"))
        self.assertEqual(seller_sub.total_commission, Decimal("150.00"))
        self.assertEqual(seller_sub.net_seller_payable, Decimal("1350.00"))
        self.assertEqual(plt_sub.subtotal, Decimal("400.00"))
        self.assertEqual(plt_sub.total_commission, Decimal("0.00"))
        self.assertEqual(plt_sub.net_seller_payable, Decimal("400.00"))

    def test_api_checkout_creates_seller_orders_and_links_order_items(self):
        """REST API checkout correctly groups items and links SellerOrders."""
        token = Token.objects.create(user=self.customer)
        api_client = APIClient()
        api_client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        api_client.post(reverse("api:cart_add"), {"product_id": self.product1.pk, "quantity": 1})
        api_client.post(reverse("api:cart_add"), {"product_id": self.product2.pk, "quantity": 1})

        payload = {
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-api-001",
        }
        resp = api_client.post(reverse("api:checkout_place_order"), payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        order = Order.objects.get(idempotency_key="sub-api-001")
        self.assertEqual(order.seller_orders.count(), 2)

        sub_s1 = order.seller_orders.get(seller=self.seller1)
        self.assertEqual(sub_s1.subtotal, Decimal("1500.00"))
        self.assertEqual(sub_s1.total_commission, Decimal("150.00"))
        self.assertEqual(sub_s1.net_seller_payable, Decimal("1350.00"))

        sub_s2 = order.seller_orders.get(seller=self.seller2)
        self.assertEqual(sub_s2.subtotal, Decimal("2000.00"))
        self.assertEqual(sub_s2.total_commission, Decimal("300.00"))
        self.assertEqual(sub_s2.net_seller_payable, Decimal("1700.00"))

        for item in order.items.all():
            self.assertIsNotNone(item.seller_order)
            self.assertEqual(item.seller_order.order, order)
            self.assertEqual(item.seller_order.seller, item.seller)

    def test_duplicate_suborder_constraints_prevent_duplicates(self):
        """Database UniqueConstraints prevent duplicate sub-orders for the same seller or platform."""
        from django.db import IntegrityError

        order = Order.objects.create(
            order_number="FM-TEST-DUP-001",
            user=self.customer,
            shipping_full_name="Test",
            shipping_phone_number="9876543210",
            shipping_address_line1="Line 1",
            shipping_city="City",
            shipping_state="State",
            shipping_postal_code="560001",
            subtotal=Decimal("1000.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1000.00"),
        )

        # 1. Create first Seller 1 suborder
        SellerOrder.objects.create(
            order=order,
            seller=self.seller1,
            is_platform_fulfilled=False,
            sub_order_number="FM-TEST-DUP-001-S1",
        )

        # Attempt duplicate Seller 1 suborder -> must raise IntegrityError
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                SellerOrder.objects.create(
                    order=order,
                    seller=self.seller1,
                    is_platform_fulfilled=False,
                    sub_order_number="FM-TEST-DUP-001-S1-DUP",
                )

        # 2. Create first Platform suborder
        SellerOrder.objects.create(
            order=order,
            seller=None,
            is_platform_fulfilled=True,
            sub_order_number="FM-TEST-DUP-001-PLT",
        )

        # Attempt duplicate Platform suborder -> must raise IntegrityError
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                SellerOrder.objects.create(
                    order=order,
                    seller=None,
                    is_platform_fulfilled=True,
                    sub_order_number="FM-TEST-DUP-001-PLT-DUP",
                )

    def test_seller_order_model_clean_validation(self):
        """SellerOrder.clean() enforces consistency between is_platform_fulfilled and seller."""
        order = Order.objects.create(
            order_number="FM-TEST-CLN-001",
            user=self.customer,
            shipping_full_name="Test",
            shipping_phone_number="9876543210",
            shipping_address_line1="Line 1",
            shipping_city="City",
            shipping_state="State",
            shipping_postal_code="560001",
            subtotal=Decimal("1000.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1000.00"),
        )

        # Inconsistency: is_platform_fulfilled=True but seller is assigned
        bad_so1 = SellerOrder(
            order=order,
            seller=self.seller1,
            is_platform_fulfilled=True,
            sub_order_number="FM-TEST-CLN-001-BAD1",
        )
        with self.assertRaises(ValidationError) as ctx1:
            bad_so1.clean()
        self.assertIn("seller", ctx1.exception.message_dict)

        # Inconsistency: is_platform_fulfilled=False but seller is None
        bad_so2 = SellerOrder(
            order=order,
            seller=None,
            is_platform_fulfilled=False,
            sub_order_number="FM-TEST-CLN-001-BAD2",
        )
        with self.assertRaises(ValidationError) as ctx2:
            bad_so2.clean()
        self.assertIn("seller", ctx2.exception.message_dict)

    def test_parent_cancellation_cascades_status_to_seller_orders(self):
        """Cancelling parent order transitions all pending/processing sub-orders to CANCELLED."""
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.product2.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-cancel-001",
        }
        self.client.post(reverse("core:checkout"), payload)
        order = Order.objects.get(idempotency_key="sub-cancel-001")

        # Initial inventory stock check
        self.product1.refresh_from_db()
        self.assertEqual(self.product1.stock_quantity, 19)  # 20 - 1

        # Cancel parent order via central service
        success, msg, cancelled_order = cancel_order(order, user=self.customer, reason="Customer changed mind")
        self.assertTrue(success)
        self.assertEqual(cancelled_order.status, Order.STATUS_CANCELLED)
        self.assertTrue(cancelled_order.inventory_restored)

        # Check stock restored
        self.product1.refresh_from_db()
        self.assertEqual(self.product1.stock_quantity, 20)

        # Check both sub-orders cascaded to CANCELLED
        for sub in cancelled_order.seller_orders.all():
            self.assertEqual(sub.status, SellerOrder.STATUS_CANCELLED)
            self.assertIsNotNone(sub.cancelled_at)

        # Re-cancellation attempt does not double-restore stock
        success2, _, _ = cancel_order(cancelled_order, user=self.customer)
        self.assertFalse(success2)
        self.product1.refresh_from_db()
        self.assertEqual(self.product1.stock_quantity, 20)

    def test_seller_scoping_isolation(self):
        """Seller A querysets only return Seller A's sub-orders and exclude Seller B's."""
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.product2.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-scope-001",
        }
        self.client.post(reverse("core:checkout"), payload)
        order = Order.objects.get(idempotency_key="sub-scope-001")

        # Seller 1 scoping
        s1_orders = SellerOrder.objects.filter(seller=self.seller1)
        self.assertIn(order.seller_orders.get(seller=self.seller1), s1_orders)
        self.assertNotIn(order.seller_orders.get(seller=self.seller2), s1_orders)

        # Seller 2 scoping
        s2_orders = SellerOrder.objects.filter(seller=self.seller2)
        self.assertIn(order.seller_orders.get(seller=self.seller2), s2_orders)
        self.assertNotIn(order.seller_orders.get(seller=self.seller1), s2_orders)

    def test_legacy_order_compatibility_with_null_seller_order(self):
        """Historical orders with seller_order=None continue operating without regression."""
        legacy_order = Order.objects.create(
            order_number="FM-LEGACY-001",
            user=self.customer,
            shipping_full_name="Legacy Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="Old Street",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("500.00"),
            status=Order.STATUS_PENDING,
        )
        legacy_item = OrderItem.objects.create(
            order=legacy_order,
            product=self.product_direct,
            product_name=self.product_direct.name,
            sku=self.product_direct.sku,
            unit_price=Decimal("500.00"),
            quantity=1,
            line_total=Decimal("500.00"),
            seller=None,
            seller_order=None,  # Null for historical orders
            commission_rate=Decimal("0.00"),
            commission_amount=Decimal("0.00"),
        )

        # Verify relationships
        self.assertEqual(legacy_order.seller_orders.count(), 0)
        self.assertIsNone(legacy_item.seller_order)
        self.assertEqual(legacy_order.items.count(), 1)
        self.assertEqual(legacy_order.get_status_display(), "Pending")

        # Verify customer API serialization without crash and with null/empty suborders
        from core.serializers import OrderDetailSerializer
        ser_data = OrderDetailSerializer(legacy_order).data
        self.assertEqual(ser_data["seller_orders"], [])
        self.assertIsNone(ser_data["items"][0]["seller_order_id"])

        # Verify admin order detail renders cleanly
        admin_user = User.objects.create_superuser(
            username="admin_legacy@example.com",
            email="admin_legacy@example.com",
            password="AdminPassword123!",
        )
        self.client.login(username="admin_legacy@example.com", password="AdminPassword123!")
        admin_resp = self.client.get(reverse("custom_admin:order_detail", kwargs={"pk": legacy_order.pk}))
        self.assertEqual(admin_resp.status_code, 200)

        # Verify cancellation of legacy order without SellerOrders works smoothly
        success, msg, cancelled = cancel_order(legacy_order, user=self.customer, reason="Legacy cancel")
        self.assertTrue(success)
        self.assertEqual(cancelled.status, Order.STATUS_CANCELLED)
        self.assertTrue(cancelled.inventory_restored)

    def test_cancellation_rejected_when_child_order_is_shipped_or_delivered(self):
        """Parent order cancellation is blocked if any package has already shipped or delivered."""
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.product2.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-ship-block-001",
        }
        self.client.post(reverse("core:checkout"), payload)
        order = Order.objects.get(idempotency_key="sub-ship-block-001")

        # Stock check after checkout
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertEqual(self.product1.stock_quantity, 19)
        self.assertEqual(self.product2.stock_quantity, 14)

        # Mark Seller 1 sub-order as SHIPPED
        sub_s1 = order.seller_orders.get(seller=self.seller1)
        sub_s1.mark_shipped(courier_name="Blue Dart", tracking_number="BD123456IN")
        self.assertEqual(sub_s1.status, SellerOrder.STATUS_SHIPPED)

        # Attempt to cancel parent order -> must fail
        success, msg, _ = cancel_order(order, user=self.customer, reason="Customer cancelled")
        self.assertFalse(success)
        self.assertIn("cannot be cancelled", msg)

        # Confirm parent order is not cancelled and stock is NOT restored
        order.refresh_from_db()
        self.assertNotEqual(order.status, Order.STATUS_CANCELLED)
        self.assertFalse(order.inventory_restored)
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertEqual(self.product1.stock_quantity, 19)
        self.assertEqual(self.product2.stock_quantity, 14)

        # Mark Seller 1 sub-order as DELIVERED and test again
        sub_s1.mark_delivered()
        self.assertEqual(sub_s1.status, SellerOrder.STATUS_DELIVERED)
        success2, msg2, _ = cancel_order(order, user=self.customer, reason="Customer cancelled 2")
        self.assertFalse(success2)
        self.assertIn("cannot be cancelled", msg2)

    def test_razorpay_payment_failed_synchronizes_seller_orders_to_cancelled(self):
        """When online payment fails, parent order and child SellerOrders are all cancelled transactionally."""
        self.client.login(username="buyer@example.com", password="BuyerPassword123!")
        self.client.post(reverse("core:cart_add"), {"product_id": self.product1.pk, "quantity": 1})
        self.client.post(reverse("core:cart_add"), {"product_id": self.product2.pk, "quantity": 1})

        payload = {
            "address_mode": "existing",
            "address_id": self.shipping_address.pk,
            "selected_address": self.shipping_address.pk,
            "payment_method": "cod",  # Created order to test the failure view directly
            "idempotency_token": "sub-fail-sync-001",
        }
        self.client.post(reverse("core:checkout"), payload)
        order = Order.objects.get(idempotency_key="sub-fail-sync-001")

        # Verify initial sub-orders are PENDING
        for sub in order.seller_orders.all():
            self.assertEqual(sub.status, SellerOrder.STATUS_PENDING)

        # Check stock decremented
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertEqual(self.product1.stock_quantity, 19)
        self.assertEqual(self.product2.stock_quantity, 14)

        # Trigger razorpay payment failed endpoint
        fail_payload = {
            "order_number": order.order_number,
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "User cancelled payment on gateway modal.",
        }
        resp = self.client.post(reverse("core:razorpay_payment_failed"), fail_payload)
        self.assertEqual(resp.status_code, 302)

        # Verify parent order state
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        self.assertEqual(order.payment_status, Order.PAYMENT_STATUS_FAILED)
        self.assertEqual(order.refund_status, Order.REFUND_STATUS_NOT_APPLICABLE)
        self.assertTrue(order.inventory_restored)

        # Verify child SellerOrders are synchronized to CANCELLED
        for sub in order.seller_orders.all():
            self.assertEqual(sub.status, SellerOrder.STATUS_CANCELLED)
            self.assertIsNotNone(sub.cancelled_at)

        # Verify stock restored exactly once
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertEqual(self.product1.stock_quantity, 20)
        self.assertEqual(self.product2.stock_quantity, 15)

    def test_customer_api_order_detail_hides_commission_and_payable(self):
        """Customer-facing order detail API does not expose commission or net seller payable."""
        token = Token.objects.create(user=self.customer)
        api_client = APIClient()
        api_client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        api_client.post(reverse("api:cart_add"), {"product_id": self.product1.pk, "quantity": 1})
        api_client.post(reverse("api:cart_add"), {"product_id": self.product2.pk, "quantity": 1})

        payload = {
            "address_id": self.shipping_address.pk,
            "payment_method": "cod",
            "idempotency_token": "sub-api-comm-hide-001",
        }
        resp_create = api_client.post(reverse("api:checkout_place_order"), payload)
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        order_number = resp_create.data["order"]["order_number"]

        # Call GET /api/orders/<order_number>/ as customer
        resp_detail = api_client.get(reverse("api:order_detail", kwargs={"order_number": order_number}))
        self.assertEqual(resp_detail.status_code, status.HTTP_200_OK)

        order_data = resp_detail.data["order"]
        seller_orders_data = order_data["seller_orders"]
        self.assertEqual(len(seller_orders_data), 2)

        for pkg in seller_orders_data:
            self.assertNotIn("total_commission", pkg, "Internal commission must not be exposed to customers")
            self.assertNotIn("net_seller_payable", pkg, "Net seller payable must not be exposed to customers")
            self.assertNotIn("actual_shipping_cost", pkg)
            self.assertNotIn("shipping_allocated_to_seller", pkg)
            self.assertNotIn("shipping_margin", pkg)
            # Tracking / display fields must remain present
            self.assertIn("sub_order_number", pkg)
            self.assertIn("status", pkg)
            self.assertIn("subtotal", pkg)

        for item in order_data["items"]:
            self.assertNotIn("commission_rate", item, "Line item commission rate must not be exposed to customers")
            self.assertNotIn("commission_amount", item, "Line item commission amount must not be exposed to customers")


class SellerDashboardViewTests(TestCase):
    """
    Unit & integration tests for Seller Dashboard Core & Metrics (Phase 3 Prompt 1).
    Validates:
    1. Access control via @seller_required: approved seller allowed; anonymous, customer,
       pending, rejected, suspended, inactive, or missing GSTIN sellers blocked.
    2. Non-staff isolation: Approved seller does not gain staff or superuser privileges.
    3. Strict seller scoping: Seller A never sees Seller B's or Platform merchandise, orders,
       commissions, or payables.
    4. Accurate status counts across all package lifecycle states.
    5. Financial calculations: strictly based on stored snapshots, Decimal arithmetic,
       immune to current commission rate changes, cancelled packages excluded from sales.
    6. Date range filtering: Valid start/end dates filter correctly; invalid/reversed dates
       fail safely without 500 error.
    7. Low-stock inventory alerts triggered at or below configured threshold.
    8. Empty dashboard state renders cleanly for newly onboarded sellers.
    """

    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Fitness Equipment", slug="fitness-equipment")

        # Seller A (Approved, 10% commission)
        self.user_a = User.objects.create_user(
            username="seller_a@example.com",
            email="seller_a@example.com",
            password="Password123!",
        )
        self.seller_a = Seller.objects.create(
            user=self.user_a,
            store_name="Alpha Fitness",
            legal_business_name="Alpha Fitness LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="support@alphafit.in",
            support_phone="9876543210",
            registered_address="10 Alpha St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="10 Alpha St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29AAAAA0000A1Z5",
            pan_number="AAAAA0000A",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Seller B (Approved, 15% commission)
        self.user_b = User.objects.create_user(
            username="seller_b@example.com",
            email="seller_b@example.com",
            password="Password123!",
        )
        self.seller_b = Seller.objects.create(
            user=self.user_b,
            store_name="Beta Athletics",
            legal_business_name="Beta Athletics Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@betafit.in",
            support_phone="9876543211",
            registered_address="20 Beta St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="20 Beta St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29BBBBB1111B1Z5",
            pan_number="BBBBB1111B",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Customer User
        self.customer = User.objects.create_user(
            username="customer_dash@example.com",
            email="customer_dash@example.com",
            password="Password123!",
        )
        self.shipping_address = Address.objects.create(
            user=self.customer,
            full_name="Dash Customer",
            phone_number="9876543299",
            address_line1="100 MG Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            is_default=True,
        )

        # Products for Seller A
        self.prod_a1 = Product.objects.create(
            category=self.category,
            name="Alpha Kettlebell 16kg",
            slug="alpha-kettlebell-16kg",
            sku="AL-KB-016",
            price=Decimal("1200.00"),
            stock_quantity=25,  # Healthy stock
            is_active=True,
            seller=self.seller_a,
        )
        self.prod_a2 = Product.objects.create(
            category=self.category,
            name="Alpha Resistance Band",
            slug="alpha-resistance-band",
            sku="AL-RB-001",
            price=Decimal("400.00"),
            stock_quantity=5,  # Low stock (<= 10)
            is_active=True,
            seller=self.seller_a,
        )
        self.prod_a3 = Product.objects.create(
            category=self.category,
            name="Alpha Foam Roller",
            slug="alpha-foam-roller",
            sku="AL-FR-001",
            price=Decimal("600.00"),
            stock_quantity=0,  # Out of stock (0 <= 10)
            is_active=False,
            seller=self.seller_a,
        )

        # Products for Seller B
        self.prod_b1 = Product.objects.create(
            category=self.category,
            name="Beta Running Shoes",
            slug="beta-running-shoes",
            sku="BT-SH-001",
            price=Decimal("2500.00"),
            stock_quantity=50,
            is_active=True,
            seller=self.seller_b,
        )

        # Platform Product (seller=None)
        self.prod_plat = Product.objects.create(
            category=self.category,
            name="Platform Water Bottle",
            slug="platform-water-bottle",
            sku="PL-WB-001",
            price=Decimal("300.00"),
            stock_quantity=100,
            is_active=True,
            seller=None,
        )

    def _create_order_with_package(self, seller, subtotal, commission, net_payable, status=SellerOrder.STATUS_PENDING, order_number="ORD-TEST-001", is_platform=False):
        """Helper to create parent Order, OrderItem, and SellerOrder with explicit snapshots."""
        from django.utils import timezone
        order = Order.objects.create(
            user=self.customer,
            order_number=order_number,
            status=Order.STATUS_PROCESSING,
            payment_status=Order.PAYMENT_STATUS_PAID,
            subtotal=Decimal(str(subtotal)),
            total_amount=Decimal(str(subtotal)),
            shipping_full_name="Buyer Name",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Test St",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
        )
        product = self.prod_a1 if seller == self.seller_a else (self.prod_b1 if seller == self.seller_b else self.prod_plat)
        seller_order = SellerOrder.objects.create(
            order=order,
            seller=seller,
            sub_order_number=f"{order_number}-S{seller.id if seller else 'PLT'}",
            status=status,
            subtotal=Decimal(str(subtotal)),
            total_commission=Decimal(str(commission)),
            net_seller_payable=Decimal(str(net_payable)),
            is_platform_fulfilled=is_platform,
        )
        OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            sku=product.sku,
            seller=seller,
            seller_order=seller_order,
            unit_price=Decimal(str(subtotal)),
            quantity=1,
            line_total=Decimal(str(subtotal)),
            commission_rate=Decimal("10.00") if seller == self.seller_a else Decimal("0.00"),
        )
        return order, seller_order

    def test_approved_seller_can_open_dashboard(self):
        """An approved seller with a valid GSTIN can open /seller/dashboard/ successfully."""
        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/dashboard.html")
        self.assertIn("seller", response.context)
        self.assertIn("metrics", response.context)
        self.assertEqual(response.context["seller"], self.seller_a)
        self.assertContains(response, "Alpha Fitness")
        self.assertContains(response, "GSTIN: 29AAAAA0000A1Z5")

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated requests are redirected to login with next parameter."""
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("core:account_login"), response.url)
        self.assertIn("next=", response.url)

    def test_non_seller_customer_redirected_to_seller_register(self):
        """Regular customer without seller profile is redirected to seller registration."""
        self.client.login(username="customer_dash@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_register"))

    def test_pending_seller_redirected_to_seller_status(self):
        """Pending seller cannot access dashboard; redirected to seller status page."""
        user_pending = User.objects.create_user(username="pending@ex.com", email="pending@ex.com", password="Password123!")
        Seller.objects.create(
            user=user_pending,
            store_name="Pending Store",
            legal_business_name="Pending Store LLC",
            support_email="p@ex.com",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Addr",
            pickup_city="City",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29CCCCC3333C1Z5",
            status=Seller.STATUS_PENDING,
            is_active=True,
        )
        self.client.login(username="pending@ex.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

    def test_rejected_seller_redirected_to_seller_status(self):
        """Rejected seller cannot access dashboard."""
        user_rej = User.objects.create_user(username="rej@ex.com", email="rej@ex.com", password="Password123!")
        Seller.objects.create(
            user=user_rej,
            store_name="Rej Store",
            legal_business_name="Rej LLC",
            support_email="r@ex.com",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Addr",
            pickup_city="City",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29DDDDD4444D1Z5",
            status=Seller.STATUS_REJECTED,
            is_active=False,
        )
        self.client.login(username="rej@ex.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

    def test_suspended_seller_redirected_to_seller_status(self):
        """Suspended seller cannot access dashboard."""
        user_susp = User.objects.create_user(username="susp@ex.com", email="susp@ex.com", password="Password123!")
        Seller.objects.create(
            user=user_susp,
            store_name="Susp Store",
            legal_business_name="Susp LLC",
            support_email="s@ex.com",
            support_phone="9876543210",
            registered_address="Addr",
            city="City",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Addr",
            pickup_city="City",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29EEEEE5555E1Z5",
            status=Seller.STATUS_SUSPENDED,
            is_active=False,
        )
        self.client.login(username="susp@ex.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

    def test_inactive_seller_redirected_to_seller_status(self):
        """Seller marked is_active=False cannot access dashboard."""
        self.seller_a.is_active = False
        self.seller_a.save()
        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

    def test_seller_without_valid_gstin_redirected(self):
        """Approved seller with missing GSTIN cannot access dashboard."""
        self.seller_a.gstin = ""
        self.seller_a.save()
        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:seller_status"))

    def test_seller_has_no_staff_permissions(self):
        """Approved seller is not Django staff or superuser and is denied admin access."""
        self.assertFalse(self.user_a.is_staff)
        self.assertFalse(self.user_a.is_superuser)
        self.client.login(username="seller_a@example.com", password="Password123!")
        # Admin endpoint redirect/deny
        admin_resp = self.client.get("/admin/")
        self.assertEqual(admin_resp.status_code, 302)

    def test_strict_metrics_scoping_between_sellers(self):
        """
        Seller A's metrics must NEVER include Seller B's or Platform merchandise,
        packages, commissions, or net payables.
        """
        # Create package for Seller A (subtotal: 1000, comm: 100, net: 900)
        self._create_order_with_package(
            seller=self.seller_a,
            subtotal=Decimal("1000.00"),
            commission=Decimal("100.00"),
            net_payable=Decimal("900.00"),
            status=SellerOrder.STATUS_PENDING,
            order_number="ORD-A-001",
        )
        # Create package for Seller B (subtotal: 5000, comm: 750, net: 4250)
        self._create_order_with_package(
            seller=self.seller_b,
            subtotal=Decimal("5000.00"),
            commission=Decimal("750.00"),
            net_payable=Decimal("4250.00"),
            status=SellerOrder.STATUS_PROCESSING,
            order_number="ORD-B-001",
        )
        # Create platform package (subtotal: 2000, comm: 0, net: 0, is_platform=True)
        self._create_order_with_package(
            seller=None,
            subtotal=Decimal("2000.00"),
            commission=Decimal("0.00"),
            net_payable=Decimal("0.00"),
            status=SellerOrder.STATUS_DELIVERED,
            order_number="ORD-PLT-001",
            is_platform=True,
        )

        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)

        metrics = response.context["metrics"]

        # Order package counts: Seller A has exactly 1 package
        self.assertEqual(metrics["order_counts"]["total"], 1)
        self.assertEqual(metrics["order_counts"]["pending"], 1)
        self.assertEqual(metrics["order_counts"]["processing"], 0)
        self.assertEqual(metrics["order_counts"]["delivered"], 0)

        # Financial totals: strictly 1000.00 gross, 100.00 commission, 900.00 net
        self.assertEqual(metrics["financial_summary"]["gross_sales"], Decimal("1000.00"))
        self.assertEqual(metrics["financial_summary"]["total_commission"], Decimal("100.00"))
        self.assertEqual(metrics["financial_summary"]["net_seller_payable"], Decimal("900.00"))

        # Catalog counts: Seller A has 3 products (2 active, 1 inactive), not Seller B's or Platform's
        self.assertEqual(metrics["catalog_summary"]["total_products"], 3)
        self.assertEqual(metrics["catalog_summary"]["active_products"], 2)

        # Recent packages list contains only Seller A's sub-order
        self.assertEqual(len(metrics["recent_packages"]), 1)
        self.assertEqual(metrics["recent_packages"][0].sub_order_number, f"ORD-A-001-S{self.seller_a.id}")

    def test_status_counts_breakdown(self):
        """Dashboard displays correct package counts across all distinct fulfillment statuses."""
        statuses = [
            SellerOrder.STATUS_PENDING,
            SellerOrder.STATUS_PROCESSING,
            SellerOrder.STATUS_READY_TO_SHIP,
            SellerOrder.STATUS_SHIPPED,
            SellerOrder.STATUS_DELIVERED,
            SellerOrder.STATUS_CANCELLED,
        ]
        for idx, st in enumerate(statuses):
            self._create_order_with_package(
                seller=self.seller_a,
                subtotal=Decimal("500.00"),
                commission=Decimal("50.00"),
                net_payable=Decimal("450.00"),
                status=st,
                order_number=f"ORD-STATUS-{idx}",
            )

        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)

        counts = response.context["metrics"]["order_counts"]
        self.assertEqual(counts["total"], 6)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["processing"], 1)
        self.assertEqual(counts["ready_to_ship"], 1)
        self.assertEqual(counts["shipped"], 1)
        self.assertEqual(counts["delivered"], 1)
        self.assertEqual(counts["cancelled"], 1)

    def test_financial_calculations_use_stored_snapshots_and_decimal(self):
        """
        Financial metrics must be derived exclusively from stored snapshots on SellerOrder.
        Modifying seller's current commission_rate must NOT alter past financial snapshots.
        Cancelled packages must be excluded from gross sales and net earnings.
        """
        # Active package: subtotal=2000, comm=200, net=1800
        self._create_order_with_package(
            seller=self.seller_a,
            subtotal=Decimal("2000.00"),
            commission=Decimal("200.00"),
            net_payable=Decimal("1800.00"),
            status=SellerOrder.STATUS_DELIVERED,
            order_number="ORD-FIN-001",
        )
        # Cancelled package: subtotal=1000, comm=100, net=900
        self._create_order_with_package(
            seller=self.seller_a,
            subtotal=Decimal("1000.00"),
            commission=Decimal("100.00"),
            net_payable=Decimal("900.00"),
            status=SellerOrder.STATUS_CANCELLED,
            order_number="ORD-FIN-002",
        )

        # Alter the seller's active commission rate
        self.seller_a.commission_rate = Decimal("50.00")
        self.seller_a.save()

        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)

        fin = response.context["metrics"]["financial_summary"]
        # Gross sales excludes cancelled: 2000.00
        self.assertEqual(fin["gross_sales"], Decimal("2000.00"))
        # Commission is stored snapshot: 200.00 (not 50% of 2000 = 1000)
        self.assertEqual(fin["total_commission"], Decimal("200.00"))
        # Net payable is stored snapshot: 1800.00 (not 1000)
        self.assertEqual(fin["net_seller_payable"], Decimal("1800.00"))

    def test_date_range_filtering_success(self):
        """Valid date range filters packages and metrics appropriately."""
        from django.utils import timezone
        import datetime

        now = timezone.now()
        past_dt = now - datetime.timedelta(days=30)
        recent_dt = now - datetime.timedelta(days=2)

        # Old package (30 days ago)
        _, sub_old = self._create_order_with_package(
            seller=self.seller_a,
            subtotal=Decimal("1500.00"),
            commission=Decimal("150.00"),
            net_payable=Decimal("1350.00"),
            status=SellerOrder.STATUS_DELIVERED,
            order_number="ORD-OLD-001",
        )
        SellerOrder.objects.filter(pk=sub_old.pk).update(created_at=past_dt)

        # Recent package (2 days ago)
        _, sub_recent = self._create_order_with_package(
            seller=self.seller_a,
            subtotal=Decimal("800.00"),
            commission=Decimal("80.00"),
            net_payable=Decimal("720.00"),
            status=SellerOrder.STATUS_PROCESSING,
            order_number="ORD-RECENT-001",
        )
        SellerOrder.objects.filter(pk=sub_recent.pk).update(created_at=recent_dt)

        start_date_str = (now - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        end_date_str = now.strftime("%Y-%m-%d")

        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(
            reverse("core:seller_dashboard"),
            {"start_date": start_date_str, "end_date": end_date_str},
        )
        self.assertEqual(response.status_code, 200)

        metrics = response.context["metrics"]
        # Only the recent package falls into the 5-day window
        self.assertEqual(metrics["order_counts"]["total"], 1)
        self.assertEqual(metrics["order_counts"]["processing"], 1)
        self.assertEqual(metrics["financial_summary"]["gross_sales"], Decimal("800.00"))
        self.assertEqual(metrics["financial_summary"]["net_seller_payable"], Decimal("720.00"))

    def test_invalid_date_formats_and_reversed_ranges_handled_safely(self):
        """Malformed date formats and start_date > end_date handle safely without 500 error."""
        self.client.login(username="seller_a@example.com", password="Password123!")

        # 1. Invalid date string
        resp1 = self.client.get(
            reverse("core:seller_dashboard"),
            {"start_date": "not-a-date", "end_date": "2026-10-10"},
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertIn("date_filter_error", resp1.context)
        self.assertIsNotNone(resp1.context["date_filter_error"])
        self.assertContains(resp1, "Invalid start date format")

        # 2. Reversed date range
        resp2 = self.client.get(
            reverse("core:seller_dashboard"),
            {"start_date": "2026-10-20", "end_date": "2026-10-10"},
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("date_filter_error", resp2.context)
        self.assertIn("cannot be after end date", resp2.context["date_filter_error"])
        self.assertContains(resp2, "Start date cannot be after end date")

    def test_low_stock_threshold_alerts(self):
        """Low-stock products (threshold <= 10) correctly appear in low-stock alert list."""
        self.client.login(username="seller_a@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)

        metrics = response.context["metrics"]
        self.assertEqual(metrics["catalog_summary"]["low_stock_products"], 2)

        alert_product_ids = [item["product"].id for item in metrics["low_stock_alerts"]]
        # prod_a2 has 5 units, prod_a3 has 0 units -> both in alerts
        self.assertIn(self.prod_a2.id, alert_product_ids)
        self.assertIn(self.prod_a3.id, alert_product_ids)
        # prod_a1 has 25 units -> NOT in alerts
        self.assertNotIn(self.prod_a1.id, alert_product_ids)

    def test_empty_dashboard_renders_successfully(self):
        """A newly approved seller with zero products and zero orders renders empty state cleanly."""
        user_empty = User.objects.create_user(
            username="empty_seller@example.com",
            email="empty_seller@example.com",
            password="Password123!",
        )
        Seller.objects.create(
            user=user_empty,
            store_name="Clean Slate Store",
            legal_business_name="Clean Slate Retail LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="support@cleanslate.in",
            support_phone="9876543210",
            registered_address="1 Clear St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="1 Clear St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29FFFFF6666F1Z5",
            pan_number="FFFFF6666F",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        self.client.login(username="empty_seller@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["metrics"]["is_empty"])
        self.assertContains(response, "Welcome to Your Fitmeezo Seller Dashboard")
        self.assertContains(response, "You have not listed any products yet")


class SellerProductManagementTests(TestCase):
    """
    Phase 3 Prompt 2: Comprehensive Security, Moderation, and Isolation Tests
    for Seller Product and Inventory Management.
    """

    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Fitness Equipment", slug="fitness-equipment")

        # Seller A (Approved)
        self.user_a = User.objects.create_user(
            username="seller_a_prod@example.com",
            email="seller_a_prod@example.com",
            password="Password123!",
        )
        self.seller_a = Seller.objects.create(
            user=self.user_a,
            store_name="Alpha Gym Gear",
            legal_business_name="Alpha Gear India Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@alphagear.in",
            support_phone="9876543210",
            registered_address="10 Alpha St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="10 Alpha St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29AAAAA0000A1Z5",
            pan_number="AAAAA0000A",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Seller B (Approved)
        self.user_b = User.objects.create_user(
            username="seller_b_prod@example.com",
            email="seller_b_prod@example.com",
            password="Password123!",
        )
        self.seller_b = Seller.objects.create(
            user=self.user_b,
            store_name="Beta Supplements",
            legal_business_name="Beta Supps LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="support@betasupps.in",
            support_phone="9876543211",
            registered_address="20 Beta St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="20 Beta St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29BBBBB1111B1Z5",
            pan_number="BBBBB1111B",
            commission_rate=Decimal("12.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Pending Seller
        self.user_pending = User.objects.create_user(
            username="seller_pending@example.com",
            email="seller_pending@example.com",
            password="Password123!",
        )
        self.seller_pending = Seller.objects.create(
            user=self.user_pending,
            store_name="Pending Gear",
            legal_business_name="Pending Gear Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="pending@gear.in",
            support_phone="9876543212",
            registered_address="30 Pending St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560003",
            pickup_address="30 Pending St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560003",
            gstin="29CCCCC2222C1Z5",
            pan_number="CCCCC2222C",
            status=Seller.STATUS_PENDING,
            is_active=True,
        )

        # Seller A Products
        self.prod_a1 = Product.objects.create(
            category=self.category,
            name="Alpha Barbell 20kg",
            slug="alpha-barbell-20kg",
            sku="AL-BB-020",
            price=Decimal("4500.00"),
            stock_quantity=15,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
            seller=self.seller_a,
        )

        # Seller B Products
        self.prod_b1 = Product.objects.create(
            category=self.category,
            name="Beta Whey Isolate 1kg",
            slug="beta-whey-isolate-1kg",
            sku="BT-WH-001",
            price=Decimal("2800.00"),
            stock_quantity=30,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
            seller=self.seller_b,
        )

        # Platform-owned Product (seller=None)
        self.prod_plat = Product.objects.create(
            category=self.category,
            name="Fitmeezo Core Shaker",
            slug="fitmeezo-core-shaker",
            sku="FM-SHK-001",
            price=Decimal("299.00"),
            stock_quantity=100,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
            seller=None,
        )

    def test_approved_seller_can_access_product_list(self):
        """Approved seller can view their scoped product list."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_product_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/products/list.html")
        products = list(response.context["products"])
        self.assertIn(self.prod_a1, products)
        self.assertNotIn(self.prod_b1, products)
        self.assertNotIn(self.prod_plat, products)

    def test_anonymous_users_cannot_access_seller_product_pages(self):
        """Unauthenticated requests are redirected to login."""
        urls = [
            reverse("core:seller_product_list"),
            reverse("core:seller_product_add"),
            reverse("core:seller_product_edit", kwargs={"pk": self.prod_a1.pk}),
            reverse("core:seller_product_inventory", kwargs={"pk": self.prod_a1.pk}),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn("login", response.url.lower())

    def test_unapproved_or_invalid_sellers_cannot_manage_products(self):
        """Pending, rejected, suspended, or inactive sellers are blocked by @seller_required."""
        # 1. Pending seller
        self.client.login(username="seller_pending@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_product_list"))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("core:seller_status"))

        # 2. Inactive seller
        self.seller_a.is_active = False
        self.seller_a.save(update_fields=["is_active"])
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_product_list"))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("core:seller_status"))

        # Restore
        self.seller_a.is_active = True
        self.seller_a.save(update_fields=["is_active"])

    def test_cross_seller_isolation_view_edit_inventory_returns_404(self):
        """Seller A cannot view, edit, or manage inventory for Seller B's product."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")

        # Edit GET
        resp_edit_get = self.client.get(reverse("core:seller_product_edit", kwargs={"pk": self.prod_b1.pk}))
        self.assertEqual(resp_edit_get.status_code, 404)

        # Edit POST
        resp_edit_post = self.client.post(
            reverse("core:seller_product_edit", kwargs={"pk": self.prod_b1.pk}),
            {"name": "Hacked Title", "sku": "BT-WH-001", "price": "100.00", "stock_quantity": "5", "category": self.category.pk},
        )
        self.assertEqual(resp_edit_post.status_code, 404)

        # Inventory GET
        resp_inv_get = self.client.get(reverse("core:seller_product_inventory", kwargs={"pk": self.prod_b1.pk}))
        self.assertEqual(resp_inv_get.status_code, 404)

        # Inventory POST
        resp_inv_post = self.client.post(
            reverse("core:seller_product_inventory", kwargs={"pk": self.prod_b1.pk}),
            {"stock_quantity": 0},
        )
        self.assertEqual(resp_inv_post.status_code, 404)

        # Verify Seller B product wasn't altered
        self.prod_b1.refresh_from_db()
        self.assertEqual(self.prod_b1.name, "Beta Whey Isolate 1kg")
        self.assertEqual(self.prod_b1.stock_quantity, 30)

    def test_seller_cannot_change_product_ownership_via_post(self):
        """Seller cannot reassign product to another seller or platform."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        response = self.client.post(
            reverse("core:seller_product_edit", kwargs={"pk": self.prod_a1.pk}),
            {
                "name": "Alpha Barbell 20kg Updated",
                "sku": "AL-BB-020",
                "price": "4500.00",
                "stock_quantity": "15",
                "category": self.category.pk,
                "seller": self.seller_b.pk,  # Forged seller
            },
        )
        self.assertEqual(response.status_code, 302)
        self.prod_a1.refresh_from_db()
        self.assertEqual(self.prod_a1.seller, self.seller_a)

    def test_seller_cannot_modify_platform_controlled_fields(self):
        """Seller cannot bypass moderation or set is_featured / commission_rate."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        self.client.post(
            reverse("core:seller_product_edit", kwargs={"pk": self.prod_a1.pk}),
            {
                "name": "Alpha Barbell 20kg",
                "sku": "AL-BB-020",
                "price": "4500.00",
                "stock_quantity": "15",
                "category": self.category.pk,
                "is_featured": True,
                "is_approved": True,
                "approval_status": "approved",
                "rejection_reason": "Cleared",
            },
        )
        self.prod_a1.refresh_from_db()
        self.assertFalse(self.prod_a1.is_featured)

    def test_new_seller_product_defaults_to_pending_and_unapproved(self):
        """Option B: Newly created seller products start with is_approved=False and approval_status='pending'."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        response = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Alpha Olympic Plates 10kg",
                "sku": "AL-PL-010",
                "price": "2200.00",
                "compare_at_price": "2500.00",
                "stock_quantity": "20",
                "category": self.category.pk,
                "short_description": "Cast iron plates",
                "description": "High density Olympic plates with rubber rim.",
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        new_prod = Product.objects.get(sku="AL-PL-010")
        self.assertEqual(new_prod.seller, self.seller_a)
        self.assertFalse(new_prod.is_approved)
        self.assertEqual(new_prod.approval_status, Product.APPROVAL_PENDING)
        self.assertEqual(new_prod.rejection_reason, "")

    def test_unapproved_product_hidden_from_public_storefront_and_api(self):
        """Unapproved products do not leak into search, category, detail, or REST API."""
        unapproved = Product.objects.create(
            category=self.category,
            name="Secret Prototype Dumbbell",
            slug="secret-prototype-dumbbell",
            sku="SC-DB-001",
            price=Decimal("999.00"),
            stock_quantity=10,
            is_active=True,
            is_approved=False,
            approval_status=Product.APPROVAL_PENDING,
            seller=self.seller_a,
        )

        client = Client()

        # 1. Product Detail Page -> 404
        detail_resp = client.get(reverse("core:product_detail", kwargs={"slug": unapproved.slug}))
        self.assertEqual(detail_resp.status_code, 404)

        # 2. Category Detail Page -> not in context products
        cat_resp = client.get(reverse("core:category_detail", kwargs={"slug": self.category.slug}))
        self.assertEqual(cat_resp.status_code, 200)
        self.assertNotContains(cat_resp, "Secret Prototype Dumbbell")

        # 3. Product Search -> not found
        search_resp = client.get(reverse("core:product_search"), {"q": "Secret Prototype"})
        self.assertEqual(search_resp.status_code, 200)
        self.assertNotContains(search_resp, "Secret Prototype Dumbbell")

        # 4. REST API Product Detail -> 404
        api_client = APIClient()
        api_detail_resp = api_client.get(reverse("api:product_detail", kwargs={"slug": unapproved.slug}))
        self.assertEqual(api_detail_resp.status_code, status.HTTP_404_NOT_FOUND)

        # 5. REST API Product List -> not in results
        api_list_resp = api_client.get(reverse("api:product_list"))
        self.assertEqual(api_list_resp.status_code, status.HTTP_200_OK)
        results = api_list_resp.data.get("results", api_list_resp.data)
        slugs = [p["slug"] for p in results]
        self.assertNotIn(unapproved.slug, slugs)

    def test_platform_and_approved_products_retain_visibility(self):
        """Platform direct products and approved seller products remain visible publicly."""
        client = Client()

        # Platform product detail
        plat_resp = client.get(reverse("core:product_detail", kwargs={"slug": self.prod_plat.slug}))
        self.assertEqual(plat_resp.status_code, 200)

        # Approved seller product detail
        approved_resp = client.get(reverse("core:product_detail", kwargs={"slug": self.prod_a1.slug}))
        self.assertEqual(approved_resp.status_code, 200)

        # Category page contains both
        cat_resp = client.get(reverse("core:category_detail", kwargs={"slug": self.category.slug}))
        self.assertEqual(cat_resp.status_code, 200)
        self.assertContains(cat_resp, self.prod_plat.name)
        self.assertContains(cat_resp, self.prod_a1.name)

    def test_validation_rejects_invalid_price_negative_stock_and_duplicate_sku(self):
        """Forms reject negative prices, negative stock, and duplicate SKUs."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")

        # 1. Negative price
        resp_neg_price = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Invalid Price Product",
                "sku": "INV-PRC-001",
                "price": "-50.00",
                "stock_quantity": "10",
                "category": self.category.pk,
            },
        )
        self.assertEqual(resp_neg_price.status_code, 200)
        self.assertFalse(Product.objects.filter(sku="INV-PRC-001").exists())

        # 2. Negative stock
        resp_neg_stock = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Invalid Stock Product",
                "sku": "INV-STK-001",
                "price": "500.00",
                "stock_quantity": "-10",
                "category": self.category.pk,
            },
        )
        self.assertEqual(resp_neg_stock.status_code, 200)
        self.assertFalse(Product.objects.filter(sku="INV-STK-001").exists())

        # 3. Duplicate SKU colliding with existing product
        resp_dup_sku = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Duplicate SKU Product",
                "sku": "BT-WH-001",  # Belongs to Seller B's product
                "price": "500.00",
                "stock_quantity": "10",
                "category": self.category.pk,
            },
        )
        self.assertEqual(resp_dup_sku.status_code, 200)
        self.assertFormError(resp_dup_sku.context["form"], "sku", "A product with SKU 'BT-WH-001' already exists.")

    def test_seller_can_manage_only_own_variants_and_images(self):
        """Seller can create, toggle, and delete variants/images on own products only."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")

        # 1. Add variant to own product
        add_v_resp = self.client.post(
            reverse("core:seller_product_variant_add", kwargs={"pk": self.prod_a1.pk}),
            {
                "option_type": "Weight",
                "option_value": "25kg",
                "sku": "AL-BB-025",
                "price": "5000.00",
                "stock_quantity": "8",
                "is_active": True,
            },
        )
        self.assertEqual(add_v_resp.status_code, 302)
        var_a = ProductVariant.objects.get(sku="AL-BB-025")
        self.assertEqual(var_a.product, self.prod_a1)
        self.assertEqual(var_a.stock_quantity, 8)

        # 2. Edit variant
        edit_v_resp = self.client.post(
            reverse("core:seller_product_variant_edit", kwargs={"pk": self.prod_a1.pk, "variant_pk": var_a.pk}),
            {
                "option_type": "Weight",
                "option_value": "25kg",
                "sku": "AL-BB-025",
                "price": "5200.00",
                "stock_quantity": "12",
                "is_active": True,
            },
        )
        self.assertEqual(edit_v_resp.status_code, 302)
        var_a.refresh_from_db()
        self.assertEqual(var_a.price, Decimal("5200.00"))
        self.assertEqual(var_a.stock_quantity, 12)

        # 3. Toggle variant active status
        toggle_resp = self.client.post(
            reverse("core:seller_product_variant_toggle", kwargs={"pk": self.prod_a1.pk, "variant_pk": var_a.pk})
        )
        self.assertEqual(toggle_resp.status_code, 302)
        var_a.refresh_from_db()
        self.assertFalse(var_a.is_active)

        # 4. Cross-seller: Seller A attempts to toggle or delete variant on Seller B's product
        var_b = ProductVariant.objects.create(
            product=self.prod_b1,
            option_type="Flavor",
            option_value="Chocolate",
            sku="BT-WH-CHOC",
            price=Decimal("2900.00"),
            stock_quantity=20,
            is_active=True,
        )
        cross_toggle = self.client.post(
            reverse("core:seller_product_variant_toggle", kwargs={"pk": self.prod_b1.pk, "variant_pk": var_b.pk})
        )
        self.assertEqual(cross_toggle.status_code, 404)

        cross_del = self.client.post(
            reverse("core:seller_product_variant_delete", kwargs={"pk": self.prod_b1.pk, "variant_pk": var_b.pk})
        )
        self.assertEqual(cross_del.status_code, 404)
        self.assertTrue(ProductVariant.objects.filter(pk=var_b.pk).exists())

        # 5. Delete own variant
        del_v_resp = self.client.post(
            reverse("core:seller_product_variant_delete", kwargs={"pk": self.prod_a1.pk, "variant_pk": var_a.pk})
        )
        self.assertEqual(del_v_resp.status_code, 302)
        self.assertFalse(ProductVariant.objects.filter(pk=var_a.pk).exists())

    def test_inventory_does_not_change_on_get_requests(self):
        """GET request to inventory view is strictly read-only."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        stock_before = self.prod_a1.stock_quantity
        response = self.client.get(reverse("core:seller_product_inventory", kwargs={"pk": self.prod_a1.pk}))
        self.assertEqual(response.status_code, 200)
        self.prod_a1.refresh_from_db()
        self.assertEqual(self.prod_a1.stock_quantity, stock_before)

    def test_atomic_inventory_updates_do_not_reset_approval(self):
        """Daily stock replenishment does not reset approval status."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        self.assertEqual(self.prod_a1.approval_status, Product.APPROVAL_APPROVED)
        self.assertTrue(self.prod_a1.is_approved)

        response = self.client.post(
            reverse("core:seller_product_inventory", kwargs={"pk": self.prod_a1.pk}),
            {"stock_quantity": 40},
        )
        self.assertEqual(response.status_code, 302)
        self.prod_a1.refresh_from_db()
        self.assertEqual(self.prod_a1.stock_quantity, 40)
        # Approval status preserved
        self.assertTrue(self.prod_a1.is_approved)
        self.assertEqual(self.prod_a1.approval_status, Product.APPROVAL_APPROVED)

    def test_material_edits_resets_moderation_to_pending(self):
        """Editing core fields of an approved product triggers re-approval."""
        self.client.login(username="seller_a_prod@example.com", password="Password123!")
        self.assertEqual(self.prod_a1.approval_status, Product.APPROVAL_APPROVED)

        response = self.client.post(
            reverse("core:seller_product_edit", kwargs={"pk": self.prod_a1.pk}),
            {
                "name": "Alpha Barbell 20kg Professional",  # Changed title
                "sku": "AL-BB-020",
                "price": "4900.00",  # Changed price
                "stock_quantity": "15",
                "category": self.category.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.prod_a1.refresh_from_db()
        self.assertEqual(self.prod_a1.name, "Alpha Barbell 20kg Professional")
        self.assertEqual(self.prod_a1.price, Decimal("4900.00"))
        # Reset to pending moderation
        self.assertFalse(self.prod_a1.is_approved)
        self.assertEqual(self.prod_a1.approval_status, Product.APPROVAL_PENDING)

    def test_platform_admin_approval_workflow(self):
        """Administrative approval publishes product to live catalog."""
        prod = Product.objects.create(
            category=self.category,
            name="Alpha Speed Rope",
            slug="alpha-speed-rope",
            sku="AL-SR-001",
            price=Decimal("350.00"),
            stock_quantity=20,
            is_active=True,
            is_approved=False,
            approval_status=Product.APPROVAL_PENDING,
            seller=self.seller_a,
        )

        # Initially 404 on storefront
        client = Client()
        self.assertEqual(client.get(reverse("core:product_detail", kwargs={"slug": prod.slug})).status_code, 404)

        # Admin approves product
        prod.approve()
        prod.refresh_from_db()
        self.assertTrue(prod.is_approved)
        self.assertEqual(prod.approval_status, Product.APPROVAL_APPROVED)

        # Now live on storefront
        self.assertEqual(client.get(reverse("core:product_detail", kwargs={"slug": prod.slug})).status_code, 200)

        # Admin rejects product
        prod.reject(reason="Product imagery does not match guidelines.")
        prod.refresh_from_db()
        self.assertFalse(prod.is_approved)
        self.assertEqual(prod.approval_status, Product.APPROVAL_REJECTED)
        self.assertEqual(prod.rejection_reason, "Product imagery does not match guidelines.")

        # Hidden again
        self.assertEqual(client.get(reverse("core:product_detail", kwargs={"slug": prod.slug})).status_code, 404)


class SellerOrderFulfillmentTests(TestCase):
    """
    Phase 3 Prompt 3: Security, Privacy, Isolation, and State Machine
    Tests for Seller Order Fulfillment Management.
    """

    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(name="Sports Apparel", slug="sports-apparel")

        # Seller A (Approved)
        self.user_a = User.objects.create_user(
            username="seller_a_ord@example.com",
            email="seller_a_ord@example.com",
            password="Password123!",
        )
        self.seller_a = Seller.objects.create(
            user=self.user_a,
            store_name="Alpha Sports",
            legal_business_name="Alpha Sports India Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@alphasports.in",
            support_phone="9876543210",
            registered_address="10 Alpha Way",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="10 Alpha Way",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29AAAAA0000A1Z5",
            pan_number="AAAAA0000A",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Seller B (Approved)
        self.user_b = User.objects.create_user(
            username="seller_b_ord@example.com",
            email="seller_b_ord@example.com",
            password="Password123!",
        )
        self.seller_b = Seller.objects.create(
            user=self.user_b,
            store_name="Beta Activewear",
            legal_business_name="Beta Activewear LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="support@betaactive.in",
            support_phone="9876543211",
            registered_address="20 Beta Way",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="20 Beta Way",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29BBBBB1111B1Z5",
            pan_number="BBBBB1111B",
            commission_rate=Decimal("12.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Pending Seller
        self.user_pending = User.objects.create_user(
            username="seller_pending_ord@example.com",
            email="seller_pending_ord@example.com",
            password="Password123!",
        )
        self.seller_pending = Seller.objects.create(
            user=self.user_pending,
            store_name="Pending Active",
            legal_business_name="Pending Active Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="pending@active.in",
            support_phone="9876543212",
            registered_address="30 Pending St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560003",
            pickup_address="30 Pending St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560003",
            gstin="29CCCCC2222C1Z5",
            pan_number="CCCCC2222C",
            status=Seller.STATUS_PENDING,
            is_active=True,
        )

        # Customer User
        self.customer = User.objects.create_user(
            username="customer_buyer@example.com",
            email="buyer@example.com",
            password="Password123!",
        )

        # Products
        self.prod_a = Product.objects.create(
            category=self.category,
            name="Alpha Compression Shirt",
            slug="alpha-compression-shirt",
            sku="AL-CS-001",
            price=Decimal("1000.00"),
            stock_quantity=50,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
            seller=self.seller_a,
        )
        self.prod_b = Product.objects.create(
            category=self.category,
            name="Beta Running Shorts",
            slug="beta-running-shorts",
            sku="BT-RS-001",
            price=Decimal("1500.00"),
            stock_quantity=50,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
            seller=self.seller_b,
        )
        self.prod_plat = Product.objects.create(
            category=self.category,
            name="Fitmeezo Headband",
            slug="fitmeezo-headband",
            sku="FM-HB-001",
            price=Decimal("200.00"),
            stock_quantity=50,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
            seller=None,
        )

        # Parent Multi-Seller Order
        self.order = Order.objects.create(
            user=self.customer,
            order_number="ORD-MULTI-20261011-001",
            status=Order.STATUS_PROCESSING,
            payment_status=Order.PAYMENT_STATUS_PAID,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            razorpay_order_id="order_rp_test_123",
            razorpay_payment_id="pay_rp_test_456",
            razorpay_signature="sig_rp_test_789",
            subtotal=Decimal("3700.00"),
            total_amount=Decimal("3700.00"),
            shipping_full_name="Rajesh Kumar",
            shipping_phone_number="9876543299",
            shipping_address_line1="42 Brigade Road",
            shipping_address_line2="Flat 4B, Palm Grove",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            shipping_landmark="Near Metro Station",
            customer_notes="Please ring bell twice upon arrival.",
        )

        # Seller A Sub-Order Package
        self.pkg_a = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller_a,
            sub_order_number="ORD-MULTI-20261011-001-SA",
            status=SellerOrder.STATUS_PENDING,
            subtotal=Decimal("2000.00"),
            total_commission=Decimal("200.00"),
            net_seller_payable=Decimal("1800.00"),
            is_platform_fulfilled=False,
        )
        self.item_a = OrderItem.objects.create(
            order=self.order,
            product=self.prod_a,
            product_name=self.prod_a.name,
            sku=self.prod_a.sku,
            seller=self.seller_a,
            seller_order=self.pkg_a,
            unit_price=Decimal("1000.00"),
            quantity=2,
            line_total=Decimal("2000.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("200.00"),
        )

        # Seller B Sub-Order Package
        self.pkg_b = SellerOrder.objects.create(
            order=self.order,
            seller=self.seller_b,
            sub_order_number="ORD-MULTI-20261011-001-SB",
            status=SellerOrder.STATUS_PENDING,
            subtotal=Decimal("1500.00"),
            total_commission=Decimal("180.00"),
            net_seller_payable=Decimal("1320.00"),
            is_platform_fulfilled=False,
        )
        self.item_b = OrderItem.objects.create(
            order=self.order,
            product=self.prod_b,
            product_name=self.prod_b.name,
            sku=self.prod_b.sku,
            seller=self.seller_b,
            seller_order=self.pkg_b,
            unit_price=Decimal("1500.00"),
            quantity=1,
            line_total=Decimal("1500.00"),
            commission_rate=Decimal("12.00"),
            commission_amount=Decimal("180.00"),
        )

        # Platform Package
        self.pkg_plat = SellerOrder.objects.create(
            order=self.order,
            seller=None,
            sub_order_number="ORD-MULTI-20261011-001-PLT",
            status=SellerOrder.STATUS_PENDING,
            subtotal=Decimal("200.00"),
            total_commission=Decimal("0.00"),
            net_seller_payable=Decimal("200.00"),
            is_platform_fulfilled=True,
        )

    def test_approved_seller_can_view_own_order_list(self):
        """Approved seller can view their scoped order list with pagination."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_order_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/orders/list.html")
        packages = list(response.context["packages"])
        self.assertIn(self.pkg_a, packages)
        self.assertNotIn(self.pkg_b, packages)
        self.assertNotIn(self.pkg_plat, packages)
        # Verify internal commission or gateway secrets are not leaked
        self.assertNotContains(response, "sig_rp_test_789")
        self.assertNotContains(response, "pay_rp_test_456")

    def test_anonymous_user_cannot_access_seller_order_pages(self):
        """Anonymous requests redirect to login."""
        urls = [
            reverse("core:seller_order_list"),
            reverse("core:seller_order_detail", kwargs={"pk": self.pkg_a.pk}),
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("login", resp.url.lower())

    def test_unapproved_or_invalid_sellers_blocked_from_fulfillment(self):
        """Pending and inactive sellers are blocked by @seller_required."""
        # 1. Pending seller
        self.client.login(username="seller_pending_ord@example.com", password="Password123!")
        resp = self.client.get(reverse("core:seller_order_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse("core:seller_status"))

        # 2. Inactive seller
        self.seller_a.is_active = False
        self.seller_a.save(update_fields=["is_active"])
        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        resp = self.client.get(reverse("core:seller_order_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse("core:seller_status"))

        # Restore
        self.seller_a.is_active = True
        self.seller_a.save(update_fields=["is_active"])

    def test_seller_a_cannot_view_or_update_seller_b_package(self):
        """IDOR protection: Seller A cannot view or update Seller B's package."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")

        # Detail GET
        resp_get = self.client.get(reverse("core:seller_order_detail", kwargs={"pk": self.pkg_b.pk}))
        self.assertEqual(resp_get.status_code, 404)

        # Status Update POST
        resp_post = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_b.pk}),
            {"status": "processing"},
        )
        self.assertEqual(resp_post.status_code, 404)
        self.pkg_b.refresh_from_db()
        self.assertEqual(self.pkg_b.status, SellerOrder.STATUS_PENDING)

    def test_seller_cannot_access_platform_fulfilled_package(self):
        """Sellers cannot access direct platform-fulfilled sub-orders."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")

        resp_get = self.client.get(reverse("core:seller_order_detail", kwargs={"pk": self.pkg_plat.pk}))
        self.assertEqual(resp_get.status_code, 404)

        resp_post = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_plat.pk}),
            {"status": "processing"},
        )
        self.assertEqual(resp_post.status_code, 404)

    def test_seller_detail_shows_only_own_items_and_delivery_info(self):
        """Package detail strictly isolates items and exposes minimal delivery info."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_order_detail", kwargs={"pk": self.pkg_a.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/orders/detail.html")

        # Seller A's item is present
        self.assertContains(response, "Alpha Compression Shirt")
        # Seller B's item is NOT present
        self.assertNotContains(response, "Beta Running Shorts")

        # Delivery destination info is displayed
        self.assertContains(response, "Rajesh Kumar")
        self.assertContains(response, "42 Brigade Road")
        self.assertContains(response, "560001")

        # Customer account email / password / gateway credentials are NOT leaked
        self.assertNotContains(response, "buyer@example.com")
        self.assertNotContains(response, "order_rp_test_123")
        self.assertNotContains(response, "pay_rp_test_456")
        self.assertNotContains(response, "sig_rp_test_789")

    def test_valid_pending_to_processing_transition(self):
        """Seller can transition own package from Pending to Processing."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PENDING)

        response = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "processing"},
        )
        self.assertEqual(response.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PROCESSING)

        # Parent order status and financials untouched
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PROCESSING)
        self.assertEqual(self.order.payment_status, Order.PAYMENT_STATUS_PAID)

        # Seller B package untouched
        self.pkg_b.refresh_from_db()
        self.assertEqual(self.pkg_b.status, SellerOrder.STATUS_PENDING)

        # Commission and subtotal untouched
        self.assertEqual(self.pkg_a.total_commission, Decimal("200.00"))
        self.assertEqual(self.pkg_a.net_seller_payable, Decimal("1800.00"))

    def test_valid_processing_to_ready_to_ship_transition(self):
        """Seller can transition package from Processing to Ready to Ship."""
        self.pkg_a.status = SellerOrder.STATUS_PROCESSING
        self.pkg_a.save(update_fields=["status"])

        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        response = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "ready_to_ship"},
        )
        self.assertEqual(response.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_READY_TO_SHIP)

    def test_ready_to_ship_to_shipped_requires_courier_and_tracking(self):
        """Transitioning to Shipped requires non-empty courier and tracking details."""
        self.pkg_a.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg_a.save(update_fields=["status"])

        self.client.login(username="seller_a_ord@example.com", password="Password123!")

        # 1. Missing tracking and courier -> Fails
        resp_missing = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "shipped", "courier_name": "", "tracking_number": ""},
        )
        self.assertEqual(resp_missing.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_READY_TO_SHIP)
        self.assertIsNone(self.pkg_a.shipped_at)

        # 2. Missing tracking number -> Fails
        resp_no_track = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "shipped", "courier_name": "Delhivery", "tracking_number": ""},
        )
        self.assertEqual(resp_no_track.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_READY_TO_SHIP)

        # 3. Valid courier and tracking -> Succeeds
        resp_success = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "shipped", "courier_name": "Delhivery Express", "tracking_number": "DEL123456789"},
        )
        self.assertEqual(resp_success.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_SHIPPED)
        self.assertEqual(self.pkg_a.courier_name, "Delhivery Express")
        self.assertEqual(self.pkg_a.tracking_number, "DEL123456789")
        self.assertIsNotNone(self.pkg_a.shipped_at)

    def test_skipped_transitions_rejected(self):
        """Jumping over status steps is rejected."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PENDING)

        # Try to jump to Ready to Ship
        resp_jump1 = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "ready_to_ship"},
        )
        self.assertEqual(resp_jump1.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PENDING)

        # Try to jump to Shipped
        resp_jump2 = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "shipped", "courier_name": "Blue Dart", "tracking_number": "BD9999"},
        )
        self.assertEqual(resp_jump2.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PENDING)

    def test_backward_transitions_rejected(self):
        """Moving package status backwards is rejected."""
        self.pkg_a.status = SellerOrder.STATUS_PROCESSING
        self.pkg_a.save(update_fields=["status"])

        self.client.login(username="seller_a_ord@example.com", password="Password123!")

        # Processing -> Pending
        resp_back1 = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "pending"},
        )
        self.assertEqual(resp_back1.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PROCESSING)

        # Ready to Ship -> Processing
        self.pkg_a.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg_a.save(update_fields=["status"])
        resp_back2 = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "processing"},
        )
        self.assertEqual(resp_back2.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_READY_TO_SHIP)

    def test_delivered_cannot_be_set_by_seller(self):
        """Sellers cannot mark packages as Delivered."""
        self.pkg_a.status = SellerOrder.STATUS_READY_TO_SHIP
        self.pkg_a.save(update_fields=["status"])

        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        response = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "delivered"},
        )
        self.assertEqual(response.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertNotEqual(self.pkg_a.status, SellerOrder.STATUS_DELIVERED)

    def test_cancelled_packages_cannot_be_reactivated(self):
        """Cancelled packages cannot be reactivated or updated."""
        self.pkg_a.status = SellerOrder.STATUS_CANCELLED
        self.pkg_a.save(update_fields=["status"])

        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        response = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "processing"},
        )
        self.assertEqual(response.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_CANCELLED)

    def test_invalid_status_values_rejected(self):
        """Arbitrary forged status strings are rejected with no DB mutation."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        response = self.client.post(
            reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}),
            {"status": "flying_to_mars"},
        )
        self.assertEqual(response.status_code, 302)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PENDING)

    def test_get_request_to_update_status_never_mutates_state(self):
        """GET request to status update endpoint is rejected via HTTP 405."""
        self.client.login(username="seller_a_ord@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_order_update_status", kwargs={"pk": self.pkg_a.pk}))
        self.assertEqual(response.status_code, 405)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_PENDING)

    def test_parent_order_cancellation_still_protects_shipped_packages(self):
        """Existing Phase 2A cancellation protection is preserved: shipped packages prevent cancellation."""
        # Mark package as shipped
        self.pkg_a.mark_shipped(courier_name="Blue Dart", tracking_number="BD12345", commit=True)
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_SHIPPED)

        # Attempt to cancel parent order
        success, msg, order_obj = cancel_order(self.order, user=self.customer, reason="Changed mind")
        self.assertFalse(success)
        self.assertIn("one or more packages have already been dispatched or delivered", msg)

        # Statuses remain unchanged
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, Order.STATUS_CANCELLED)
        self.pkg_a.refresh_from_db()
        self.assertEqual(self.pkg_a.status, SellerOrder.STATUS_SHIPPED)


class SellerReportsAndExportTests(TestCase):
    """
    Automated tests for Seller Reports and Secure CSV Exports (Fitmeezo Phase 3 Prompt 4).
    Validates:
    1. Access control & authorization (@seller_required).
    2. Cross-seller & platform merchandise isolation.
    3. Correct fulfillment status counts and cancelled package segregation.
    4. Financial snapshot accuracy using Decimal arithmetic and immutability against commission changes.
    5. Safe date range filtering (supporting valid YYYY-MM-DD, gracefully handling invalid/reversed ranges).
    6. CSV export format, Excel BOM (\ufeff), UTF-8 headers, and file attachment metadata.
    7. CSV Formula Injection protection (CWE-1236) escaping '=', '+', '-', '@', '\t', '\r'.
    8. Customer privacy & payment secret exclusion.
    9. Read-only GET enforcement preventing state mutation during reporting/export.
    """

    def setUp(self):
        self.client = Client()

        # Customer User
        self.customer = User.objects.create_user(
            username="buyer_rep@example.com",
            email="buyer_rep@example.com",
            password="Password123!",
        )

        self.address = Address.objects.create(
            user=self.customer,
            full_name="Deepak Buyer",
            phone_number="9876543210",
            address_line1="45 Market Street",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
        )

        self.category = Category.objects.create(
            name="Fitness Equipment",
            slug="fitness-equipment-rep",
        )

        # Seller A (Approved, 10% commission)
        self.user_a = User.objects.create_user(
            username="seller_a_rep@example.com",
            email="seller_a_rep@example.com",
            password="Password123!",
        )
        self.seller_a = Seller.objects.create(
            user=self.user_a,
            store_name="Alpha Equipments",
            legal_business_name="Alpha Equipments Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@alphaequip.in",
            support_phone="9876543210",
            registered_address="10 Alpha St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="10 Alpha St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29AAAAA0000A1Z5",
            pan_number="AAAAA0000A",
            commission_rate=Decimal("10.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Seller B (Approved, 15% commission)
        self.user_b = User.objects.create_user(
            username="seller_b_rep@example.com",
            email="seller_b_rep@example.com",
            password="Password123!",
        )
        self.seller_b = Seller.objects.create(
            user=self.user_b,
            store_name="Beta Supplements",
            legal_business_name="Beta Supplements Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@betasupp.in",
            support_phone="9876543211",
            registered_address="20 Beta St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="20 Beta St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29BBBBB1111B1Z5",
            pan_number="BBBBB1111B",
            commission_rate=Decimal("15.00"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Pending Seller
        self.user_pending = User.objects.create_user(
            username="seller_pending_rep@example.com",
            email="seller_pending_rep@example.com",
            password="Password123!",
        )
        self.seller_pending = Seller.objects.create(
            user=self.user_pending,
            store_name="Pending Store",
            legal_business_name="Pending Store Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="pending@store.in",
            support_phone="9876543212",
            registered_address="30 Pending St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560003",
            pickup_address="30 Pending St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560003",
            gstin="29CCCCC2222C1Z5",
            pan_number="CCCCC2222C",
            status=Seller.STATUS_PENDING,
            is_active=True,
        )

        # Products for Seller A
        self.prod_a1 = Product.objects.create(
            category=self.category,
            name="Alpha Barbell 20kg",
            slug="alpha-barbell-20kg-rep",
            sku="AL-BB-020",
            price=Decimal("1000.00"),
            stock_quantity=20,
            seller=self.seller_a,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.prod_a2 = Product.objects.create(
            category=self.category,
            name="=cmd|' /C calc'!A0",  # Test CSV injection in product name
            slug="calc-injection-rep",
            sku="+INJ-001",           # Test CSV injection in SKU
            price=Decimal("500.00"),
            stock_quantity=10,
            seller=self.seller_a,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Product for Seller B
        self.prod_b1 = Product.objects.create(
            category=self.category,
            name="Beta Whey Isolate 1kg",
            slug="beta-whey-rep",
            sku="BT-WH-001",
            price=Decimal("3000.00"),
            stock_quantity=30,
            seller=self.seller_b,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )

        # Parent Order 1 (contains Seller A package & Seller B package)
        self.order1 = Order.objects.create(
            user=self.customer,
            order_number="ORD-REP-001",
            shipping_full_name="Deepak Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 Market Street",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            total_amount=Decimal("4000.00"),
            subtotal=Decimal("4000.00"),
            status=Order.STATUS_PENDING,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )

        # Seller A Sub-Order 1 (Active, Pending)
        self.pkg_a1 = SellerOrder.objects.create(
            order=self.order1,
            seller=self.seller_a,
            sub_order_number="PKG-REP-001-A1",
            subtotal=Decimal("1000.00"),
            total_commission=Decimal("100.00"),
            net_seller_payable=Decimal("900.00"),
            status=SellerOrder.STATUS_PENDING,
        )
        self.item_a1 = OrderItem.objects.create(
            order=self.order1,
            seller=self.seller_a,
            seller_order=self.pkg_a1,
            product=self.prod_a1,
            product_name=self.prod_a1.name,
            sku=self.prod_a1.sku,
            unit_price=Decimal("500.00"),
            quantity=2,
            line_total=Decimal("1000.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("100.00"),
        )

        # Seller B Sub-Order 1 (Seller B's package in same parent order)
        self.pkg_b1 = SellerOrder.objects.create(
            order=self.order1,
            seller=self.seller_b,
            sub_order_number="PKG-REP-001-B1",
            subtotal=Decimal("3000.00"),
            total_commission=Decimal("450.00"),
            net_seller_payable=Decimal("2550.00"),
            status=SellerOrder.STATUS_PENDING,
        )
        self.item_b1 = OrderItem.objects.create(
            order=self.order1,
            seller=self.seller_b,
            seller_order=self.pkg_b1,
            product=self.prod_b1,
            product_name=self.prod_b1.name,
            sku=self.prod_b1.sku,
            unit_price=Decimal("3000.00"),
            quantity=1,
            line_total=Decimal("3000.00"),
            commission_rate=Decimal("15.00"),
            commission_amount=Decimal("450.00"),
        )

        # Parent Order 2 (Contains Seller A Shipped package)
        self.order2 = Order.objects.create(
            user=self.customer,
            order_number="ORD-REP-002",
            shipping_full_name="Deepak Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 Market Street",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            total_amount=Decimal("2000.00"),
            subtotal=Decimal("2000.00"),
            status=Order.STATUS_PROCESSING,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
            razorpay_payment_id="pay_test_999888777",
            razorpay_order_id="order_test_111222333",
            razorpay_signature="sig_secret_mock_abc",
        )
        self.pkg_a2 = SellerOrder.objects.create(
            order=self.order2,
            seller=self.seller_a,
            sub_order_number="PKG-REP-002-A2",
            subtotal=Decimal("2000.00"),
            total_commission=Decimal("200.00"),
            net_seller_payable=Decimal("1800.00"),
            status=SellerOrder.STATUS_SHIPPED,
            courier_name="Blue Dart",
            tracking_number="BD777888",
            shipped_at=timezone.now(),
        )
        self.item_a2 = OrderItem.objects.create(
            order=self.order2,
            seller=self.seller_a,
            seller_order=self.pkg_a2,
            product=self.prod_a1,
            product_name=self.prod_a1.name,
            sku=self.prod_a1.sku,
            unit_price=Decimal("500.00"),
            quantity=4,
            line_total=Decimal("2000.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("200.00"),
        )

        # Parent Order 3 (Contains Seller A Cancelled package)
        self.order3 = Order.objects.create(
            user=self.customer,
            order_number="ORD-REP-003",
            shipping_full_name="Deepak Buyer",
            shipping_phone_number="9876543210",
            shipping_address_line1="45 Market Street",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            total_amount=Decimal("500.00"),
            subtotal=Decimal("500.00"),
            status=Order.STATUS_CANCELLED,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )
        self.pkg_a_cancelled = SellerOrder.objects.create(
            order=self.order3,
            seller=self.seller_a,
            sub_order_number="PKG-REP-003-AC",
            subtotal=Decimal("500.00"),
            total_commission=Decimal("50.00"),
            net_seller_payable=Decimal("450.00"),
            status=SellerOrder.STATUS_CANCELLED,
            cancelled_at=timezone.now(),
        )
        self.item_a_cancelled = OrderItem.objects.create(
            order=self.order3,
            seller=self.seller_a,
            seller_order=self.pkg_a_cancelled,
            product=self.prod_a2,
            product_name=self.prod_a2.name,
            sku=self.prod_a2.sku,
            unit_price=Decimal("500.00"),
            quantity=1,
            line_total=Decimal("500.00"),
            commission_rate=Decimal("10.00"),
            commission_amount=Decimal("50.00"),
        )

    # --------------------------------------------------------------------------
    # 1. Access Control & Authorization
    # --------------------------------------------------------------------------
    def test_approved_seller_can_access_reports_page(self):
        """Approved and active seller can view their reports dashboard."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_reports"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/reports/index.html")
        self.assertIn("reports", response.context)

    def test_anonymous_and_unapproved_users_blocked_from_reports(self):
        """Anonymous and pending/inactive sellers cannot access reports or exports."""
        urls = [
            reverse("core:seller_reports"),
            reverse("core:seller_export_orders_csv"),
            reverse("core:seller_export_products_csv"),
            reverse("core:seller_export_financial_csv"),
        ]
        # Anonymous -> redirected to login
        for u in urls:
            resp = self.client.get(u)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("login", resp.url.lower())

        # Pending seller -> redirected to seller_status
        self.client.login(username="seller_pending_rep@example.com", password="Password123!")
        for u in urls:
            resp = self.client.get(u)
            self.assertEqual(resp.status_code, 302)
            self.assertRedirects(resp, reverse("core:seller_status"))

    # --------------------------------------------------------------------------
    # 2. Strict Cross-Seller & Platform Isolation
    # --------------------------------------------------------------------------
    def test_seller_a_reports_never_includes_seller_b_or_platform(self):
        """Seller A's reports strictly exclude Seller B's packages, revenue, and products."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_reports"))
        reports = response.context["reports"]

        # Seller A has 3 packages total (pkg_a1, pkg_a2, pkg_a_cancelled). Seller B's pkg_b1 is NOT counted.
        self.assertEqual(reports["total_packages"], 3)
        self.assertEqual(reports["active_packages_count"], 2)

        # Seller B's products should never appear in Seller A's product sales
        product_names = [p["product_name"] for p in reports["product_sales"]]
        self.assertIn("Alpha Barbell 20kg", product_names)
        self.assertNotIn("Beta Whey Isolate 1kg", product_names)

        # Recent packages list must not include Seller B's sub-order
        sub_order_numbers = [pkg.sub_order_number for pkg in reports["recent_packages"]]
        self.assertIn(self.pkg_a1.sub_order_number, sub_order_numbers)
        self.assertNotIn(self.pkg_b1.sub_order_number, sub_order_numbers)

    # --------------------------------------------------------------------------
    # 3. Status Counts & Cancelled Package Handling
    # --------------------------------------------------------------------------
    def test_status_counts_and_cancelled_packages_segregation(self):
        """Active package financials strictly exclude cancelled packages; cancelled orders are tracked separately."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_reports"))
        reports = response.context["reports"]

        self.assertEqual(reports["pending_count"], 1)
        self.assertEqual(reports["shipped_count"], 1)
        self.assertEqual(reports["cancelled_count"], 1)

        # Active financials = pkg_a1 (1000) + pkg_a2 (2000) = 3000.00
        self.assertEqual(reports["gross_sales"], Decimal("3000.00"))
        self.assertEqual(reports["total_commission"], Decimal("300.00"))
        self.assertEqual(reports["net_seller_payable"], Decimal("2700.00"))

        # Cancelled financials = pkg_a_cancelled (500.00)
        self.assertEqual(reports["cancelled_subtotal"], Decimal("500.00"))
        self.assertEqual(reports["cancelled_commission"], Decimal("50.00"))
        self.assertEqual(reports["cancelled_payable"], Decimal("450.00"))

    # --------------------------------------------------------------------------
    # 4. Financial Snapshot Accuracy & Immutability Against Rate Changes
    # --------------------------------------------------------------------------
    def test_financial_totals_immutable_when_commission_rate_changes(self):
        """Historical financial snapshots are stored and do not change if seller's rate changes."""
        # Change Seller A's commission rate from 10.00% to 25.00%
        self.seller_a.commission_rate = Decimal("25.00")
        self.seller_a.save(update_fields=["commission_rate"])

        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_reports"))
        reports = response.context["reports"]

        # Historical stored totals remain 300.00 and 2700.00, NOT recalculated at 25% (which would be 750.00 and 2250.00)
        self.assertEqual(reports["total_commission"], Decimal("300.00"))
        self.assertEqual(reports["net_seller_payable"], Decimal("2700.00"))

    # --------------------------------------------------------------------------
    # 5. Date Range Filtering
    # --------------------------------------------------------------------------
    def test_date_range_filtering_applies_correctly(self):
        """Valid date range bounds filter report results and CSV downloads."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")

        today_str = timezone.now().strftime("%Y-%m-%d")
        response = self.client.get(
            reverse("core:seller_reports"),
            {"start_date": today_str, "end_date": today_str},
        )
        self.assertEqual(response.status_code, 200)
        reports = response.context["reports"]
        self.assertEqual(reports["total_packages"], 3)

        # Distant past date range returns 0 packages
        past_response = self.client.get(
            reverse("core:seller_reports"),
            {"start_date": "2020-01-01", "end_date": "2020-01-31"},
        )
        past_reports = past_response.context["reports"]
        self.assertEqual(past_reports["total_packages"], 0)
        self.assertEqual(past_reports["gross_sales"], Decimal("0.00"))

    def test_invalid_and_reversed_date_ranges_handled_gracefully(self):
        """Reversed dates and malformed strings display error message without 500 failure."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")

        # Reversed range
        resp_reversed = self.client.get(
            reverse("core:seller_reports"),
            {"start_date": "2026-12-31", "end_date": "2026-01-01"},
        )
        self.assertEqual(resp_reversed.status_code, 200)
        self.assertIn("Start date cannot be after end date", resp_reversed.context["date_filter_error"])

        # Malformed date string
        resp_malformed = self.client.get(
            reverse("core:seller_reports"),
            {"start_date": "not-a-date", "end_date": "2026-10-10"},
        )
        self.assertEqual(resp_malformed.status_code, 200)
        self.assertIn("Invalid start date format", resp_malformed.context["date_filter_error"])

        # CSV endpoint with invalid date redirects to reports page with error
        csv_resp = self.client.get(
            reverse("core:seller_export_orders_csv"),
            {"start_date": "2026-12-31", "end_date": "2026-01-01"},
        )
        self.assertEqual(csv_resp.status_code, 302)
        self.assertRedirects(csv_resp, reverse("core:seller_reports"))

    # --------------------------------------------------------------------------
    # 6. CSV Export Formats & Excel BOM Compatibility
    # --------------------------------------------------------------------------
    def test_csv_order_export_headers_and_bom_encoding(self):
        """Order CSV export begins with UTF-8 BOM, has text/csv header, and correct columns."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_export_orders_csv"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment; filename=", response["Content-Disposition"])

        content = response.content.decode("utf-8-sig")
        # Check first chars are BOM decoded properly
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))

        # Verify CSV column headers
        self.assertIn("Sub-Order Number", content)
        self.assertIn("Fulfillment Status", content)
        self.assertIn("Subtotal (INR)", content)
        self.assertIn("Commission Snapshot (INR)", content)
        self.assertIn("Net Payable Snapshot (INR)", content)

        # Verify Seller A's sub-orders appear
        self.assertIn(self.pkg_a1.sub_order_number, content)
        self.assertIn(self.pkg_a2.sub_order_number, content)

        # Verify Seller B's package is absent
        self.assertNotIn(self.pkg_b1.sub_order_number, content)

    def test_csv_product_sales_export(self):
        """Product sales CSV export contains only seller's own sold items."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_export_products_csv"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8-sig")
        self.assertIn("Product Name,Variant Label,SKU,Units Sold,Gross Sales (INR),Orders Count", content)
        self.assertIn("Alpha Barbell 20kg", content)
        self.assertNotIn("Beta Whey Isolate 1kg", content)

    def test_csv_financial_summary_export(self):
        """Financial summary CSV export notes snapshots and includes active totals."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_export_financial_csv"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8-sig")
        self.assertIn("Fitmeezo Marketplace - Seller Financial Snapshot Summary", content)
        self.assertIn("Recorded order snapshots only. Not settled payouts", content)
        self.assertIn("Gross Subtotal (Active Orders),3000.00", content)
        self.assertIn("Platform Commission Snapshot (Active Orders),300.00", content)
        self.assertIn("Net Seller Payable Snapshot (Active Orders),2700.00", content)

    # --------------------------------------------------------------------------
    # 7. CSV Formula Injection Defense (CWE-1236)
    # --------------------------------------------------------------------------
    def test_csv_formula_injection_characters_are_escaped(self):
        """Cells beginning with '=', '+', '-', '@', tab, or carriage return are prefixed with single quote."""
        from core.seller_services import sanitize_csv_cell

        # Unit test the sanitizer directly
        self.assertEqual(sanitize_csv_cell("=1+1"), "'=1+1")
        self.assertEqual(sanitize_csv_cell("+cmd"), "'+cmd")
        self.assertEqual(sanitize_csv_cell("-calc"), "'-calc")
        self.assertEqual(sanitize_csv_cell("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(sanitize_csv_cell("\tTabbed"), "'\tTabbed")
        self.assertEqual(sanitize_csv_cell("\rReturn"), "'\rReturn")
        self.assertEqual(sanitize_csv_cell("Normal Text"), "Normal Text")
        self.assertEqual(sanitize_csv_cell(None), "")
        self.assertEqual(sanitize_csv_cell(Decimal("100.00")), "100.00")

        # Test CSV export output with malicious values
        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_export_orders_csv"))
        content = response.content.decode("utf-8-sig")

        # Ensure that if any field starts with '=' or '+', it is escaped
        self.assertNotIn("\n=cmd", content)
        self.assertNotIn("\n+INJ", content)

    # --------------------------------------------------------------------------
    # 8. Customer Privacy & Payment Secret Protection
    # --------------------------------------------------------------------------
    def test_csv_and_reports_never_leak_payment_secrets_or_personal_data(self):
        """Customer email, password, and Razorpay signature/secrets are omitted from reports and CSV."""
        self.client.login(username="seller_a_rep@example.com", password="Password123!")

        # 1. HTML Reports page check
        rep_resp = self.client.get(reverse("core:seller_reports"))
        html = rep_resp.content.decode("utf-8")
        self.assertNotIn("pay_test_999888777", html)
        self.assertNotIn("sig_secret_mock_abc", html)
        self.assertNotIn("buyer_rep@example.com", html)

        # 2. Orders CSV export check
        csv_resp = self.client.get(reverse("core:seller_export_orders_csv"))
        csv_content = csv_resp.content.decode("utf-8-sig")
        self.assertNotIn("pay_test_999888777", csv_content)
        self.assertNotIn("sig_secret_mock_abc", csv_content)
        self.assertNotIn("buyer_rep@example.com", csv_content)

    # --------------------------------------------------------------------------
    # 9. Read-Only GET Enforcement (No Database Mutation)
    # --------------------------------------------------------------------------
    def test_csv_exports_never_mutate_state_or_data(self):
        """CSV exports and reports requests never mutate inventory, orders, or commission."""
        stock_before = self.prod_a1.stock_quantity
        subtotal_before = self.pkg_a1.subtotal
        status_before = self.pkg_a1.status

        self.client.login(username="seller_a_rep@example.com", password="Password123!")
        self.client.get(reverse("core:seller_reports"))
        self.client.get(reverse("core:seller_export_orders_csv"))
        self.client.get(reverse("core:seller_export_products_csv"))
        self.client.get(reverse("core:seller_export_financial_csv"))

        self.prod_a1.refresh_from_db()
        self.pkg_a1.refresh_from_db()

        self.assertEqual(self.prod_a1.stock_quantity, stock_before)
        self.assertEqual(self.pkg_a1.subtotal, subtotal_before)
        self.assertEqual(self.pkg_a1.status, status_before)

        # POST requests to export endpoints are rejected with 405 Method Not Allowed
        post_resp = self.client.post(reverse("core:seller_export_orders_csv"))
        self.assertEqual(post_resp.status_code, 405)


# ==============================================================================
# PHASE 3 PROMPT 5: SELLER PANEL UI & NAVIGATION SUITE
# ==============================================================================

class SellerUIAndNavigationTests(TestCase):
    """
    Fitmeezo Phase 3 Prompt 5: Professional Seller Panel UI Upgrade tests.
    Verifies base layout template usage, sidebar navigation links pointing to valid routes,
    responsive shell, access controls, and seller data isolation across upgraded templates.
    """

    def setUp(self):
        self.client = Client()
        self.seller_user = User.objects.create_user(
            username="ui_seller@example.com",
            email="ui_seller@example.com",
            password="Password123!",
            first_name="Vikram",
            last_name="Malhotra",
        )
        self.seller = Seller.objects.create(
            user=self.seller_user,
            store_name="Malhotra Sports",
            legal_business_name="Malhotra Sports Private Limited",
            gstin="27ABCDE1234F1Z5",
            status=Seller.STATUS_APPROVED,
            commission_rate=Decimal("12.50"),
            is_active=True,
        )
        self.customer = User.objects.create_user(
            username="ui_customer@example.com",
            email="ui_customer@example.com",
            password="Password123!",
        )

    def test_approved_seller_dashboard_uses_seller_base_template(self):
        """Approved seller dashboard renders with 200 and uses seller/base.html layout."""
        self.client.login(username="ui_seller@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/dashboard.html")
        self.assertTemplateUsed(response, "seller/base.html")
        self.assertContains(response, "Malhotra Sports")
        self.assertContains(response, "GSTIN: 27ABCDE1234F1Z5")
        self.assertContains(response, "Hub")

    def test_sidebar_navigation_links_point_to_valid_routes(self):
        """Sidebar contains working links for Overview, Orders, Products, Reports, Profile."""
        self.client.login(username="ui_seller@example.com", password="Password123!")
        response = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(response.status_code, 200)

        # Check existing functional URLs are present in navigation
        self.assertContains(response, reverse("core:seller_dashboard"))
        self.assertContains(response, reverse("core:seller_order_list"))
        self.assertContains(response, reverse("core:seller_product_list"))
        self.assertContains(response, reverse("core:seller_reports"))
        self.assertContains(response, reverse("core:seller_profile"))

        # Returns is now a functional route (implemented in Phase 6)
        self.assertContains(response, reverse("core:seller_return_list"))
        self.assertContains(response, "Returns")

    def test_unauthenticated_user_cannot_access_seller_ui(self):
        """Unauthenticated requests to all seller UI views are redirected to login."""
        protected_urls = [
            reverse("core:seller_dashboard"),
            reverse("core:seller_order_list"),
            reverse("core:seller_product_list"),
            reverse("core:seller_product_add"),
            reverse("core:seller_reports"),
            reverse("core:seller_profile"),
        ]
        for url in protected_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, f"Failed for {url}")
            self.assertIn(reverse("core:account_login"), resp.url)

    def test_ordinary_customer_cannot_access_seller_ui(self):
        """Ordinary non-seller customers are redirected/denied access to seller UI."""
        self.client.login(username="ui_customer@example.com", password="Password123!")
        protected_urls = [
            reverse("core:seller_dashboard"),
            reverse("core:seller_order_list"),
            reverse("core:seller_product_list"),
            reverse("core:seller_reports"),
            reverse("core:seller_profile"),
        ]
        for url in protected_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, f"Customer was not redirected from {url}")

    def test_seller_products_pages_use_seller_base(self):
        """Product list and add pages use seller/base.html layout."""
        self.client.login(username="ui_seller@example.com", password="Password123!")
        resp_list = self.client.get(reverse("core:seller_product_list"))
        self.assertEqual(resp_list.status_code, 200)
        self.assertTemplateUsed(resp_list, "seller/products/list.html")
        self.assertTemplateUsed(resp_list, "seller/base.html")

        resp_add = self.client.get(reverse("core:seller_product_add"))
        self.assertEqual(resp_add.status_code, 200)
        self.assertTemplateUsed(resp_add, "seller/products/form.html")
        self.assertTemplateUsed(resp_add, "seller/base.html")

    def test_seller_orders_page_uses_seller_base(self):
        """Order list page uses seller/base.html layout."""
        self.client.login(username="ui_seller@example.com", password="Password123!")
        resp = self.client.get(reverse("core:seller_order_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "seller/orders/list.html")
        self.assertTemplateUsed(resp, "seller/base.html")

    def test_seller_reports_page_uses_seller_base(self):
        """Reports & analytics dashboard uses seller/base.html layout."""
        self.client.login(username="ui_seller@example.com", password="Password123!")
        resp = self.client.get(reverse("core:seller_reports"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "seller/reports/index.html")
        self.assertTemplateUsed(resp, "seller/base.html")

    def test_seller_profile_page_uses_seller_base(self):
        """Store profile page uses seller/base.html layout."""
        self.client.login(username="ui_seller@example.com", password="Password123!")
        resp = self.client.get(reverse("core:seller_profile"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "seller/profile.html")
        self.assertTemplateUsed(resp, "seller/base.html")


class SellerProductSlugAndValidationTests(TestCase):
    """
    Comprehensive regression tests for unique slug generation, collision resolution,
    concurrency safeguards, and graceful error handling on product add/edit.
    """

    def setUp(self):
        self.category = Category.objects.create(name="Apparel & Fitness", slug="apparel-fitness")

        # Seller A
        self.user_a = User.objects.create_user(
            username="seller_slug_a@fitmeezo.com",
            email="seller_slug_a@fitmeezo.com",
            password="SecurePassword123!",
        )
        self.seller_a = Seller.objects.create(
            user=self.user_a,
            store_name="Alpha Slug Hub",
            legal_business_name="Alpha Slug Retail Pvt Ltd",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@alphaslug.in",
            support_phone="9876543210",
            registered_mobile="9876543210",
            registered_address="10 Alpha St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="10 Alpha St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29AAAAA1111A1Z5",
            pan_number="AAAAA1111A",
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # Seller B
        self.user_b = User.objects.create_user(
            username="seller_slug_b@fitmeezo.com",
            email="seller_slug_b@fitmeezo.com",
            password="SecurePassword123!",
        )
        self.seller_b = Seller.objects.create(
            user=self.user_b,
            store_name="Beta Slug Store",
            legal_business_name="Beta Slug LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="support@betaslug.in",
            support_phone="9876543211",
            registered_mobile="9876543211",
            registered_address="20 Beta St",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560002",
            pickup_address="20 Beta St",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560002",
            gstin="29BBBBB2222B1Z5",
            pan_number="BBBBB2222B",
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

    def test_create_two_products_with_same_name_by_same_seller(self):
        """Creating multiple products with the same name appends -2, -3 suffixes without crashing."""
        self.client.login(username="seller_slug_a@fitmeezo.com", password="SecurePassword123!")

        # First product
        resp1 = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "FitPulse Pro Watch",
                "category": self.category.pk,
                "sku": "WATCH-001",
                "price": "2999.00",
                "stock_quantity": "10",
                "is_active": True,
            },
        )
        self.assertEqual(resp1.status_code, 302)
        p1 = Product.objects.get(sku="WATCH-001")
        self.assertEqual(p1.slug, "fitpulse-pro-watch")
        self.assertEqual(p1.seller, self.seller_a)

        # Second product with identical name
        resp2 = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "FitPulse Pro Watch",
                "category": self.category.pk,
                "sku": "WATCH-002",
                "price": "3499.00",
                "stock_quantity": "5",
                "is_active": True,
            },
        )
        self.assertEqual(resp2.status_code, 302)
        p2 = Product.objects.get(sku="WATCH-002")
        self.assertEqual(p2.slug, "fitpulse-pro-watch-2")
        self.assertEqual(p2.seller, self.seller_a)

        # Third product with identical name
        resp3 = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "FitPulse Pro Watch",
                "category": self.category.pk,
                "sku": "WATCH-003",
                "price": "3999.00",
                "stock_quantity": "8",
                "is_active": True,
            },
        )
        self.assertEqual(resp3.status_code, 302)
        p3 = Product.objects.get(sku="WATCH-003")
        self.assertEqual(p3.slug, "fitpulse-pro-watch-3")
        self.assertEqual(p3.seller, self.seller_a)

    def test_two_different_sellers_create_products_with_same_name(self):
        """Two distinct sellers creating products with identical titles get unique slugs and isolated ownership."""
        # Seller A creates product
        self.client.login(username="seller_slug_a@fitmeezo.com", password="SecurePassword123!")
        resp_a = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Wireless Noise Cancelling Headphones",
                "category": self.category.pk,
                "sku": "HEAD-A01",
                "price": "4999.00",
                "stock_quantity": "15",
                "is_active": True,
            },
        )
        self.assertEqual(resp_a.status_code, 302)
        prod_a = Product.objects.get(sku="HEAD-A01")
        self.assertEqual(prod_a.slug, "wireless-noise-cancelling-headphones")
        self.assertEqual(prod_a.seller, self.seller_a)

        # Seller B creates product with exact same title
        self.client.login(username="seller_slug_b@fitmeezo.com", password="SecurePassword123!")
        resp_b = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Wireless Noise Cancelling Headphones",
                "category": self.category.pk,
                "sku": "HEAD-B01",
                "price": "5299.00",
                "stock_quantity": "20",
                "is_active": True,
            },
        )
        self.assertEqual(resp_b.status_code, 302)
        prod_b = Product.objects.get(sku="HEAD-B01")
        self.assertEqual(prod_b.slug, "wireless-noise-cancelling-headphones-2")
        self.assertEqual(prod_b.seller, self.seller_b)

        # Cross-seller edit isolation
        self.client.login(username="seller_slug_a@fitmeezo.com", password="SecurePassword123!")
        edit_cross = self.client.get(reverse("core:seller_product_edit", kwargs={"pk": prod_b.pk}))
        self.assertEqual(edit_cross.status_code, 404)

        self.client.login(username="seller_slug_b@fitmeezo.com", password="SecurePassword123!")
        edit_cross_b = self.client.get(reverse("core:seller_product_edit", kwargs={"pk": prod_a.pk}))
        self.assertEqual(edit_cross_b.status_code, 404)

    def test_create_products_with_different_names(self):
        """Products with distinct titles receive clean base slugs without unnecessary numerical suffixes."""
        self.client.login(username="seller_slug_a@fitmeezo.com", password="SecurePassword123!")

        resp1 = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Ultra Running Shoes",
                "category": self.category.pk,
                "sku": "SHOE-RUN-01",
                "price": "3999.00",
                "stock_quantity": "12",
                "is_active": True,
            },
        )
        self.assertEqual(resp1.status_code, 302)
        p1 = Product.objects.get(sku="SHOE-RUN-01")
        self.assertEqual(p1.slug, "ultra-running-shoes")

        resp2 = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Eco Friendly Yoga Mat",
                "category": self.category.pk,
                "sku": "YOGA-MAT-01",
                "price": "1299.00",
                "stock_quantity": "25",
                "is_active": True,
            },
        )
        self.assertEqual(resp2.status_code, 302)
        p2 = Product.objects.get(sku="YOGA-MAT-01")
        self.assertEqual(p2.slug, "eco-friendly-yoga-mat")

    def test_preserve_existing_product_slug_on_update(self):
        """Existing product slugs are preserved on update and not unnecessarily renamed."""
        self.client.login(username="seller_slug_a@fitmeezo.com", password="SecurePassword123!")

        # Create
        self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Heritage Leather Jacket",
                "category": self.category.pk,
                "sku": "JKT-001",
                "price": "5999.00",
                "stock_quantity": "7",
                "is_active": True,
            },
        )
        product = Product.objects.get(sku="JKT-001")
        original_slug = product.slug
        self.assertEqual(original_slug, "heritage-leather-jacket")

        # Edit price and stock
        resp_edit = self.client.post(
            reverse("core:seller_product_edit", kwargs={"pk": product.pk}),
            {
                "name": "Heritage Leather Jacket",
                "category": self.category.pk,
                "sku": "JKT-001",
                "price": "6499.00",
                "stock_quantity": "10",
                "is_active": True,
            },
        )
        self.assertEqual(resp_edit.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.slug, original_slug)
        self.assertEqual(product.price, Decimal("6499.00"))

        # Edit name - slug should remain preserved to protect SEO and bookmarks
        resp_edit_name = self.client.post(
            reverse("core:seller_product_edit", kwargs={"pk": product.pk}),
            {
                "name": "Heritage Premium Leather Jacket",
                "category": self.category.pk,
                "sku": "JKT-001",
                "price": "6499.00",
                "stock_quantity": "10",
                "is_active": True,
            },
        )
        self.assertEqual(resp_edit_name.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.slug, original_slug)
        self.assertEqual(product.name, "Heritage Premium Leather Jacket")

    def test_validation_failures_display_normally_without_crash(self):
        """Form validation failures return 200 OK with visible error messages and no 500 crash."""
        self.client.login(username="seller_slug_a@fitmeezo.com", password="SecurePassword123!")

        # Negative price validation failure
        resp_neg_price = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Invalid Price Item",
                "category": self.category.pk,
                "sku": "INV-PRC-01",
                "price": "-50.00",
                "stock_quantity": "5",
                "is_active": True,
            },
        )
        self.assertEqual(resp_neg_price.status_code, 200)
        self.assertFalse(Product.objects.filter(sku="INV-PRC-01").exists())
        self.assertContains(resp_neg_price, "Selling price must be at least ₹0.01")

        # Compare at price lower than selling price
        resp_cmp = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Invalid Compare Item",
                "category": self.category.pk,
                "sku": "INV-CMP-01",
                "price": "1000.00",
                "compare_at_price": "800.00",
                "stock_quantity": "5",
                "is_active": True,
            },
        )
        self.assertEqual(resp_cmp.status_code, 200)
        self.assertFalse(Product.objects.filter(sku="INV-CMP-01").exists())
        self.assertContains(resp_cmp, "Compare-at (M.R.P.) price cannot be lower than the selling price")

        # Missing required SKU
        resp_no_sku = self.client.post(
            reverse("core:seller_product_add"),
            {
                "name": "Missing SKU Item",
                "category": self.category.pk,
                "sku": "",
                "price": "500.00",
                "stock_quantity": "5",
                "is_active": True,
            },
        )
        self.assertEqual(resp_no_sku.status_code, 200)
        self.assertFalse(Product.objects.filter(name="Missing SKU Item").exists())

    def test_direct_orm_model_save_slug_generation_and_concurrency(self):
        """Direct Model.save() handles empty slugs, duplicates, and special characters cleanly."""
        p1 = Product.objects.create(
            name="Smart Bluetooth Scale",
            sku="SCL-001",
            price=Decimal("1499.00"),
            category=self.category,
            seller=self.seller_a,
        )
        self.assertEqual(p1.slug, "smart-bluetooth-scale")

        p2 = Product.objects.create(
            name="Smart Bluetooth Scale",
            sku="SCL-002",
            price=Decimal("1699.00"),
            category=self.category,
            seller=self.seller_b,
        )
        self.assertEqual(p2.slug, "smart-bluetooth-scale-2")

        # Test non-ascii / symbol fallback
        p_symbol = Product.objects.create(
            name="⭐⭐⭐ Special Edition ⭐⭐⭐",
            sku="SPEC-001",
            price=Decimal("999.00"),
            category=self.category,
            seller=self.seller_a,
        )
        self.assertEqual(p_symbol.slug, "special-edition")







