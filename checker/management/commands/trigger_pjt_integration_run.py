import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from checker.services.pjt_integration import PJTIntegrationClient


class Command(BaseCommand):
    help = "Trigger the configured PJT integration run endpoint."

    def add_arguments(self, parser):
        parser.add_argument(
            "--payload-json",
            default="{}",
            help="Optional JSON payload to send to the trigger endpoint.",
        )
        parser.add_argument(
            "--path",
            default=settings.PJT_INTEGRATION_TRIGGER_PATH,
            help="Optional path override relative to PJT_INTEGRATION_BASE_URL.",
        )

    def handle(self, *args, **options):
        client = PJTIntegrationClient()
        if not client.is_configured():
            raise CommandError(
                "PJT integration is not configured. Set PJT_INTEGRATION_BASE_URL and "
                "PJT_INTEGRATION_BEARER_TOKEN before running this command."
            )

        try:
            payload = json.loads(options["payload_json"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON for --payload-json: {exc}") from exc

        response = client.request(
            method="POST",
            path=options["path"],
            json=payload,
        )
        body_preview = response.text[:500] if response.text else ""
        if response.ok:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Triggered PJT integration run successfully ({response.status_code})"
                )
            )
        else:
            raise CommandError(
                f"PJT integration trigger failed with status {response.status_code}: {body_preview}"
            )

        if body_preview:
            self.stdout.write(body_preview)
