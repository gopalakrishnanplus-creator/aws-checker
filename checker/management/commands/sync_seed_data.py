from django.core.management.base import BaseCommand

from checker.importers import sync_seed_data


class Command(BaseCommand):
    help = "Upsert the bundled AWS resource inventory and check definitions."

    def handle(self, *args, **options):
        results = sync_seed_data()
        self.stdout.write(
            self.style.SUCCESS(
                f"Synced {results['check_definitions']} check definitions and {results['resources']} resources."
            )
        )
