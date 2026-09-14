"""Ask the monitoring stack whether it can still see.

    manage.py alerts_watch_monitoring
    manage.py alerts_watch_monitoring --dry-run

One pass of the Prometheus blind-spot watchdog: ``up == 0`` on every
configured scrape target, ``absent()`` on every named metric, the
dead-man's-switch alert, and Alertmanager's active silences. Each blind spot
becomes a ``kind="monitoring"`` issue in the tracker and a message on the
Telegram fallback seam; a check that stops failing closes its issue with
``recovered <ts>``.

The same work the ``alerts-watch-monitoring`` beat entry does, so a
deployment without Celery schedules this from cron and loses nothing.

``--dry-run`` asks the questions and prints the answers without filing,
closing or announcing anything — the way to find out what the watchdog would
say before it says it to a phone at 3am.
"""
from django.core.management.base import BaseCommand

from stapel_alerts import monitoring


class Command(BaseCommand):
    help = (
        "Query Prometheus for monitoring blind spots and file each one as an "
        "issue (and a Telegram message). Recovered checks are closed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run every check and print the findings without filing, "
            "closing or notifying anything.",
        )

    def handle(self, *args, **options):
        if not monitoring.is_configured():
            self.stderr.write(
                self.style.ERROR(
                    'STAPEL_ALERTS["MONITORING"]["PROMETHEUS_URL"] is not set — '
                    "this watchdog checked nothing. An unconfigured watchdog and "
                    "a healthy fleet produce the same output, which is why this "
                    "is an error and not a quiet exit."
                )
            )
            return

        if options["dry_run"]:
            findings = monitoring.collect()
            for finding in findings:
                self.stdout.write(finding.line())
            self.stdout.write(
                self.style.SUCCESS(f"dry run: {len(findings)} blind spot(s), nothing filed")
            )
            return

        summary = monitoring.run()
        for finding in summary["findings"]:
            self.stdout.write(finding.line())
        for finding in summary["recovered"]:
            self.stdout.write(self.style.SUCCESS(f"recovered {finding.check}: {finding.target}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"{len(summary['findings'])} blind spot(s), "
                f"{summary['reported']} filed, "
                f"{len(summary['recovered'])} recovered, "
                f"fallback {'sent' if summary['announced'] else 'not sent'}"
            )
        )
