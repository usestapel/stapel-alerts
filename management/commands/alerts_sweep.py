"""Retention sweep: drop events past their level's retention, and empty issues.

    manage.py alerts_sweep
    manage.py alerts_sweep --dry-run

Two passes, in this order:

1. **events past retention** (``RETENTION_DAYS`` per level) and events beyond
   ``EVENTS_PER_ISSUE`` per issue. The issue's counters are untouched: the
   events are the sample, the count is the fact, and an issue does not become
   less real because its oldest traces were swept.
2. **issues with nothing left** — no events, and closed (fixed or muted) past
   the longest retention. An open issue is NEVER swept, however old: an open
   issue with no events is still a bug nobody fixed, and deleting it would
   turn "unresolved" into "never happened".
"""
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone
from datetime import timedelta

from stapel_alerts.conf import alerts_settings
from stapel_alerts.models import OPEN_STATUSES, ErrorEvent, Issue


class Command(BaseCommand):
    help = "Delete alert events past their retention, and issues with nothing left."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        now = timezone.now()
        retention = dict(alerts_settings.RETENTION_DAYS or {})
        keep_per_issue = int(alerts_settings.EVENTS_PER_ISSUE)

        deleted_events = 0
        for level, days in retention.items():
            cutoff = now - timedelta(days=int(days))
            qs = ErrorEvent.objects.filter(level=level, received_at__lt=cutoff)
            n = qs.count()
            if n and not dry:
                qs.delete()
            deleted_events += n
            self.stdout.write(f"{level}: {n} event(s) older than {days}d")

        over_cap = 0
        busy = (
            ErrorEvent.objects.values("issue")
            .annotate(n=Count("id"))
            .filter(n__gt=keep_per_issue)
        )
        for row in busy:
            ids = list(
                ErrorEvent.objects.filter(issue_id=row["issue"])
                .order_by("-received_at")
                .values_list("id", flat=True)[keep_per_issue:]
            )
            over_cap += len(ids)
            if ids and not dry:
                ErrorEvent.objects.filter(id__in=ids).delete()
        self.stdout.write(f"over the per-issue cap ({keep_per_issue}): {over_cap} event(s)")

        longest = max([int(d) for d in retention.values()] or [90])
        empty = (
            Issue.objects.exclude(status__in=OPEN_STATUSES)
            .filter(last_seen__lt=now - timedelta(days=longest))
            .annotate(n=Count("events"))
            .filter(n=0)
        )
        n_issues = empty.count()
        if n_issues and not dry:
            Issue.objects.filter(id__in=list(empty.values_list("id", flat=True))).delete()
        self.stdout.write(f"closed issues with no events left: {n_issues}")

        from stapel_alerts.metrics import refresh_open_gauges

        if not dry:
            refresh_open_gauges()

        self.stdout.write(
            self.style.SUCCESS(
                f"{'would delete' if dry else 'deleted'} "
                f"{deleted_events + over_cap} event(s), {n_issues} issue(s)"
            )
        )
