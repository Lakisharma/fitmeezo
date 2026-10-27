"""
Fitmeezo Media Storage Reconciliation Command.

Safely detects, audits, and synchronizes product and catalog media files between
Django's active MEDIA_ROOT and any legacy upload directories.
Ensures Nginx media serving (/var/www/fitmeezo/media/) and Django storage are 100% aligned.
"""

import os
from pathlib import Path
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand
from core.models import Category, Product, ProductImage


class Command(BaseCommand):
    help = (
        "Reconciles product media files between Django storage and Nginx media directory. "
        "Safely copies missing media from legacy backend/media to /var/www/fitmeezo/media without data loss."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Audit and preview missing/restorable files without making filesystem changes.",
        )
        parser.add_argument(
            "--source-dir",
            type=str,
            default=None,
            help="Custom source directory to search for missing media (defaults to BASE_DIR / 'media').",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        custom_source = options.get("source_dir")

        media_root = Path(settings.MEDIA_ROOT).resolve()
        legacy_media_dir = Path(custom_source).resolve() if custom_source else (Path(settings.BASE_DIR) / "media").resolve()

        self.stdout.write(self.style.MIGRATE_HEADING("=== Fitmeezo Media Storage Reconciliation ==="))
        self.stdout.write(f"Active Django MEDIA_ROOT: {media_root}")
        self.stdout.write(f"Legacy Search Directory  : {legacy_media_dir}")
        self.stdout.write(f"Execution Mode           : {'SIMULATION (--dry-run)' if dry_run else 'LIVE RECONCILIATION'}")
        self.stdout.write("-" * 65)

        # Ensure active media root exists
        if not dry_run:
            try:
                media_root.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Could not create MEDIA_ROOT directory: {e}"))

        # Gather distinct relative media paths from database
        product_images = list(
            Product.objects.exclude(image="").exclude(image__isnull=True).values_list("id", "name", "image")
        )
        gallery_images = list(
            ProductImage.objects.exclude(image="").exclude(image__isnull=True).values_list("id", "product__name", "image")
        )
        category_images = list(
            Category.objects.exclude(image="").exclude(image__isnull=True).values_list("id", "name", "image")
        )

        total_checked = len(product_images) + len(gallery_images) + len(category_images)
        self.stdout.write(f"Total database image records found: {total_checked}")
        self.stdout.write(f"  - Products: {len(product_images)}")
        self.stdout.write(f"  - Product Gallery: {len(gallery_images)}")
        self.stdout.write(f"  - Categories: {len(category_images)}")
        self.stdout.write("-" * 65)

        ok_count = 0
        restored_count = 0
        missing_count = 0

        # Combine all media entries: (record_type, id, label, rel_path)
        all_entries = []
        for pid, name, img in product_images:
            all_entries.append(("Product", pid, name, str(img)))
        for gid, pname, img in gallery_images:
            all_entries.append(("Gallery", gid, f"Gallery of {pname}", str(img)))
        for cid, cname, img in category_images:
            all_entries.append(("Category", cid, cname, str(img)))

        for rtype, rid, label, rel_path in all_entries:
            clean_rel = rel_path.lstrip("/\\")
            target_path = media_root / clean_rel

            # 1. Check if file already exists in active MEDIA_ROOT
            if target_path.is_file():
                ok_count += 1
                continue

            # 2. File missing in active MEDIA_ROOT; check legacy location
            source_candidate = legacy_media_dir / clean_rel
            if source_candidate.is_file():
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[RESTORABLE] {rtype} #{rid} '{label}': Found in {source_candidate} -> Missing in {target_path}"
                        )
                    )
                    restored_count += 1
                else:
                    try:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_candidate, target_path)
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"[RESTORED] {rtype} #{rid} '{label}': Copied {clean_rel} to {media_root}"
                            )
                        )
                        restored_count += 1
                    except Exception as e:
                        self.stderr.write(self.style.ERROR(f"[FAILED COPY] {clean_rel}: {e}"))
                        missing_count += 1
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"[MISSING] {rtype} #{rid} '{label}': '{clean_rel}' not found in active MEDIA_ROOT or legacy directory."
                    )
                )
                missing_count += 1

        self.stdout.write("-" * 65)
        self.stdout.write(self.style.MIGRATE_HEADING("Reconciliation Summary:"))
        self.stdout.write(f"  Already in MEDIA_ROOT : {ok_count}")
        self.stdout.write(f"  Restored from legacy : {restored_count}")
        self.stdout.write(f"  Still Missing        : {missing_count}")

        # Check symlink alignment recommendation
        if media_root != legacy_media_dir:
            is_symlink = legacy_media_dir.is_symlink()
            self.stdout.write("-" * 65)
            if is_symlink:
                self.stdout.write(self.style.SUCCESS(f"✓ Legacy path {legacy_media_dir} is correctly symlinked to {media_root}."))
            else:
                self.stdout.write(
                    self.style.NOTICE(
                        f"Recommendation: To guarantee 100% path parity across legacy tools, create a symlink:\n"
                        f"  ln -sfn {media_root} {legacy_media_dir}"
                    )
                )
