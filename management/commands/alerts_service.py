"""Create (or rotate) the API key a reporting service authenticates with.

    manage.py alerts_service svc-billing
    manage.py alerts_service svc-billing --rotate

The key is printed ONCE. Only its sha256 is stored, so there is no second
chance and no support path that ends in "read it out of the database" —
which is the property that makes a leaked database dump not a write
credential for the tracker.
"""
from django.core.management.base import BaseCommand, CommandError

from stapel_alerts.models import Service


class Command(BaseCommand):
    help = "Register a service that may report alerts, and print its key once."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Service name, as it appears in the tracker")
        parser.add_argument(
            "--rotate",
            action="store_true",
            help="Issue a new key for an existing service (the old one stops working)",
        )

    def handle(self, *args, **options):
        name = options["name"]
        existing = Service.objects.filter(name=name).first()

        if existing and not options["rotate"]:
            raise CommandError(
                f"Service {name!r} already exists. Its key cannot be read back "
                "(only the hash is stored) — use --rotate to issue a new one."
            )

        if existing:
            raw = existing.rotate_key()
            service = existing
        else:
            service, raw = Service.create_with_key(name)

        self.stdout.write(self.style.SUCCESS(f"service: {service.name}"))
        self.stdout.write(f"id:      {service.id}")
        self.stdout.write(f"key:     {raw}")
        self.stdout.write("")
        self.stdout.write(
            'Put it in the reporting service: STAPEL_ALERTS = {"OWNER_URL": ..., '
            '"SERVICE_KEY": "<the key above>"}. It is not shown again.'
        )
