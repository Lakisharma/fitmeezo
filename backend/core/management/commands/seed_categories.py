from django.core.management.base import BaseCommand
from core.models import Category

INITIAL_CATEGORIES = [
    {
        "name": "Electronics",
        "slug": "electronics",
        "description": "Smartphones, Audio, Computing, Wearables & Tech Accessories",
        "display_order": 1,
    },
    {
        "name": "Fashion",
        "slug": "fashion",
        "description": "Curated Men's & Women's Apparel, Footwear & Accessories",
        "display_order": 2,
    },
    {
        "name": "Home & Living",
        "slug": "home-living",
        "description": "Kitchen Essentials, Home Decor, Cookware & Premium Bedding",
        "display_order": 3,
    },
    {
        "name": "Beauty",
        "slug": "beauty",
        "description": "Skincare Serums, Haircare, Organic Fragrances & Personal Grooming",
        "display_order": 4,
    },
    {
        "name": "Sports",
        "slug": "sports",
        "description": "Fitness Equipment, Gym Wear, Outdoor Sports & Active Gear",
        "display_order": 5,
    },
    {
        "name": "Baby & Kids",
        "slug": "baby-kids",
        "description": "Interactive Toys, Baby Gear, Feeding Essentials & Nursery Care",
        "display_order": 6,
    },
]


class Command(BaseCommand):
    help = "Seeds initial 6 core categories for Fitmeezo (idempotent)."

    def handle(self, *args, **options):
        created_count = 0
        existing_count = 0

        for item in INITIAL_CATEGORIES:
            # Look up by slug or name to preserve existing records and avoid duplicates
            category = Category.objects.filter(slug=item["slug"]).first()
            if not category:
                # Also check if name exists under another slug
                category = Category.objects.filter(name=item["name"]).first()

            if category:
                existing_count += 1
                self.stdout.write(
                    self.style.NOTICE(
                        f"Category already exists (preserved intact): {category.name} [slug: {category.slug}]"
                    )
                )
            else:
                category = Category.objects.create(
                    name=item["name"],
                    slug=item["slug"],
                    description=item["description"],
                    display_order=item["display_order"],
                    is_active=True,
                )
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created category: {category.name} [slug: {category.slug}]"
                    )
                )

        total_active = Category.objects.filter(is_active=True).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeding completed: {created_count} newly created, {existing_count} preserved. "
                f"Total active categories: {total_active}."
            )
        )
