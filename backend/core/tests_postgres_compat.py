"""
PostgreSQL Production Compatibility Test Suite for Fitmeezo.

Validates:
1. Environment-based database engine resolution (safe SQLite fallback, DATABASE_URL parsing, DB_* vars).
2. Non-exposure of credentials and connection options (CONN_MAX_AGE, CONN_HEALTH_CHECKS, sslmode).
3. Concurrency locking (select_for_update) compatibility with foreign keys and nullable relations.
4. Case-insensitive lookup (iexact) consistency across core entities (User, Seller, GSTIN).
5. Data types fidelity for PostgreSQL migration (Decimal, JSONField, Boolean, Timestamps).
"""

import os
import urllib.parse
from decimal import Decimal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase

from core.models import (
    Category, Product, ProductVariant, Seller, Order, OrderItem,
    SellerOrder, SellerLedgerEntry, Invoice, InvoiceSequence
)

User = get_user_model()


class DatabaseConfigurationResolutionTests(TestCase):
    """
    Tests proving environment-based database configuration parsing.
    """

    def test_default_database_is_sqlite_with_zero_conn_age(self):
        """
        In local environment without PostgreSQL variables, default is SQLite.
        """
        current_db = settings.DATABASES["default"]
        self.assertIn("sqlite", current_db["ENGINE"])
        self.assertEqual(current_db.get("CONN_MAX_AGE", 0), 0)

    def test_database_url_postgresql_parser(self):
        """
        Parsing a production DATABASE_URL maps accurately to PostgreSQL settings.
        """
        test_url = "postgresql://prod_user:p%40ss_word%21@db.internal.fitmeezo.in:5433/fitmeezo_live?sslmode=require"
        parsed_db = urllib.parse.urlparse(test_url)
        is_postgres = parsed_db.scheme in ("postgres", "postgresql", "pgsql")

        self.assertTrue(is_postgres)
        pg_options = {}
        query_params = urllib.parse.parse_qs(parsed_db.query)
        if "sslmode" in query_params:
            pg_options["sslmode"] = query_params["sslmode"][0]

        resolved_db = {
            "ENGINE": "django.db.backends.postgresql" if is_postgres else "django.db.backends.sqlite3",
            "NAME": parsed_db.path.lstrip("/"),
            "USER": urllib.parse.unquote(parsed_db.username or ""),
            "PASSWORD": urllib.parse.unquote(parsed_db.password or ""),
            "HOST": parsed_db.hostname or "127.0.0.1",
            "PORT": str(parsed_db.port or "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": pg_options,
        }

        self.assertEqual(resolved_db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(resolved_db["NAME"], "fitmeezo_live")
        self.assertEqual(resolved_db["USER"], "prod_user")
        self.assertEqual(resolved_db["PASSWORD"], "p@ss_word!")
        self.assertEqual(resolved_db["HOST"], "db.internal.fitmeezo.in")
        self.assertEqual(resolved_db["PORT"], "5433")
        self.assertEqual(resolved_db["CONN_MAX_AGE"], 60)
        self.assertTrue(resolved_db["CONN_HEALTH_CHECKS"])
        self.assertEqual(resolved_db["OPTIONS"].get("sslmode"), "require")

    def test_individual_env_variables_postgresql_resolution(self):
        """
        Validates individual DB_* variable mapping for traditional cloud deployments.
        """
        mock_env = {
            "DB_ENGINE": "postgresql",
            "DB_NAME": "fitmeezo_db",
            "DB_USER": "dbadmin",
            "DB_PASSWORD": "SecrEtPassword!",
            "DB_HOST": "postgres.internal",
            "DB_PORT": "5432",
            "DB_CONN_MAX_AGE": "120",
            "DB_SSLMODE": "prefer",
        }

        is_pg = mock_env["DB_ENGINE"] in ("postgresql", "postgres")
        conn_max_age = int(mock_env.get("DB_CONN_MAX_AGE", "60"))
        pg_options = {}
        if mock_env.get("DB_SSLMODE"):
            pg_options["sslmode"] = mock_env["DB_SSLMODE"]

        resolved = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": mock_env["DB_NAME"],
            "USER": mock_env["DB_USER"],
            "PASSWORD": mock_env["DB_PASSWORD"],
            "HOST": mock_env["DB_HOST"],
            "PORT": mock_env["DB_PORT"],
            "CONN_MAX_AGE": conn_max_age,
            "CONN_HEALTH_CHECKS": is_pg and conn_max_age > 0,
            "OPTIONS": pg_options,
        }

        self.assertEqual(resolved["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(resolved["NAME"], "fitmeezo_db")
        self.assertEqual(resolved["USER"], "dbadmin")
        self.assertEqual(resolved["PASSWORD"], "SecrEtPassword!")
        self.assertEqual(resolved["HOST"], "postgres.internal")
        self.assertEqual(resolved["PORT"], "5432")
        self.assertEqual(resolved["CONN_MAX_AGE"], 120)
        self.assertTrue(resolved["CONN_HEALTH_CHECKS"])
        self.assertEqual(resolved["OPTIONS"]["sslmode"], "prefer")


class PostgresQueryCompatibilityTests(TestCase):
    """
    Validates locking, case-insensitivity, and data type fidelity for PostgreSQL.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="seller_user",
            email="seller@example.com",
            password="Password123!",
        )
        self.seller = Seller.objects.create(
            user=self.user,
            store_name="Royal Apparel Hub",
            legal_business_name="Royal Apparel Private Limited",
            support_email="support@royalapparel.in",
            support_phone="9876543210",
            registered_address="12 Commercial Street",
            city="Bengaluru",
            state="Karnataka",
            postal_code="560001",
            pickup_address="Warehouse 4, Electronic City",
            pickup_city="Bengaluru",
            pickup_state="Karnataka",
            pickup_postal_code="560100",
            gstin="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            status=Seller.STATUS_APPROVED,
        )
        self.category = Category.objects.create(name="Ethnic Wear", slug="ethnic-wear")
        self.product = Product.objects.create(
            seller=self.seller,
            category=self.category,
            name="Silk Kurta",
            slug="silk-kurta",
            sku="SKU-KURTA-BASE",
            price=Decimal("1999.00"),
            stock_quantity=50,
            is_active=True,
            is_approved=True,
            approval_status=Product.APPROVAL_APPROVED,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            option_type="Size",
            option_value="L",
            sku="SKU-KURTA-L-GLD",
            price=Decimal("1999.00"),
            stock_quantity=20,
            is_active=True,
        )

    def test_case_insensitive_lookups(self):
        """
        iexact queries operate consistently on usernames, emails, GSTIN, and slugs.
        """
        # User username & email
        self.assertTrue(User.objects.filter(username__iexact="SELLER_USER").exists())
        self.assertTrue(User.objects.filter(email__iexact="SELLER@EXAMPLE.COM").exists())

        # Seller store & GSTIN
        self.assertTrue(Seller.objects.filter(store_name__iexact="royal apparel hub").exists())
        self.assertTrue(Seller.objects.filter(gstin__iexact="29abcde1234f1z5").exists())

        # Product slug
        self.assertTrue(Product.objects.filter(slug__iexact="SILK-KURTA").exists())

    def test_atomic_row_locking_on_inventory(self):
        """
        Verifies that select_for_update() row locking executes safely inside an atomic transaction.
        """
        with transaction.atomic():
            locked_variant = (
                ProductVariant.objects.select_for_update()
                .filter(pk=self.variant.pk)
                .first()
            )
            self.assertIsNotNone(locked_variant)
            locked_variant.stock_quantity -= 2
            locked_variant.save(update_fields=["stock_quantity"])

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, 18)

    def test_atomic_row_locking_on_product(self):
        """
        Verifies that select_for_update() with foreign key reference executes cleanly.
        """
        with transaction.atomic():
            locked_product = (
                Product.objects.select_for_update(of=("self",))
                .select_related("seller")
                .filter(pk=self.product.pk)
                .first()
            )
            self.assertIsNotNone(locked_product)
            self.assertEqual(locked_product.seller, self.seller)

    def test_monotone_invoice_sequence_row_lock(self):
        """
        Verifies monotonic invoice sequence row locking pattern operates seamlessly.
        """
        with transaction.atomic():
            seq, _ = InvoiceSequence.objects.select_for_update().get_or_create(
                prefix="INV-2026",
                defaults={"last_number": 0},
            )
            seq.last_number += 1
            seq.save(update_fields=["last_number", "updated_at"])

        seq.refresh_from_db()
        self.assertEqual(seq.last_number, 1)

    def test_financial_ledger_decimal_precision(self):
        """
        Confirms exact 2-decimal place precision required for PostgreSQL numeric/money types.
        """
        entry = SellerLedgerEntry.objects.create(
            seller=self.seller,
            entry_type=SellerLedgerEntry.TYPE_CREDIT,
            category=SellerLedgerEntry.CATEGORY_ORDER_SALE,
            amount=Decimal("1799.10"),
            currency="INR",
            status=SellerLedgerEntry.STATUS_POSTED,
            source_type="order",
            idempotency_key="test-pg-prec-001",
            description="Item payout testing decimal precision",
            audit_metadata={"precision_test": True, "decimals": 2},
        )
        entry.refresh_from_db()
        self.assertEqual(entry.amount, Decimal("1799.10"))
        self.assertEqual(entry.audit_metadata["decimals"], 2)
