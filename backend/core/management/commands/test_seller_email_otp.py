import secrets
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from core.otp_services import normalize_email_address, mask_email_address


class Command(BaseCommand):
    help = (
        "Diagnoses Django SMTP email configuration and optionally dispatches a test "
        "Seller Hub login verification OTP email to a destination address."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            type=str,
            help="Destination registered seller email address (e.g. vendor@yourstore.in)",
        )
        parser.add_argument(
            "--otp",
            type=str,
            default="",
            help="Optional 6-digit test code (defaults to random cryptographic code)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect SMTP settings and environment credentials without connecting or sending",
        )

    def handle(self, *args, **options):
        email = options.get("email")
        otp = options.get("otp")
        dry_run = options.get("dry_run")

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Fitmeezo Seller Hub SMTP / Email Diagnostics ==="))

        backend = getattr(settings, "EMAIL_BACKEND", "")
        host = getattr(settings, "EMAIL_HOST", "")
        port = getattr(settings, "EMAIL_PORT", "")
        host_user = getattr(settings, "EMAIL_HOST_USER", "")
        host_pw = getattr(settings, "EMAIL_HOST_PASSWORD", "")
        use_tls = getattr(settings, "EMAIL_USE_TLS", False)
        use_ssl = getattr(settings, "EMAIL_USE_SSL", False)
        timeout = getattr(settings, "EMAIL_TIMEOUT", 15)
        default_from = getattr(settings, "DEFAULT_FROM_EMAIL", "")

        self.stdout.write(f"EMAIL_BACKEND:   {self.style.SUCCESS(backend)}")
        self.stdout.write(f"EMAIL_HOST:      {host}:{port}")
        self.stdout.write(f"EMAIL_USE_TLS:   {use_tls}")
        self.stdout.write(f"EMAIL_USE_SSL:   {use_ssl}")
        self.stdout.write(f"EMAIL_TIMEOUT:   {timeout}s")
        self.stdout.write(f"DEFAULT_FROM:    {default_from}")

        has_user = bool(host_user)
        has_pw = bool(host_pw)
        self.stdout.write("\nCredential Status:")
        self.stdout.write(f"  - EMAIL_HOST_USER:     {'Configured (' + host_user + ')' if has_user else self.style.WARNING('Missing / Blank')}")
        self.stdout.write(f"  - EMAIL_HOST_PASSWORD: {'Configured (length ' + str(len(host_pw)) + ')' if has_pw else self.style.WARNING('Missing / Blank')}")

        if not email:
            self.stdout.write(
                self.style.WARNING(
                    "\nNo destination email provided. Pass '--email vendor@yourstore.in' to test delivery."
                )
            )
            return

        clean_email = normalize_email_address(email)
        if not clean_email:
            self.stdout.write(
                self.style.ERROR(f"\nInvalid email format: '{email}'. Please provide a valid address.")
            )
            return

        if not otp:
            otp = "".join(secrets.choice("0123456789") for _ in range(6))

        masked = mask_email_address(clean_email)
        self.stdout.write(f"\nDestination Email: {masked}")
        self.stdout.write(f"Test OTP Code:     {otp}")

        if dry_run:
            self.stdout.write(
                self.style.NOTICE("\n[DRY RUN] Inspection complete. No SMTP connection was established.")
            )
            return

        self.stdout.write(f"\nDispatching test OTP email to {clean_email} via {backend}...")

        brand_name = getattr(settings, "BRAND_NAME", "Fitmeezo")
        subject = f"Your {brand_name} Seller Hub Verification Code: {otp}"
        body_text = (
            f"Hello Valued Merchant,\n\n"
            f"Your one-time Fitmeezo Seller Hub verification code is: {otp}\n\n"
            f"This code expires in 5 minutes.\n\n"
            f"Fitmeezo Seller Operations\nhttps://fitmeezo.com\n"
        )
        body_html = (
            f"<div style='font-family: sans-serif; padding: 20px;'>"
            f"<h2>Fitmeezo Seller Hub Verification</h2>"
            f"<p>Use this one-time code to sign in:</p>"
            f"<div style='font-size: 32px; font-weight: bold; letter-spacing: 6px; padding: 15px; background: #f1f5f9; border-radius: 6px;'>{otp}</div>"
            f"<p style='color: #64748b; font-size: 13px;'>Expires in 5 minutes.</p>"
            f"</div>"
        )

        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=body_text,
                from_email=default_from,
                to=[clean_email],
            )
            msg.attach_alternative(body_html, "text/html")
            sent_count = msg.send(fail_silently=False)

            if sent_count:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"\n[SUCCESS] Test OTP email successfully dispatched to {clean_email}!"
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"\n[WARNING] Email backend reported 0 sent messages."
                    )
                )
        except Exception as exc:
            self.stdout.write(
                self.style.ERROR(
                    f"\n[FAILED] SMTP delivery error: {type(exc).__name__}: {str(exc)}"
                )
            )
            self.stdout.write(
                self.style.NOTICE(
                    "\nTroubleshooting tips for Gmail SMTP:\n"
                    "1. Ensure EMAIL_HOST_USER is your full Gmail address (e.g. store@gmail.com).\n"
                    "2. Ensure EMAIL_HOST_PASSWORD is a Google 16-character 'App Password', NOT your personal Gmail account password.\n"
                    "   (Generate at: https://myaccount.google.com/apppasswords with 2-Step Verification enabled).\n"
                    "3. Ensure EMAIL_HOST=smtp.gmail.com, EMAIL_PORT=587, EMAIL_USE_TLS=True."
                )
            )
