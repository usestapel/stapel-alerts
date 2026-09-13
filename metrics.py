"""What a dashboard can ask this store without querying the database.

Two measurements, and the pair is deliberate:

``alerts_new_total{level,service}``   a COUNTER — how many issues were opened.
                                      What a spike alert fires on.
``alerts_open_total{level,service}``  a GAUGE — how many are open right now.
                                      What a wall board shows, and what says
                                      whether a fix wave actually landed.

Both go through ``stapel_core.observability.metrics``, so a deployment names
the system they land in and this library imports no vendor.

The gauge is declared for every (level, service) pair the store knows, INCLUDING
the ones at zero. A gauge that only exists once something is broken cannot be
alerted on — an expression over a series that has never existed does not fire.
That is the same failure the DLQ counter was given ``declare_topics`` for.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

NEW_METRIC = "alerts_new_total"
OPEN_METRIC = "alerts_open_total"

_NEW_DESCRIPTION = "Issues opened in this alert store"
_OPEN_DESCRIPTION = "Issues currently open (new or regressed)"


def observe_event(issue, *, created: bool) -> None:
    """Count a newly opened issue and refresh the open gauge. Never raises."""
    try:
        from stapel_core.observability import metrics

        if created:
            metrics.counter(
                NEW_METRIC,
                labels={"level": issue.level, "service": issue.service},
                description=_NEW_DESCRIPTION,
            )
    except Exception:  # pragma: no cover - the facade guards itself
        logger.debug("alerts: new-issue metric not recorded", exc_info=True)
    refresh_open_gauges()


def refresh_open_gauges() -> None:
    """Set ``alerts_open_total`` for every (level, service) the store knows.

    Every pair, not only the non-empty ones: a service whose issues were all
    fixed must report 0, or a dashboard cannot tell "fixed" from "the exporter
    stopped". Cheap — one grouped query over an indexed pair of columns.
    """
    try:
        from django.db.models import Count

        from stapel_core.observability import metrics

        from .models import OPEN_STATUSES, Issue

        known = set(
            Issue.objects.values_list("service", "level").distinct()
        )
        open_counts = {
            (row["service"], row["level"]): row["n"]
            for row in Issue.objects.filter(status__in=OPEN_STATUSES)
            .values("service", "level")
            .annotate(n=Count("id"))
        }
        for service, level in known | set(open_counts):
            metrics.gauge(
                OPEN_METRIC,
                open_counts.get((service, level), 0),
                labels={"level": level, "service": service},
                description=_OPEN_DESCRIPTION,
            )
    except Exception:  # pragma: no cover - never on the failure path's way
        logger.debug("alerts: open gauge not refreshed", exc_info=True)


__all__ = ["NEW_METRIC", "OPEN_METRIC", "observe_event", "refresh_open_gauges"]
