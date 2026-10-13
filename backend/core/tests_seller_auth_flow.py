"""
Fitmeezo Test Suite: Seller Registration, Authentication, Password Reset & Access Isolation.
Validates:
1. Public "Become a Seller" registration for unauthenticated merchants.
2. Secure password hashing using Django's built-in authentication (no plain-text passwords).
3. Seller profile initialization with STATUS_PENDING awaiting administrative review.
4. Duplicate prevention: usernames, emails, store names, and GSTINs.
5. Staff & superuser account exclusion from seller onboarding and vendor login.
6. Seller login portal (/seller/login/) with state machine routing:
   - Approved -> Seller Dashboard
   - Pending -> Seller Status
   - Rejected / Suspended -> Seller Status with security flash message
   - Customer without seller profile -> Guidance to register
7. Server-side isolation: Non-approved sellers blocked from dashboard and endpoints.
8. Customer storefront login (/account/login/) remains 100% operational.
9. Seller logout (/seller/logout/) terminates session and redirects to seller login.
10. Secure token-based password reset workflow for sellers.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse

from core.models import CustomerProfile, Seller

User = get_user_model()


class SellerAuthAndRegistrationFlowTests(TestCase):
    """
    Integration tests covering public registration, login, logout, password recovery,
    and server-side authorization for marketplace merchants.
    """

    def setUp(self):
        self.client = Client()

        # Pre-configured Approved Seller
        self.approved_user = User.objects.create_user(
            username="approved_partner",
            email="approved@partner.in",
            password="SecurePass123!",
            first_name="Approved",
            last_name="Partner",
        )
        self.approved_seller = Seller.objects.create(
            user=self.approved_user,
            store_name="Royal Heritage Apparel",
            legal_business_name="Royal Heritage Private Limited",
            business_type=Seller.BUSINESS_TYPE_PVT_LTD,
            support_email="support@royalheritage.in",
            support_phone="9876543210",
            registered_address="100 Commercial Street",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Warehouse 2, Electronic City",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_APPROVED,
            is_active=True,
            commission_rate=Decimal("10.00"),
        )

        # Pre-configured Pending Seller
        self.pending_user = User.objects.create_user(
            username="pending_partner",
            email="pending@partner.in",
            password="SecurePass123!",
            first_name="Pending",
            last_name="Partner",
        )
        self.pending_seller = Seller.objects.create(
            user=self.pending_user,
            store_name="Modern Footwear",
            legal_business_name="Modern Footwear LLP",
            business_type=Seller.BUSINESS_TYPE_LLP,
            support_email="support@modernfootwear.in",
            support_phone="9876543211",
            registered_address="200 Brigade Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560025",
            pickup_address="200 Brigade Road",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560025",
            gstin="29BCDEF2345G1Z6",
            pan_number="BCDEF2345G",
            status=Seller.STATUS_PENDING,
            is_active=True,
            commission_rate=Decimal("0.00"),
        )

        # Pre-configured Rejected Seller
        self.rejected_user = User.objects.create_user(
            username="rejected_partner",
            email="rejected@partner.in",
            password="SecurePass123!",
        )
        self.rejected_seller = Seller.objects.create(
            user=self.rejected_user,
            store_name="Rejected Crafts",
            legal_business_name="Rejected Crafts Ltd",
            support_email="support@rejected.in",
            support_phone="9876543212",
            registered_address="300 MG Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="300 MG Road",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29CDEFG3456H1Z7",
            status=Seller.STATUS_REJECTED,
            is_active=False,
        )

        # Pre-configured Suspended Seller
        self.suspended_user = User.objects.create_user(
            username="suspended_partner",
            email="suspended@partner.in",
            password="SecurePass123!",
        )
        self.suspended_seller = Seller.objects.create(
            user=self.suspended_user,
            store_name="Suspended Goods",
            legal_business_name="Suspended Goods Ltd",
            support_email="support@suspended.in",
            support_phone="9876543213",
            registered_address="400 Residency Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560025",
            pickup_address="400 Residency Road",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560025",
            gstin="29DEFGHI4567I1Z8",
            status=Seller.STATUS_SUSPENDED,
            is_active=False,
        )

        # Standard Customer (No Seller Profile)
        self.customer_user = User.objects.create_user(
            username="shopper_john",
            email="john@shopper.in",
            password="SecurePass123!",
            first_name="John",
            last_name="Doe",
        )

        # Platform Administrator
        self.admin_user = User.objects.create_superuser(
            username="fitmeezo_admin",
            email="admin@fitmeezo.in",
            password="AdminSuperPassword123!",
        )

    # --------------------------------------------------------------------------
    # 1. Public "Become a Seller" Registration Tests
    # --------------------------------------------------------------------------

    def test_public_seller_registration_page_loads_for_anonymous_visitor(self):
        """Unauthenticated visitors can access the Become a Seller page directly."""
        response = self.client.get(reverse("core:seller_register"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/register.html")
        self.assertTrue(response.context["is_public_registration"])
        self.assertContains(response, "Merchant Account &amp; Login Credentials")
        self.assertContains(response, "Store Display Name")
        self.assertContains(response, "GSTIN (15 Digits)")

    def test_public_seller_registration_creates_account_and_pending_seller(self):
        """
        Public merchant registration creates user account with hashed password,
        links customer profile, initializes Seller with STATUS_PENDING,
        and logs user in.
        """
        post_data = {
            "full_name": "Aarav Patel",
            "email": "aarav@pateltextiles.in",
            "mobile": "9876501234",
            "username": "aarav_textiles",
            "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!",
            "store_name": "Patel Handloom Textiles",
            "legal_business_name": "Patel Handloom Private Limited",
            "business_type": Seller.BUSINESS_TYPE_PVT_LTD,
            "store_description": "Finest cotton and silk handlooms from Gujarat.",
            "support_email": "support@pateltextiles.in",
            "support_phone": "9876501234",
            "registered_address": "Plot 45, GIDC Industrial Estate",
            "city": "Surat",
            "state": "Gujarat",
            "postal_code": "395001",
            "pickup_address": "Plot 45, GIDC Industrial Estate",
            "pickup_city": "Surat",
            "pickup_state": "Gujarat",
            "pickup_postal_code": "395001",
            "gstin": "24ABCDE1234F1Z9",
            "pan_number": "ABCDE1234F",
            "terms": "on",
        }

        response = self.client.post(reverse("core:seller_register"), post_data)
        self.assertRedirects(response, reverse("core:seller_status"))

        # Verify user creation & security
        user = User.objects.get(username="aarav_textiles")
        self.assertEqual(user.email, "aarav@pateltextiles.in")
        self.assertEqual(user.first_name, "Aarav")
        self.assertEqual(user.last_name, "Patel")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)

        # Critical: Password must NOT be stored in plain text
        self.assertNotEqual(user.password, "StrongPassword123!")
        self.assertTrue(check_password("StrongPassword123!", user.password))

        # Verify CustomerProfile phone
        profile = CustomerProfile.objects.get(user=user)
        self.assertEqual(profile.phone_number, "9876501234")

        # Verify Seller profile
        seller = Seller.objects.get(user=user)
        self.assertEqual(seller.store_name, "Patel Handloom Textiles")
        self.assertEqual(seller.legal_business_name, "Patel Handloom Private Limited")
        self.assertEqual(seller.status, Seller.STATUS_PENDING)
        self.assertTrue(seller.is_active)
        self.assertEqual(seller.commission_rate, Decimal("0.00"))
        self.assertFalse(seller.is_approved)
        self.assertFalse(seller.can_sell)
        self.assertEqual(seller.gstin, "24ABCDE1234F1Z9")

    def test_public_seller_registration_rejects_duplicate_credentials(self):
        """Prevents duplicate username, duplicate email, duplicate store name, and duplicate GSTIN."""
        base_data = {
            "full_name": "Test Vendor",
            "email": "unique@vendor.in",
            "mobile": "9876500000",
            "username": "unique_vendor",
            "password": "Password123!",
            "confirm_password": "Password123!",
            "store_name": "Unique Store Name",
            "legal_business_name": "Unique Entity Ltd",
            "business_type": Seller.BUSINESS_TYPE_PVT_LTD,
            "support_email": "support@unique.in",
            "support_phone": "9876500000",
            "registered_address": "Street Address",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560001",
            "gstin": "29XYZAB1234C1Z0",
            "terms": "on",
        }

        # 1. Duplicate username
        data1 = base_data.copy()
        data1["username"] = "approved_partner"  # Existing username
        resp1 = self.client.post(reverse("core:seller_register"), data1)
        self.assertEqual(resp1.status_code, 200)
        self.assertIn("username", resp1.context["form"].errors)
        self.assertIn("already taken", resp1.context["form"].errors["username"][0])

        # 2. Duplicate email
        data2 = base_data.copy()
        data2["email"] = "approved@partner.in"  # Existing email
        resp2 = self.client.post(reverse("core:seller_register"), data2)
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("email", resp2.context["form"].errors)
        self.assertIn("already exists", resp2.context["form"].errors["email"][0])

        # 3. Duplicate store name
        data3 = base_data.copy()
        data3["store_name"] = "Royal Heritage Apparel"  # Existing store name
        resp3 = self.client.post(reverse("core:seller_register"), data3)
        self.assertEqual(resp3.status_code, 200)
        self.assertIn("store_name", resp3.context["form"].errors)
        self.assertIn("already registered", resp3.context["form"].errors["store_name"][0])

        # 4. Duplicate GSTIN
        data4 = base_data.copy()
        data4["gstin"] = "29ABCDE1234F1Z5"  # Existing GSTIN
        resp4 = self.client.post(reverse("core:seller_register"), data4)
        self.assertEqual(resp4.status_code, 200)
        self.assertIn("gstin", resp4.context["form"].errors)
        self.assertIn("already registered", resp4.context["form"].errors["gstin"][0])

    def test_authenticated_customer_can_onboard_seller_profile(self):
        """Logged-in customer without seller profile can register a store attached to their account."""
        self.client.login(username="shopper_john", password="SecurePass123!")
        post_data = {
            "store_name": "John's Sports Gear",
            "legal_business_name": "John Sports Goods LLP",
            "business_type": Seller.BUSINESS_TYPE_LLP,
            "support_email": "john@sportsgear.in",
            "support_phone": "9876543299",
            "registered_address": "12 Sports Complex Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postal_code": "560001",
            "pickup_address": "12 Sports Complex Road",
            "pickup_city": "Bengaluru",
            "pickup_state": "Karnataka",
            "pickup_postal_code": "560001",
            "gstin": "29LMNOP1234Q1Z1",
            "pan_number": "LMNOP1234Q",
        }
        response = self.client.post(reverse("core:seller_register"), post_data)
        self.assertRedirects(response, reverse("core:seller_status"))

        seller = Seller.objects.get(user=self.customer_user)
        self.assertEqual(seller.store_name, "John's Sports Gear")
        self.assertEqual(seller.status, Seller.STATUS_PENDING)

    def test_staff_and_superuser_cannot_register_as_seller(self):
        """Staff members and administrators are blocked from registering as sellers."""
        self.client.login(username="fitmeezo_admin", password="AdminSuperPassword123!")
        response = self.client.get(reverse("core:seller_register"))
        self.assertRedirects(response, reverse("core:home"))

    # --------------------------------------------------------------------------
    # 2. Seller Portal Login (/seller/login/) & Routing Tests
    # --------------------------------------------------------------------------

    def test_seller_login_page_renders_cleanly(self):
        """Seller login page loads with proper merchant portal structure."""
        response = self.client.get(reverse("core:seller_login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "seller/login.html")
        self.assertContains(response, "Fitmeezo Seller Hub")
        self.assertContains(response, "Sign In to Seller Hub")
        self.assertContains(response, reverse("core:seller_register"))
        self.assertContains(response, reverse("core:seller_password_reset"))

    def test_approved_seller_login_redirects_to_dashboard(self):
        """Approved and active seller login directly redirects to Seller Dashboard."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "approved_partner",
            "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("core:seller_dashboard"))

        # Verify dashboard is accessible with 200 OK
        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(dash_resp.status_code, 200)
        self.assertTemplateUsed(dash_resp, "seller/dashboard.html")
        self.assertContains(dash_resp, "Royal Heritage Apparel")

    def test_pending_seller_login_redirects_to_status_page(self):
        """Pending seller login routes to application status view."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "pending_partner",
            "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("core:seller_status"))

        # Accessing dashboard directly must redirect to status page
        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertRedirects(dash_resp, reverse("core:seller_status"))

    def test_rejected_seller_login_redirects_to_status_page(self):
        """Rejected seller login routes to status view with error notice."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "rejected_partner",
            "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("core:seller_status"))

        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertRedirects(dash_resp, reverse("core:seller_status"))

    def test_suspended_seller_login_redirects_to_status_page(self):
        """Suspended seller login routes to status view with suspension alert."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "suspended_partner",
            "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("core:seller_status"))

        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertRedirects(dash_resp, reverse("core:seller_status"))

    def test_customer_without_seller_profile_redirected_to_registration(self):
        """Regular customer logging in at seller portal is guided to seller registration."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "shopper_john",
            "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("core:seller_register"))

    def test_staff_user_blocked_from_vendor_login_portal(self):
        """Staff/administrators cannot log in through the vendor login portal."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "fitmeezo_admin",
            "password": "AdminSuperPassword123!",
        })
        self.assertRedirects(response, reverse("core:seller_login"))

    def test_seller_login_with_email_identifier(self):
        """Merchants can sign in using their email address instead of username."""
        response = self.client.post(reverse("core:seller_login"), {
            "username_or_email": "approved@partner.in",
            "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("core:seller_dashboard"))

    # --------------------------------------------------------------------------
    # 3. Storefront Customer Login & Logout Verification
    # --------------------------------------------------------------------------

    def test_customer_storefront_login_remains_operational(self):
        """Customer login at /account/login/ operates normally without regression."""
        response = self.client.post(reverse("core:account_login"), {
            "username_or_email": "shopper_john",
            "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("core:account_dashboard"))

    def test_seller_logout_view(self):
        """Seller logout terminates session and redirects to seller login."""
        self.client.login(username="approved_partner", password="SecurePass123!",)
        response = self.client.post(reverse("core:seller_logout"))
        self.assertRedirects(response, reverse("core:seller_login"))

        # Dashboard requires authentication again
        dash_resp = self.client.get(reverse("core:seller_dashboard"))
        self.assertEqual(dash_resp.status_code, 302)

    # --------------------------------------------------------------------------
    # 4. Token-Based Password Reset Workflow
    # --------------------------------------------------------------------------

    def test_seller_password_reset_workflow(self):
        """Seller password reset generates token email and redirects to done page."""
        response = self.client.post(reverse("core:seller_password_reset"), {
            "email": "approved@partner.in",
        })
        self.assertRedirects(response, reverse("core:seller_password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Reset", mail.outbox[0].subject)
        self.assertIn("approved@partner.in", mail.outbox[0].to)
