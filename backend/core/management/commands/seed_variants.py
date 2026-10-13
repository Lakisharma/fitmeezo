from decimal import Decimal
from django.core.management.base import BaseCommand
from core.models import Category, Product, ProductVariant


class Command(BaseCommand):
    help = "Seeds sample products and product variants for Fitmeezo (idempotent)."

    def handle(self, *args, **options):
        # 1. Fashion Category - T-Shirt with Sizes
        fashion, _ = Category.objects.get_or_create(
            slug="fashion",
            defaults={"name": "Fashion", "display_order": 2, "is_active": True},
        )

        tshirt, created = Product.objects.get_or_create(
            slug="classic-crewneck-tee",
            defaults={
                "name": "Fitmeezo Signature Cotton Crewneck T-Shirt",
                "sku": "FM-TSH-001",
                "category": fashion,
                "short_description": "100% combed organic cotton everyday crewneck t-shirt.",
                "description": "Crafted from 180 GSM breathable combed cotton. Double-stitched seams and pre-shrunk fabric ensure longevity through repeated washes.",
                "price": Decimal("599.00"),
                "compare_at_price": Decimal("899.00"),
                "stock_quantity": 0,
                "is_active": True,
                "is_featured": True,
            },
        )

        variants_tshirt = [
            {"type": "Size", "value": "S", "sku": "FM-TSH-S", "stock": 12, "price": None, "compare": None},
            {"type": "Size", "value": "M", "sku": "FM-TSH-M", "stock": 25, "price": None, "compare": None},
            {"type": "Size", "value": "L", "sku": "FM-TSH-L", "stock": 18, "price": None, "compare": None},
            {"type": "Size", "value": "XL", "sku": "FM-TSH-XL", "stock": 0, "price": None, "compare": None},  # Out of stock
            {"type": "Size", "value": "XXL", "sku": "FM-TSH-XXL", "stock": 6, "price": Decimal("649.00"), "compare": Decimal("999.00")},
        ]
        for v in variants_tshirt:
            ProductVariant.objects.get_or_create(
                product=tshirt,
                sku=v["sku"],
                defaults={
                    "option_type": v["type"],
                    "option_value": v["value"],
                    "stock_quantity": v["stock"],
                    "price": v["price"],
                    "compare_at_price": v["compare"],
                    "is_active": True,
                },
            )

        # 2. Electronics Category - Earbuds with Color options
        electronics, _ = Category.objects.get_or_create(
            slug="electronics",
            defaults={"name": "Electronics", "display_order": 1, "is_active": True},
        )

        earbuds, created = Product.objects.get_or_create(
            slug="fitpulse-wireless-earbuds-pro",
            defaults={
                "name": "FitPulse Wireless ANC Earbuds Pro",
                "sku": "FM-EP-001",
                "category": electronics,
                "short_description": "Active Noise Cancelling true wireless earbuds with 36hr battery.",
                "description": "Experience immersive audio with hybrid active noise cancellation, low latency gaming mode, and IPX5 water resistance.",
                "price": Decimal("2499.00"),
                "compare_at_price": Decimal("3999.00"),
                "stock_quantity": 0,
                "is_active": True,
                "is_featured": True,
            },
        )

        variants_earbuds = [
            {"type": "Color", "value": "Midnight Black", "sku": "FM-EP-BLK", "stock": 15, "price": Decimal("2499.00"), "compare": Decimal("3999.00")},
            {"type": "Color", "value": "Pure White", "sku": "FM-EP-WHT", "stock": 8, "price": Decimal("2499.00"), "compare": Decimal("3999.00")},
            {"type": "Color", "value": "Cobalt Blue", "sku": "FM-EP-BLU", "stock": 0, "price": Decimal("2699.00"), "compare": Decimal("4199.00")},  # Out of stock
        ]
        for v in variants_earbuds:
            ProductVariant.objects.get_or_create(
                product=earbuds,
                sku=v["sku"],
                defaults={
                    "option_type": v["type"],
                    "option_value": v["value"],
                    "stock_quantity": v["stock"],
                    "price": v["price"],
                    "compare_at_price": v["compare"],
                    "is_active": True,
                },
            )

        self.stdout.write(self.style.SUCCESS("Successfully seeded sample variant products!"))
