"""
Fitmeezo Media Migration Utility: Local Storage to Cloudinary CDN.

Safely synchronizes existing local media files (product pictures, gallery photos,
category banners, and customer return proofs) from MEDIA_ROOT to Cloudinary
without modifying database strings or foreign keys.
"""

import os
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from core.models import Category, Product, ProductImage, ReturnRequest


class Command(BaseCommand):
    help = (
        "Safely syncs local media files from MEDIA_ROOT to Cloudinary persistent storage. "
        "Preserves existing database references and supports safe --dry-run simulation."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Simulate the migration without uploading any files to Cloudinary.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            default=False,
            help="Overwrite assets on Cloudinary if they already exist.",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        overwrite = options.get("overwrite", False)

        is_enabled = getattr(settings, "CLOUDINARY_ENABLED", False)

        self.stdout.write(self.style.MIGRATE_HEADING("=== Fitmeezo Cloudinary Media Migration Utility ==="))
        self.stdout.write(f"Mode: {'SIMULATION (--dry-run)' if dry_run else 'LIVE UPLOAD'}")
        self.stdout.write(f"Cloudinary Enabled in Settings: {is_enabled}")

        if not is_enabled and not dry_run:
            self.stderr.write(
                self.style.ERROR(
                    "Error: Cloudinary is not configured in environment variables. "
                    "Please set CLOUDINARY_URL or (CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET) "
                    "before running live migration, or use --dry-run to preview."
                )
            )
            return

        media_root = Path(settings.MEDIA_ROOT)
        media_tag = getattr(settings, "CLOUDINARY_MEDIA_TAG", "media")

        # Gather all distinct media paths from the database
        files_to_sync = set()

        for cat in Category.objects.exclude(image="").exclude(image__isnull=True):
            if cat.image and cat.image.name:
                files_to_sync.add((cat.image.name, "Category Banner", cat.name))

        for prod in Product.objects.exclude(image="").exclude(image__isnull=True):
            if prod.image and prod.image.name:
                files_to_sync.add((prod.image.name, "Product Primary Image", prod.name))

        for p_img in ProductImage.objects.exclude(image="").exclude(image__isnull=True):
            if p_img.image and p_img.image.name:
                files_to_sync.add((p_img.image.name, "Product Gallery Image", getattr(p_img.product, "name", "Gallery")))

        from core.models import ReturnRequest
        for ret in ReturnRequest.objects.all():
            for photo_field in (ret.photo_1, ret.photo_2, ret.photo_3):
                if photo_field and photo_field.name:
                    files_to_sync.add((photo_field.name, "Return Proof Photo", f"Return #{ret.request_number}"))

        self.stdout.write(f"Found {len(files_to_sync)} active media references in the database.\n")

        uploader = None
        if not dry_run and is_enabled:
            try:
                import cloudinary.uploader
                uploader = cloudinary.uploader
            except ImportError:
                self.stderr.write(self.style.ERROR("Error: cloudinary package is not installed."))
                return

        uploaded_count = 0
        skipped_count = 0
        missing_count = 0

        for rel_path, doc_type, label in sorted(files_to_sync, key=lambda x: x[0]):
            local_file = media_root / rel_path

            if not local_file.exists():
                self.stdout.write(self.style.WARNING(f" [MISSING ON DISK] {rel_path} ({doc_type}: {label})"))
                missing_count += 1
                continue

            public_id = f"{media_tag}/{rel_path}" if media_tag else rel_path

            if dry_run:
                self.stdout.write(
                    self.style.SUCCESS(f" [WOULD SYNC] {rel_path} -> Cloudinary public_id: {public_id} ({local_file.stat().st_size} bytes)")
                )
                uploaded_count += 1
            else:
                try:
                    self.stdout.write(f" Uploading {rel_path} -> {public_id} ...", ending="")
                    uploader.upload(
                        str(local_file),
                        public_id=public_id,
                        overwrite=overwrite,
                        resource_type="auto",
                    )
                    self.stdout.write(self.style.SUCCESS(" OK"))
                    uploaded_count += 1
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f" FAILED: {exc}"))
                    skipped_count += 1

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Migration Summary ==="))
        self.stdout.write(f"Total Database Media References: {len(files_to_sync)}")
        self.stdout.write(f"Successfully Synced / Planned:    {uploaded_count}")
        self.stdout.write(f"Skipped / Upload Errors:           {skipped_count}")
        self.stdout.write(f"Missing Local Files on Disk:       {missing_count}")
        self.stdout.write(self.style.SUCCESS("Done.\n"))
