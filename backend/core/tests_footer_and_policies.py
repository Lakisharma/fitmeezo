"""
Comprehensive Test Suite for Fitmeezo Footer Links, Informational Pages,
Contact Inquiry Submissions, Order Tracking Security, and Policy Verification.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from core.models import (
    ContactInquiry,
    Order,
    OrderStatusHistory,
    Seller,
    SellerOrder,
)

User = get_user_model()


class FooterLinksAndPolicyPagesTest(TestCase):
    """
    Validates that every single footer link resolves to a valid URL,
    returns HTTP 200, displays correct brand metadata, and eliminates
    Phase 1 placeholder notices.
    """

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testcustomer",
            email="customer@example.com",
            first_name="Anita",
            last_name="Desai",
            password="StrongPassword123!",
        )

    def test_all_footer_links_resolve_and_return_200(self):
        """Every footer link in all 3 sections must return 200 OK."""
        routes = [
            # Section 1: Customer Service
            ("core:contact", "Contact Us"),
            ("core:order_track", "Track Your Order"),
            ("core:shipping_policy", "Shipping &amp; Delivery Policy"),
            ("core:return_policy", "Return &amp; Refund Policy"),
            ("core:faqs", "Frequently Asked Questions"),
            # Section 2: About Fitmeezo
            ("core:about", "About Us"),
            ("core:careers", "Careers"),
            ("core:guarantee", "100% Genuine"),
            ("core:seller_register", "Register"),
            ("core:press", "Newsroom"),
            # Section 3: Legal & Connect
            ("core:privacy_policy", "Privacy Policy"),
            ("core:terms", "Terms &amp; Conditions"),
            ("core:refund_cancellation_policy", "Refund &amp; Cancellation"),
            ("core:cookie_policy", "Cookie Policy"),
            ("core:grievance_policy", "Grievance Redressal"),
        ]

        for route_name, expected_text in routes:
            url = reverse(route_name)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, f"Route {route_name} ({url}) returned {resp.status_code}")
            content = resp.content.decode("utf-8")
            self.assertIn(expected_text, content, f"Expected '{expected_text}' in response for {route_name}")

    def test_footer_rendered_on_homepage_has_no_placeholder_attributes(self):
        """Footer rendered on homepage must not have data-placeholder in footer links."""
        resp = self.client.get(reverse("core:home"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        # Verify all 3 section headings exist
        self.assertIn("Customer Service", content)
        self.assertIn("About Fitmeezo", content)
        self.assertIn("Legal &amp; Connect", content)

        # Verify required strings
        self.assertIn("940885323", content)
        self.assertIn("support@fitmeezo.com", content)

        # Verify no footer link has data-placeholder="true"
        self.assertNotIn('class="fm-footer-link" data-placeholder="true"', content)

    def test_contact_form_submission_success(self):
        """Submitting valid contact form persists inquiry and redirects with success message."""
        payload = {
            "full_name": "Vikram Singh",
            "email": "vikram@example.com",
            "phone_number": "+91 9876543210",
            "subject": "order",
            "order_number": "FM-20261010-AB12",
            "message": "I would like to inquire about my delivery timeline.",
        }
        resp = self.client.post(reverse("core:contact"), data=payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Your message has been received")

        # Verify database record
        inquiry = ContactInquiry.objects.filter(email="vikram@example.com").first()
        self.assertIsNotNone(inquiry)
        self.assertEqual(inquiry.full_name, "Vikram Singh")
        self.assertEqual(inquiry.subject, "order")
        self.assertEqual(inquiry.order_number, "FM-20261010-AB12")
        self.assertEqual(inquiry.status, ContactInquiry.STATUS_NEW)

    def test_contact_form_validation_errors(self):
        """Submitting invalid contact form fails gracefully with specific field errors."""
        payload = {
            "full_name": "A",  # too short (< 2)
            "email": "invalid-email-format",
            "phone_number": "",
            "subject": "other",
            "order_number": "",
            "message": "Short",  # too short (< 10)
        }
        resp = self.client.post(reverse("core:contact"), data=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertFormError(resp.context["form"], "full_name", "Please enter your full name (at least 2 characters).")
        self.assertFormError(resp.context["form"], "email", "Enter a valid email address.")
        self.assertFormError(resp.context["form"], "message", "Your message is too short. Please provide at least 10 characters.")

    def test_order_tracking_authorized_success_with_email(self):
        """Authorized customer can track order using Order Number and matching checkout email."""
        order = Order.objects.create(
            order_number="FM-TEST-TRACK-01",
            user=self.user,
            shipping_full_name="Anita Desai",
            shipping_phone_number="+91 9876543210",
            shipping_address_line1="123 MG Road",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_postal_code="560001",
            subtotal=Decimal("999.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("999.00"),
            status=Order.STATUS_CONFIRMED,
            payment_method=Order.PAYMENT_METHOD_RAZORPAY,
            payment_status=Order.PAYMENT_STATUS_PAID,
        )
        OrderStatusHistory.objects.create(
            order=order,
            from_status=Order.STATUS_PENDING,
            to_status=Order.STATUS_CONFIRMED,
            notes="Order verified and confirmed.",
        )

        resp = self.client.get(
            reverse("core:order_track"),
            {"order_number": "FM-TEST-TRACK-01", "verification": "customer@example.com"}
        )
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("FM-TEST-TRACK-01", content)
        self.assertIn("Fulfillment Progress", content)
        self.assertIn("Customer privacy protected with field masking", content)
        # Customer name must be masked
        self.assertIn("A*** i", content)

    def test_order_tracking_authorized_success_with_phone(self):
        """Authorized customer can track order using Order Number and matching phone digits."""
        order = Order.objects.create(
            order_number="FM-TEST-TRACK-02",
            user=self.user,
            shipping_full_name="Rohan Varma",
            shipping_phone_number="+91 9876500000",
            shipping_address_line1="45 Anna Salai",
            shipping_city="Chennai",
            shipping_state="Tamil Nadu",
            shipping_postal_code="600002",
            subtotal=Decimal("499.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("499.00"),
            status=Order.STATUS_SHIPPED,
            payment_method=Order.PAYMENT_METHOD_COD,
            payment_status=Order.PAYMENT_STATUS_UNPAID,
        )

        resp = self.client.get(
            reverse("core:order_track"),
            {"order_number": "FM-TEST-TRACK-02", "verification": "9876500000"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "FM-TEST-TRACK-02")
        self.assertContains(resp, "Shipped")

    def test_order_tracking_unauthorized_rejection(self):
        """Tracking attempt with mismatched contact detail is rejected and does not expose data."""
        order = Order.objects.create(
            order_number="FM-TEST-SECRET-03",
            user=self.user,
            shipping_full_name="Secret Customer",
            shipping_phone_number="+91 9999999999",
            shipping_address_line1="Private Villa",
            shipping_city="Mumbai",
            shipping_state="Maharashtra",
            shipping_postal_code="400001",
            subtotal=Decimal("1500.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("1500.00"),
            status=Order.STATUS_CONFIRMED,
        )

        resp = self.client.get(
            reverse("core:order_track"),
            {"order_number": "FM-TEST-SECRET-03", "verification": "wrongperson@example.com"}
        )
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("We could not find an order matching that order number", content)
        self.assertNotIn("Private Villa", content)
        self.assertNotIn("Secret Customer", content)

    def test_shipping_policy_substantiates_business_rules(self):
        """Shipping policy must document the ₹499 free shipping threshold and ₹49 flat fee."""
        resp = self.client.get(reverse("core:shipping_policy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "₹499.00")
        self.assertContains(resp, "FREE Delivery")
        self.assertContains(resp, "₹49.00")
        self.assertContains(resp, "Cash on Delivery")

    def test_return_policy_substantiates_7_day_window(self):
        """Return policy must document the verified 7-day return window and doorstep pickup."""
        resp = self.client.get(reverse("core:return_policy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "7 calendar days")
        self.assertContains(resp, "doorstep")
        self.assertContains(resp, "5 to 7 Business Days")

    def test_grievance_policy_statutory_details(self):
        """Grievance policy must document statutory acknowledgment within 48h and 30-day resolution."""
        resp = self.client.get(reverse("core:grievance_policy"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Consumer Protection (E-Commerce) Rules, 2020")
        self.assertContains(resp, "grievance@fitmeezo.com")
        self.assertContains(resp, "48 hours")
        self.assertContains(resp, "30 calendar days")

    def test_careers_page_honest_status(self):
        """Careers page must clearly state hiring status without inventing vacancies."""
        resp = self.client.get(reverse("core:careers"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Current Recruitment Status")
        self.assertContains(resp, "careers@fitmeezo.com")
