"""
Management command: load_demo_data
===================================
Safely loads category and product fixtures for Fitmeezo on Render (or any fresh database).

Safety rules:
- Idempotent: skips loading if categories already exist (non-zero count).
- Never deletes existing records.
- Never imports user accounts, passwords, sessions, or OAuth tokens.
- Seller references in product fixtures must be null (seller rows are not included in demo data).
- Prints clear status output for build logs.
"""
import os
from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, ProgrammingError


class Command(BaseCommand):
    help = (
        "Idempotently loads category and product demo fixtures for Fitmeezo. "
        "Skips loading if records already exist. Safe to run on every deploy."
    )

    def handle(self, *args, **options):
        try:
            from core.models import Category, Product

            cat_count = Category.objects.count()
            prod_count = Product.objects.count()

            if cat_count > 0:
                self.stdout.write(
                    self.style.NOTICE(
                        f"[load_demo_data] Skipping fixture load: "
                        f"{cat_count} categories and {prod_count} products already exist in database."
                    )
                )
                return

            self.stdout.write(
                self.style.WARNING(
                    "[load_demo_data] No categories found — loading fixtures..."
                )
            )

            # Load fixtures in dependency order: categories first, then products.
            # Products must not reference seller rows — demo fixtures use seller=null.
            fixtures_dir = os.path.join(
                os.path.dirname(__file__), "..", "..", "fixtures"
            )
            categories_fixture = os.path.join(fixtures_dir, "categories.json")
            products_fixture = os.path.join(fixtures_dir, "products.json")

            if os.path.exists(categories_fixture):
                self.stdout.write("[load_demo_data] Loading categories.json ...")
                call_command("loaddata", categories_fixture, verbosity=1)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[load_demo_data] Categories loaded: {Category.objects.count()} total."
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"[load_demo_data] categories.json not found at {categories_fixture}, skipping."
                    )
                )

            if os.path.exists(products_fixture):
                self.stdout.write("[load_demo_data] Loading products.json ...")
                call_command("loaddata", products_fixture, verbosity=1)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[load_demo_data] Products loaded: {Product.objects.count()} total."
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"[load_demo_data] products.json not found at {products_fixture}, skipping."
                    )
                )

        except IntegrityError as exc:
            # A fixture FK violation (e.g. product references a missing seller/category).
            # Fail loudly so the issue is visible in Render build logs.
            self.stderr.write(
                self.style.ERROR(
                    f"[load_demo_data] FIXTURE INTEGRITY ERROR — check that all foreign-key "
                    f"references in fixtures point to rows that also exist in the fixtures: {exc}"
                )
            )
            raise SystemExit(1)

        except (OperationalError, ProgrammingError) as exc:
            # Tables may not exist yet (migration not yet applied); safe to skip.
            self.stdout.write(
                self.style.WARNING(
                    f"[load_demo_data] Database not ready yet, skipping: {exc}"
                )
            )
