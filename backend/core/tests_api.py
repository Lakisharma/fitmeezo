"""
Fitmeezo REST API & SEO Test Suite.
Verifies catalogue endpoints, customer authentication, token management,
cart operations, address ownership isolation, checkout order creation,
safe inventory deduction, order data privacy, robots.txt, and sitemap.xml.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Address, Category, Order, OrderItem, Product, ProductVariant, Seller

User = get_user_model()


class APICatalogueTests(TestCase):
    """Tests public catalogue browsing endpoints."""

    def setUp(self):
        self.client = APIClient()
        self.cat1 = Category.objects.create(name="Electronics", slug="electronics", is_active=True)
        self.cat_inactive = Category.objects.create(name="Secret Deals", slug="secret-deals", is_active=False)

        self.prod1 = Product.objects.create(
            name="FitPulse Smartwatch",
            slug="fitpulse-smartwatch",
            sku="SMW-FP-001",
            category=self.cat1,
            price=Decimal("2499.00"),
            compare_at_price=Decimal("3499.00"),
            stock_quantity=20,
            is_active=True,
            is_featured=True,
        )
        self.prod2 = Product.objects.create(
            name="Wireless Neckband",
            slug="wireless-neckband",
            sku="AUD-NCK-001",
            category=self.cat1,
            price=Decimal("899.00"),
            stock_quantity=0,  # Out of stock
            is_active=True,
        )
        self.prod_inactive = Product.objects.create(
            name="Draft Item",
            slug="draft-item",
            sku="DRF-001",
            category=self.cat1,
            price=Decimal("500.00"),
            stock_quantity=10,
            is_active=False,
        )

    def test_category_list_api(self):
        """Active categories are returned; inactive categories are excluded."""
        resp = self.client.get(reverse("api:category_list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        slugs = [c["slug"] for c in data["categories"]]
        self.assertIn("electronics", slugs)
        self.assertNotIn("secret-deals", slugs)

    def test_category_detail_api(self):
        """Category details include active products only."""
        resp = self.client.get(reverse("api:category_detail", kwargs={"slug": "electronics"}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        prods = [p["slug"] for p in data["category"]["products"]]
        self.assertIn("fitpulse-smartwatch", prods)
        self.assertNotIn("draft-item", prods)

    def test_product_list_api_search_and_filter(self):
        """Product list supports query search, in_stock filter, and sorting."""
        # 1. Search by name/SKU
        resp = self.client.get(reverse("api:product_list"), {"q": "Smartwatch"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["sku"], "SMW-FP-001")

        # 2. In-stock filter
        resp_stock = self.client.get(reverse("api:product_list"), {"in_stock": "true"})
        self.assertEqual(resp_stock.status_code, status.HTTP_200_OK)
        stock_data = resp_stock.json()
        skus = [p["sku"] for p in stock_data["results"]]
        self.assertIn("SMW-FP-001", skus)
        self.assertNotIn("AUD-NCK-001", skus)  # Out of stock excluded

    def test_product_detail_api(self):
        """Product detail returns specs, rating breakdown, and in-stock status."""
        resp = self.client.get(reverse("api:product_detail", kwargs={"slug": "fitpulse-smartwatch"}))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        p = data["product"]
        self.assertEqual(p["sku"], "SMW-FP-001")
        self.assertEqual(p["price"], "2499.00")
        self.assertTrue(p["is_in_stock"])
        self.assertIn("rating_breakdown", p)


class APIAuthenticationAndProfileTests(TestCase):
    """Tests customer registration, token authentication, and profile endpoints."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="existing_user",
            email="existing@example.com",
            password="StrongPassword123!",
            first_name="Rohan",
            last_name="Sharma",
        )
        self.token = Token.objects.create(user=self.user)

    def test_api_register_success(self):
        """New customer registers and receives an authentication token."""
        payload = {
            "full_name": "Priya Patel",
            "email": "priya@example.com",
            "password": "SecurePassword987!",
            "confirm_password": "SecurePassword987!",
        }
        resp = self.client.post(reverse("api:register"), payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertIn("token", data)
        self.assertEqual(data["user"]["email"], "priya@example.com")
        self.assertTrue(User.objects.filter(email="priya@example.com").exists())

    def test_api_register_duplicate_email_fails(self):
        """Duplicate registration is rejected with an informative error."""
        payload = {
            "full_name": "Duplicate User",
            "email": "existing@example.com",
            "password": "SecurePassword987!",
            "confirm_password": "SecurePassword987!",
        }
        resp = self.client.post(reverse("api:register"), payload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()
        self.assertIn("email", data["errors"])

    def test_api_login_success_returns_token(self):
        """Customer logs in via email or username and gets a token."""
        resp = self.client.post(
            reverse("api:login"),
            {"username_or_email": "existing@example.com", "password": "StrongPassword123!"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["token"], self.token.key)

    def test_api_login_invalid_credentials_fails(self):
        """Invalid password returns HTTP 400 Bad Request."""
        resp = self.client.post(
            reverse("api:login"),
            {"username_or_email": "existing@example.com", "password": "WrongPassword!"},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_profile_authenticated(self):
        """Authenticated request with Token header accesses profile."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        resp = self.client.get(reverse("api:profile"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["profile"]["email"], "existing@example.com")
        self.assertEqual(data["profile"]["full_name"], "Rohan Sharma")

    def test_api_profile_unauthenticated_fails(self):
        """Unauthenticated request to profile endpoint is rejected with 401 Unauthorized."""
        resp = self.client.get(reverse("api:profile"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_api_profile_update(self):
        """Customer updates full_name and phone_number."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        resp = self.client.patch(
            reverse("api:profile"),
            {"full_name": "Rohan Verma", "phone_number": "9876543210"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Rohan")
        self.assertEqual(self.user.last_name, "Verma")
        self.assertEqual(self.user.profile.phone_number, "9876543210")

    def test_api_logout(self):
        """Logout deletes user token."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        resp = self.client.post(reverse("api:logout"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(Token.objects.filter(user=self.user).exists())


class APIAddressTests(TestCase):
    """Tests address management and multi-tenant customer isolation."""

    def setUp(self):
        self.client1 = APIClient()
        self.client2 = APIClient()

        self.user1 = User.objects.create_user(username="cust_one", email="one@test.com", password="Pass123!")
        self.token1 = Token.objects.create(user=self.user1)
        self.client1.credentials(HTTP_AUTHORIZATION=f"Token {self.token1.key}")

        self.user2 = User.objects.create_user(username="cust_two", email="two@test.com", password="Pass123!")
        self.token2 = Token.objects.create(user=self.user2)
        self.client2.credentials(HTTP_AUTHORIZATION=f"Token {self.token2.key}")

        self.addr1 = Address.objects.create(
            user=self.user1,
            full_name="User One",
            phone_number="9876543210",
            address_line1="101 First Ave",
            city="Pune",
            state="Maharashtra",
            postal_code="411001",
            is_default=True,
        )

    def test_address_list_and_create(self):
        """Customer lists their addresses and creates a new one."""
        # List
        resp = self.client1.get(reverse("api:address_list_create"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()["addresses"]), 1)

        # Create
        new_payload = {
            "full_name": "User One Office",
            "phone_number": "9123456789",
            "address_line1": "Tech Park Tower B",
            "city": "Pune",
            "state": "Maharashtra",
            "postal_code": "411002",
            "address_type": "work",
        }
        create_resp = self.client1.post(reverse("api:address_list_create"), new_payload)
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Address.objects.filter(user=self.user1).count(), 2)

    def test_address_customer_isolation(self):
        """Customer 2 cannot inspect, modify, or delete Customer 1's address."""
        # GET by other user returns 404
        resp = self.client2.get(reverse("api:address_detail", kwargs={"pk": self.addr1.pk}))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

        # DELETE by other user returns 404 and does not delete
        del_resp = self.client2.delete(reverse("api:address_detail", kwargs={"pk": self.addr1.pk}))
        self.assertEqual(del_resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Address.objects.filter(pk=self.addr1.pk).exists())


class APICartAndCheckoutTests(TestCase):
    """Tests shopping cart, checkout, atomic inventory deduction, and customer orders."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="shopper", email="shopper@test.com", password="Pass123!")
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

        self.category = Category.objects.create(name="Fitness", slug="fitness", is_active=True)

        self.product = Product.objects.create(
            name="Yoga Mat Pro",
            slug="yoga-mat-pro",
            sku="YG-MAT-001",
            category=self.category,
            price=Decimal("999.00"),
            stock_quantity=10,
            is_active=True,
        )

        self.address = Address.objects.create(
            user=self.user,
            full_name="Shopper",
            phone_number="9876543210",
            address_line1="77 Green Way",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            is_default=True,
        )

    def test_api_cart_operations(self):
        """Tests adding to cart, getting cart state, updating quantity, and clearing cart."""
        # 1. Add item
        add_resp = self.client.post(reverse("api:cart_add"), {"product_id": self.product.id, "quantity": 2})
        self.assertEqual(add_resp.status_code, status.HTTP_200_OK)
        cart_data = add_resp.json()["cart"]
        self.assertEqual(cart_data["total_units"], 2)
        self.assertEqual(cart_data["subtotal"], "1998.00")
        key = add_resp.json()["key"]

        # 2. Update quantity
        upd_resp = self.client.post(reverse("api:cart_update"), {"key": key, "quantity": 3})
        self.assertEqual(upd_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(upd_resp.json()["cart"]["total_units"], 3)

        # 3. Exceed stock validation
        excess_resp = self.client.post(reverse("api:cart_update"), {"key": key, "quantity": 50})
        self.assertEqual(excess_resp.status_code, status.HTTP_400_BAD_REQUEST)

        # 4. View Cart
        view_resp = self.client.get(reverse("api:cart_detail"))
        self.assertEqual(view_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(view_resp.json()["cart"]["total_units"], 3)

        # 5. Clear cart
        clear_resp = self.client.post(reverse("api:cart_clear"))
        self.assertEqual(clear_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(clear_resp.json()["cart"]["total_units"], 0)

    def test_api_checkout_place_order_cod_deducts_inventory(self):
        """Placing COD order locks rows, snapshots items, deducts stock, and clears cart."""
        # Add 3 units to cart
        self.client.post(reverse("api:cart_add"), {"product_id": self.product.id, "quantity": 3})

        # Checkout summary check
        summary_resp = self.client.get(reverse("api:checkout_summary"))
        self.assertEqual(summary_resp.status_code, status.HTTP_200_OK)
        # 3 * 999.00 = 2997.00 >= 499 free shipping threshold
        self.assertEqual(summary_resp.json()["shipping_fee"], "0.00")

        # Place Order
        order_payload = {
            "address_id": self.address.id,
            "payment_method": "cod",
            "customer_notes": "Please deliver before 5 PM",
            "idempotency_token": "token-test-12345",
        }
        order_resp = self.client.post(reverse("api:checkout_place_order"), order_payload)
        self.assertEqual(order_resp.status_code, status.HTTP_201_CREATED)
        order_data = order_resp.json()["order"]

        # Inventory check: original 10 - 3 = 7
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 7)

        # Cart check: cart must be empty
        cart_resp = self.client.get(reverse("api:cart_detail"))
        self.assertEqual(cart_resp.json()["cart"]["total_units"], 0)

        # Order details check
        self.assertEqual(order_data["status"], Order.STATUS_PENDING)
        self.assertEqual(order_data["payment_method"], Order.PAYMENT_METHOD_COD)
        self.assertEqual(order_data["payment_status"], Order.PAYMENT_STATUS_UNPAID)
        self.assertEqual(len(order_data["items"]), 1)
        self.assertEqual(order_data["items"][0]["sku"], "YG-MAT-001")

        # Idempotency check: repeated request returns already created order
        repeat_resp = self.client.post(reverse("api:checkout_place_order"), order_payload)
        self.assertEqual(repeat_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(repeat_resp.json()["order"]["order_number"], order_data["order_number"])
        # Inventory not deducted again!
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 7)

    def test_api_checkout_commission_snapshot_with_seller_and_direct_product(self):
        """
        Placing an order via REST API with a seller-backed product and direct product
        snapshots Seller, commission_rate, and commission_amount with exact Decimal arithmetic.
        Subsequent commission rate updates do not mutate historical snapshots.
        """
        # 1. Create an approved seller with a 12.50% commission rate
        seller_user = User.objects.create_user(
            username="apiseller@example.com",
            email="apiseller@example.com",
            password="SecurePass123!",
        )
        seller = Seller.objects.create(
            user=seller_user,
            store_name="Zenith Athletics",
            legal_business_name="Zenith Athletics LLP",
            support_email="support@zenith.in",
            support_phone="9876543210",
            registered_address="10 MG Road",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="10 MG Road",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560001",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            commission_rate=Decimal("12.50"),
            status=Seller.STATUS_APPROVED,
            is_active=True,
        )

        # 2. Create a seller-assigned product
        seller_product = Product.objects.create(
            name="Zenith Resistance Bands",
            slug="zenith-resistance-bands",
            sku="ZN-RES-001",
            category=self.category,
            price=Decimal("1200.00"),
            stock_quantity=20,
            is_active=True,
            seller=seller,
        )

        # 3. Add 2 units of seller product (line total: 2400.00, commission: 300.00)
        # and 1 unit of self.product (seller=None, line total: 999.00, commission: 0.00)
        self.client.post(reverse("api:cart_add"), {"product_id": seller_product.id, "quantity": 2})
        self.client.post(reverse("api:cart_add"), {"product_id": self.product.id, "quantity": 1})

        # 4. Place order via REST API
        order_payload = {
            "address_id": self.address.id,
            "payment_method": "cod",
            "customer_notes": "Deliver securely",
            "idempotency_token": "api-snap-tok-001",
        }
        resp = self.client.post(reverse("api:checkout_place_order"), order_payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        order = Order.objects.get(idempotency_key="api-snap-tok-001")
        seller_item = order.items.get(product=seller_product)
        direct_item = order.items.get(product=self.product)

        # 5. Verify seller item historical snapshot
        self.assertEqual(seller_item.seller, seller)
        self.assertEqual(seller_item.commission_rate, Decimal("12.50"))
        self.assertEqual(seller_item.line_total, Decimal("2400.00"))
        self.assertEqual(seller_item.commission_amount, Decimal("300.00"))

        # 6. Verify direct item (seller=None) snapshot
        self.assertIsNone(direct_item.seller)
        self.assertEqual(direct_item.commission_rate, Decimal("0.00"))
        self.assertEqual(direct_item.line_total, Decimal("999.00"))
        self.assertEqual(direct_item.commission_amount, Decimal("0.00"))

        # 7. Modify seller commission to 25.00%
        seller.commission_rate = Decimal("25.00")
        seller.save(update_fields=["commission_rate", "updated_at"])

        # Historical order item must remain unchanged
        seller_item.refresh_from_db()
        self.assertEqual(seller_item.commission_rate, Decimal("12.50"))
        self.assertEqual(seller_item.commission_amount, Decimal("300.00"))

    def test_api_order_privacy_isolation(self):
        """Customers can only inspect their own orders in list and detail."""
        order = Order.objects.create(
            order_number="FM-PRIV-001",
            user=self.user,
            shipping_full_name="Shopper",
            shipping_phone_number="9876543210",
            shipping_address_line1="123 Test",
            shipping_city="City",
            shipping_state="State",
            shipping_postal_code="560001",
            subtotal=Decimal("999.00"),
            shipping_fee=Decimal("0.00"),
            total_amount=Decimal("999.00"),
        )

        # Customer 1 can see their order
        resp1 = self.client.get(reverse("api:order_detail", kwargs={"order_number": "FM-PRIV-001"}))
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)

        # Customer 2 gets 404
        user2 = User.objects.create_user(username="other_cust", email="other@test.com", password="Pass123!")
        token2 = Token.objects.create(user=user2)
        client2 = APIClient()
        client2.credentials(HTTP_AUTHORIZATION=f"Token {token2.key}")

        resp2 = client2.get(reverse("api:order_detail", kwargs={"order_number": "FM-PRIV-001"}))
        self.assertEqual(resp2.status_code, status.HTTP_404_NOT_FOUND)


class APISiteMetadataAndSEOTests(TestCase):
    """Tests robots.txt, sitemap.xml, and 404 error template."""

    def setUp(self):
        self.client = Client()
        Category.objects.create(name="Bags", slug="bags", is_active=True)

    def test_robots_txt(self):
        """robots.txt returns 200 with standard search engine directives."""
        resp = self.client.get("/robots.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp["Content-Type"])
        content = resp.content.decode("utf-8")
        self.assertIn("User-agent: *", content)
        self.assertIn("Disallow: /admin/", content)
        self.assertIn("Disallow: /custom-admin/", content)
        self.assertIn("Allow: /categories/", content)
        self.assertIn("Sitemap:", content)

    def test_sitemap_xml(self):
        """sitemap.xml returns 200 with valid XML urlset containing active categories."""
        resp = self.client.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/xml", resp["Content-Type"])
        content = resp.content.decode("utf-8")
        self.assertIn("<urlset", content)
        self.assertIn("/categories/bags/", content)

    def test_404_page(self):
        """Non-existent URL renders custom branded 404 template."""
        resp = self.client.get("/this-url-definitely-does-not-exist-404/")
        self.assertEqual(resp.status_code, 404)
        self.assertTemplateUsed(resp, "404.html")
        self.assertContains(resp, "ERROR 404", status_code=404)
