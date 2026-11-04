import secrets
from django.core.management.base import BaseCommand
from django.conf import settings
from core.sms_services import get_sms_provider, mask_phone_number, _get_credential
from core.otp_services import normalize_mobile_number


class Command(BaseCommand):
    help = (
        "Diagnoses SMS provider configuration and optionally dispatches an authorized "
        "test OTP via the active SMS gateway."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--mobile",
            type=str,
            help="10-digit Indian destination mobile number (e.g. 9876543210)",
        )
        parser.add_argument(
            "--otp",
            type=str,
            default="",
            help="Optional 6-digit test code (defaults to cryptographically random code)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect configuration and credentials without calling the live SMS gateway",
        )

    def handle(self, *args, **options):
        mobile = options.get("mobile")
        otp = options.get("otp")
        dry_run = options.get("dry_run")

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Fitmeezo SMS Gateway Diagnostics ==="))

        provider_setting = getattr(settings, "SMS_PROVIDER", "")
        self.stdout.write(f"SMS_PROVIDER setting: '{provider_setting}'")

        # Provider detection
        provider = get_sms_provider()
        provider_name = provider.__class__.__name__
        self.stdout.write(f"Active Provider class: {self.style.SUCCESS(provider_name)}")

        # Credential status summary
        msg91_key = _get_credential("MSG91_AUTH_KEY", "SMS_AUTH_KEY", "SMS_API_KEY")
        msg91_template = _get_credential("MSG91_TEMPLATE_ID", "SMS_TEMPLATE_ID")
        fast2sms_key = _get_credential("FAST2SMS_API_KEY")

        self.stdout.write("\nCredential Presence:")
        self.stdout.write(f"  - MSG91 Auth Key: {'Configured' if msg91_key else 'Missing'}")
        self.stdout.write(f"  - MSG91 Template ID: {'Configured' if msg91_template else 'Missing'}")
        self.stdout.write(f"  - Fast2SMS API Key: {'Configured' if fast2sms_key else 'Missing'}")

        if not mobile:
            self.stdout.write(
                self.style.WARNING(
                    "\nNo destination mobile provided. Pass '--mobile 9876543210' to send a test SMS."
                )
            )
            return

        clean_mobile = normalize_mobile_number(mobile)
        if not clean_mobile:
            self.stdout.write(
                self.style.ERROR(
                    f"\nInvalid Indian mobile number: '{mobile}'. Must be 10 digits starting with 6-9."
                )
            )
            return

        if not otp:
            otp = "".join(secrets.choice("0123456789") for _ in range(6))

        masked = mask_phone_number(clean_mobile)
        self.stdout.write(f"\nTarget mobile: +91 {masked}")
        self.stdout.write(f"Test OTP code: {otp}")

        if dry_run:
            self.stdout.write(
                self.style.NOTICE("\n[DRY RUN] Inspection complete. SMS gateway was NOT called.")
            )
            return

        self.stdout.write(f"\nDispatching test OTP to +91 {masked} via {provider_name}...")
        success, message = provider.send_otp(clean_mobile, otp)

        if success:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\n[SUCCESS] Test SMS successfully accepted by {provider_name}!\nGateway response: {message}"
                )
            )
        else:
            self.stdout.write(
                self.style.ERROR(
                    f"\n[FAILED] SMS delivery failed via {provider_name}.\nError details: {message}"
                )
            )
            self.stdout.write(
                self.style.NOTICE(
                    "\nTroubleshooting tips:\n"
                    "1. Verify MSG91_AUTH_KEY / FAST2SMS_API_KEY is active and funded.\n"
                    "2. For MSG91: Ensure DLT Entity ID and Template ID are registered with TRAI.\n"
                    "3. Ensure the destination number is valid and in coverage."
                )
            )
