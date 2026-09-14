"""The schedule that makes the blind-spot watchdog actually run.

Shipped BY the library, as a splat a host merges, for the reason core's
taskstore sweep is: a watchdog documented in a README is a watchdog some
deployment will not have — and the failure mode of an unscheduled watchdog is
indistinguishable from a healthy fleet, which is the exact confusion it was
written to end.

Wire it in the settings module itself::

    from stapel_alerts.beat import get_alerts_beat_schedule

    CELERY_BEAT_SCHEDULE = {
        **get_alerts_beat_schedule(),
        ...
    }

Written *there*, not merged from an ``on_after_finalize`` signal, so
``manage.py check`` — which reads ``settings.CELERY_BEAT_SCHEDULE`` — sees
what beat will actually run.

This module imports **settings and nothing else**, so it is safe to import
from a settings module (executed before ``django.setup()``).

Celery is OPTIONAL. ``manage.py alerts_watch_monitoring`` is a plain
management command any scheduler — cron, a systemd timer, a k8s CronJob —
can run on the same cadence.
"""
from __future__ import annotations

#: The task name a beat schedule references (stable across refactors).
WATCH_TASK_NAME = "stapel_alerts.beat.alerts_watch_monitoring"

#: Key of the shipped entry, so a host overrides the cadence by writing the
#: same key after the splat.
WATCH_BEAT_KEY = "alerts-watch-monitoring"

#: Five minutes. The interval is the resolution of every blind spot this
#: finds: a scrape target that dies one second after a run is invisible until
#: the next one. Shorter costs a handful of instant queries; longer is the
#: window in which "nothing is firing" means nothing at all.
WATCH_INTERVAL_SECONDS = 300


def alerts_watch_monitoring() -> dict:
    """One watchdog pass. A plain callable, so a host with no Celery can call it."""
    from .monitoring import run

    return run()


def get_alerts_beat_schedule(*, seconds: int = WATCH_INTERVAL_SECONDS) -> dict:
    """Beat entry for the monitoring watchdog. Add to ``CELERY_BEAT_SCHEDULE``."""
    return {
        WATCH_BEAT_KEY: {
            "task": WATCH_TASK_NAME,
            "schedule": float(seconds),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    alerts_watch_monitoring = shared_task(name=WATCH_TASK_NAME)(alerts_watch_monitoring)


__all__ = [
    "WATCH_BEAT_KEY",
    "WATCH_INTERVAL_SECONDS",
    "WATCH_TASK_NAME",
    "alerts_watch_monitoring",
    "get_alerts_beat_schedule",
]
