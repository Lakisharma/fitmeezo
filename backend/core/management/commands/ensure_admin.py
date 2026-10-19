import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = (
        "Ensures an active administrator/superuser account exists with staff permissions. "
        "Can be run safely during build/deployment on Render and locally."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="Username for the admin user (defaults to ADMIN_USERNAME or 'admin')",
        )
        parser.add_argument(
            "--email",
            type=str,
            default=None,
            help="Email for the admin user (defaults to ADMIN_EMAIL or 'admin@fitmeezo.com')",
        )
        parser.add_argument(
            "--password",
            type=str,
            default=None,
            help="Password for the admin user (defaults to ADMIN_PASSWORD or 'FitmeezoAdmin@2026')",
        )

    def handle(self, *args, **options):
        User = get_user_model()

        username = (
            options.get("username")
            or os.environ.get("ADMIN_USERNAME")
            or os.environ.get("DJANGO_SUPERUSER_USERNAME")
            or "admin"
        ).strip()

        email = (
            options.get("email")
            or os.environ.get("ADMIN_EMAIL")
            or os.environ.get("DJANGO_SUPERUSER_EMAIL")
            or "admin@fitmeezo.com"
        ).strip()

        password = (
            options.get("password")
            or os.environ.get("ADMIN_PASSWORD")
            or os.environ.get("DJANGO_SUPERUSER_PASSWORD")
            or "FitmeezoAdmin@2026"
        )

        try:
            user = User.objects.filter(username=username).first()
            if user:
                user.email = email
                user.is_staff = True
                user.is_superuser = True
                user.is_active = True
                user.set_password(password)
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[ensure_admin] Successfully updated existing user '{username}' with superuser privileges and password."
                    )
                )
            else:
                user = User.objects.create_superuser(
                    username=username,
                    email=email,
                    password=password,
                )
                user.is_active = True
                user.is_staff = True
                user.is_superuser = True
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[ensure_admin] Successfully created new superuser '{username}' ({email})."
                    )
                )
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(f"[ensure_admin] Error ensuring admin user: {e}")
            )
            raise SystemExit(1)

        # Ensure Site 1 is configured for proper password reset links and emails
        try:
            from django.contrib.sites.models import Site
            site_domain = os.environ.get("SITE_DOMAIN", "fitmeezo.com").strip() or "fitmeezo.com"
            site = Site.objects.filter(id=1).first()
            if site:
                site.domain = site_domain
                site.name = "Fitmeezo"
                site.save(update_fields=["domain", "name"])
            else:
                Site.objects.create(id=1, domain=site_domain, name="Fitmeezo")
            self.stdout.write(
                self.style.SUCCESS(f"[ensure_admin] Successfully verified Site 1 domain: {site_domain}")
            )
        except Exception as e:
            self.stdout.write(f"[ensure_admin] Note: Site model configuration skipped: {e}")
