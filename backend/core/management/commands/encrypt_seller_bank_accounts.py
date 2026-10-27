import logging
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from core.encryption import (
    encrypt_bank_account_number,
    decrypt_bank_account_number,
    get_bank_fernet,
    BankEncryptionError,
    BankEncryptionConfigurationError,
)
from core.models import Seller

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Safely encrypts legacy plaintext seller bank account numbers at rest using authenticated Fernet encryption. "
        "Supports --dry-run for pre-validation and --rollback for emergency reversion."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate the migration without modifying any database records.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of records to process in each atomic transaction batch (default: 100).",
        )
        parser.add_argument(
            "--rollback",
            action="store_true",
            help="Reverse mode: decrypt encrypted records back to plaintext (use ONLY during emergency rollback).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = max(1, options["batch_size"])
        rollback = options["rollback"]

        self.stdout.write(self.style.MIGRATE_HEADING("=================================================="))
        mode_str = "EMERGENCY ROLLBACK (DECRYPT)" if rollback else "FORWARD ENCRYPTION"
        if dry_run:
            mode_str += " [DRY-RUN - NO CHANGES WILL BE WRITTEN]"
        self.stdout.write(self.style.MIGRATE_HEADING(f"Fitmeezo Seller Bank Account Migration: {mode_str}"))
        self.stdout.write(self.style.MIGRATE_HEADING("=================================================="))

        # 1. Verify encryption key configuration
        try:
            get_bank_fernet()
        except BankEncryptionConfigurationError as e:
            raise CommandError(
                f"Configuration Error: {e}\n"
                "Ensure FITMEEZO_BANK_ENCRYPTION_KEY environment variable is properly configured."
            )

        # 2. Query raw database state directly using SQL cursor to avoid ORM abstraction masking
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, bank_account_number, bank_verification_status FROM core_seller ORDER BY id")
            raw_rows = cursor.fetchall()

        total_sellers = len(raw_rows)
        self.stdout.write(f"Total sellers in database: {total_sellers}")

        to_process = []
        already_encrypted = 0
        already_plaintext = 0
        empty_count = 0

        for seller_id, raw_acc, v_status in raw_rows:
            raw_val = (raw_acc or "").strip()
            if not raw_val:
                empty_count += 1
                continue

            is_enc = raw_val.startswith("gAAAAA")
            if rollback:
                if is_enc:
                    to_process.append((seller_id, raw_val, v_status))
                else:
                    already_plaintext += 1
            else:
                if is_enc:
                    already_encrypted += 1
                else:
                    to_process.append((seller_id, raw_val, v_status))

        self.stdout.write(f"  - Empty / Unprovided bank accounts: {empty_count}")
        if rollback:
            self.stdout.write(f"  - Already plaintext (skipped): {already_plaintext}")
            self.stdout.write(f"  - Encrypted records to decrypt: {len(to_process)}")
        else:
            self.stdout.write(f"  - Already encrypted (skipped): {already_encrypted}")
            self.stdout.write(f"  - Plaintext records to encrypt: {len(to_process)}")

        if not to_process:
            self.stdout.write(self.style.SUCCESS("No records require processing. Database is already in desired state."))
            return

        # 3. Process records in batches
        processed_count = 0
        success_count = 0
        failed_count = 0
        failures = []

        for i in range(0, len(to_process), batch_size):
            batch = to_process[i : i + batch_size]
            batch_updates = []

            for seller_id, raw_val, v_status in batch:
                processed_count += 1
                try:
                    if rollback:
                        # Decrypt ciphertext to plaintext
                        decrypted = decrypt_bank_account_number(raw_val)
                        if not decrypted:
                            raise BankEncryptionError("Decryption resulted in empty value.")
                        batch_updates.append((seller_id, decrypted))
                    else:
                        # Encrypt plaintext to ciphertext
                        encrypted = encrypt_bank_account_number(raw_val)
                        # Verify round-trip before storing
                        round_trip = decrypt_bank_account_number(encrypted)
                        if round_trip != raw_val:
                            raise BankEncryptionError("Round-trip validation failed: decrypted mismatch.")
                        batch_updates.append((seller_id, encrypted))
                    success_count += 1
                except Exception as err:
                    failed_count += 1
                    failures.append((seller_id, str(err)))
                    self.stdout.write(self.style.ERROR(f"Error on Seller ID {seller_id}: {err}"))

            if not dry_run and batch_updates:
                try:
                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            for sid, new_val in batch_updates:
                                cursor.execute(
                                    "UPDATE core_seller SET bank_account_number = %s WHERE id = %s",
                                    [new_val, sid],
                                )
                except Exception as db_err:
                    raise CommandError(f"Database batch update failed: {db_err}")

        # 4. Final summary
        self.stdout.write("\n" + self.style.MIGRATE_HEADING("---------------- Migration Summary ----------------"))
        self.stdout.write(f"Total evaluated: {total_sellers}")
        self.stdout.write(f"Targeted for action: {len(to_process)}")
        self.stdout.write(self.style.SUCCESS(f"Successfully processed: {success_count}"))
        if failed_count > 0:
            self.stdout.write(self.style.ERROR(f"Failed records: {failed_count}"))
            for sid, err_msg in failures:
                self.stdout.write(self.style.ERROR(f"  - Seller {sid}: {err_msg}"))
        else:
            self.stdout.write(self.style.SUCCESS("Failed records: 0"))

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN COMPLETE] No records were modified in the database."))
        else:
            self.stdout.write(self.style.SUCCESS(f"\n[MIGRATION COMPLETE] Successfully applied {mode_str}."))
