"""Metrics, boot checks, the retention sweep, Sentry export, the key command."""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_alerts.checks import (
    E_NO_SERVICE,
    E_REPORTER_NO_KEY,
    W_NO_FALLBACK,
    W_OWNER_URL_IN_OWNER_MODE,
    check_alerts_configuration,
)
from stapel_alerts.metrics import NEW_METRIC, OPEN_METRIC, refresh_open_gauges
from stapel_alerts.models import ErrorEvent, Issue, IssueStatus, Service
from stapel_alerts.services import mark_fixed, record

from .traces import FK_VIOLATION_A, JWT_REFUSAL_A

pytestmark = pytest.mark.django_db


@pytest.fixture
def recorded_metrics(monkeypatch):
    """Collect what the observability facade was asked to record."""
    from stapel_core.observability import metrics as core_metrics

    seen = []
    monkeypatch.setattr(
        core_metrics, "counter",
        lambda name, value=1, labels=None, description=None: seen.append(
            ("counter", name, value, labels or {})
        ),
    )
    monkeypatch.setattr(
        core_metrics, "gauge",
        lambda name, value, labels=None, description=None,
        multiprocess_mode=None: seen.append(
            ("gauge", name, value, labels or {}, multiprocess_mode)
        ),
    )
    return seen


# ── Metrics ─────────────────────────────────────────────────────────────


def test_a_new_issue_is_counted(recorded_metrics, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a", level="error")

    counters = [m for m in recorded_metrics if m[0] == "counter" and m[1] == NEW_METRIC]
    assert counters
    assert counters[0][3] == {"level": "error", "service": "svc-a"}


def test_a_fixed_service_reports_zero_and_not_nothing(recorded_metrics):
    """A gauge that only exists once something is broken cannot be alerted on:
    an expression over a series that has never existed does not fire."""
    record(trace=FK_VIOLATION_A, service="svc-a", level="error")
    issue = Issue.objects.get()
    recorded_metrics.clear()

    mark_fixed(issue, version="1.0.0")

    gauges = {
        (m[3]["service"], m[3]["level"]): m[2]
        for m in recorded_metrics if m[0] == "gauge" and m[1] == OPEN_METRIC
    }
    assert gauges[("svc-a", "error")] == 0


def test_the_open_gauge_declares_how_workers_combine(recorded_metrics):
    """Under gunicorn with N workers, each one counts the SAME open issues.

    Without a declared mode prometheus_client emits one series per pid, and
    the obvious alternative (`livesum`) would report the store's open issues
    multiplied by the number of workers that refreshed. The freshest reading
    is the only correct answer.
    """
    record(trace=FK_VIOLATION_A, service="svc-a", level="error")
    recorded_metrics.clear()

    refresh_open_gauges()

    modes = {m[4] for m in recorded_metrics if m[1] == OPEN_METRIC}
    assert modes == {"livemostrecent"}


def test_the_open_gauge_counts_new_and_regressed(recorded_metrics):
    record(trace=FK_VIOLATION_A, service="svc-a", level="error")
    record(trace=JWT_REFUSAL_A, service="svc-b", level="fatal")
    recorded_metrics.clear()

    refresh_open_gauges()

    gauges = {
        (m[3]["service"], m[3]["level"]): m[2]
        for m in recorded_metrics if m[1] == OPEN_METRIC
    }
    assert gauges[("svc-a", "error")] == 1
    assert gauges[("svc-b", "fatal")] == 1


def test_metrics_never_break_a_capture(monkeypatch, django_capture_on_commit_callbacks):
    from stapel_core.observability import metrics as core_metrics

    def _explode(*a, **kw):
        raise RuntimeError("the metrics backend is down")

    monkeypatch.setattr(core_metrics, "counter", _explode)
    monkeypatch.setattr(core_metrics, "gauge", _explode)

    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a")

    assert Issue.objects.count() == 1


# ── Boot checks ─────────────────────────────────────────────────────────


def test_an_unnamed_service_is_an_error(settings):
    settings.STAPEL_ALERTS = {"SERVICE": ""}
    ids = [i.id for i in check_alerts_configuration(None)]
    assert E_NO_SERVICE in ids


def test_a_reporter_without_a_key_is_an_error(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "s", "OWNER_URL": "https://owner", "SERVICE_KEY": ""}
    ids = [i.id for i in check_alerts_configuration(None)]
    assert E_REPORTER_NO_KEY in ids


def test_a_reporter_without_a_fallback_is_warned(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "s", "OWNER_URL": "https://owner", "SERVICE_KEY": "k"}
    ids = [i.id for i in check_alerts_configuration(None)]
    assert W_NO_FALLBACK in ids


def test_a_properly_configured_reporter_is_clean(settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "s", "OWNER_URL": "https://owner", "SERVICE_KEY": "k",
        "FALLBACK": {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"},
    }
    assert check_alerts_configuration(None) == []


def test_an_owner_with_an_owner_url_is_warned(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "s", "MODE": "owner", "OWNER_URL": "https://owner"}
    ids = [i.id for i in check_alerts_configuration(None)]
    assert W_OWNER_URL_IN_OWNER_MODE in ids


def test_a_monolith_owner_is_clean(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "monolith"}
    assert check_alerts_configuration(None) == []


# ── The retention sweep ─────────────────────────────────────────────────


def test_the_sweep_drops_events_past_retention_and_keeps_the_count(settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "s", "RATE_LIMIT": 1000, "EVENTS_PER_ISSUE": 100,
        "RETENTION_DAYS": {"error": 30},
    }
    record(trace=FK_VIOLATION_A, service="svc-a", level="error")
    ErrorEvent.objects.update(received_at=timezone.now() - timedelta(days=40))

    call_command("alerts_sweep", stdout=StringIO())

    assert ErrorEvent.objects.count() == 0
    # The issue is still a real, open bug that happened once.
    assert Issue.objects.get().count == 1


def test_a_dry_run_deletes_nothing(settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "s", "RATE_LIMIT": 1000, "RETENTION_DAYS": {"error": 30},
    }
    record(trace=FK_VIOLATION_A, service="svc-a", level="error")
    ErrorEvent.objects.update(received_at=timezone.now() - timedelta(days=40))

    call_command("alerts_sweep", "--dry-run", stdout=StringIO())

    assert ErrorEvent.objects.count() == 1


def test_an_open_issue_is_never_swept_however_old(settings):
    """An open issue with no events is still a bug nobody fixed. Deleting it
    would turn "unresolved" into "never happened"."""
    settings.STAPEL_ALERTS = {
        "SERVICE": "s", "RATE_LIMIT": 1000, "RETENTION_DAYS": {"error": 1},
    }
    record(trace=FK_VIOLATION_A, service="svc-a", level="error")
    ErrorEvent.objects.all().delete()
    Issue.objects.update(last_seen=timezone.now() - timedelta(days=999))

    call_command("alerts_sweep", stdout=StringIO())

    assert Issue.objects.count() == 1


def test_a_closed_issue_with_nothing_left_is_swept(settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "s", "RATE_LIMIT": 1000, "RETENTION_DAYS": {"error": 1},
    }
    record(trace=FK_VIOLATION_A, service="svc-a", level="error")
    ErrorEvent.objects.all().delete()
    Issue.objects.update(
        status=IssueStatus.FIXED, last_seen=timezone.now() - timedelta(days=999)
    )

    call_command("alerts_sweep", stdout=StringIO())

    assert Issue.objects.count() == 0


# ── Sentry export ───────────────────────────────────────────────────────


def test_without_a_dsn_nothing_is_forwarded(settings, monkeypatch):
    from stapel_alerts import sentry

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    settings.STAPEL_ALERTS = {"SERVICE": "s", "RATE_LIMIT": 1000, "SENTRY_DSN": ""}
    record(trace=FK_VIOLATION_A, service="svc-a")

    assert sentry.dsn() == ""
    assert ErrorEvent.objects.get().sentry_event_id == ""


def test_with_a_dsn_the_event_is_stored_AND_forwarded(settings, monkeypatch):
    """The premise of the library: the same interface either way. Sentry adds
    a forward, it never replaces the local row."""
    from stapel_alerts import sentry

    settings.STAPEL_ALERTS = {
        "SERVICE": "s", "RATE_LIMIT": 1000, "SENTRY_DSN": "https://k@sentry.example/1",
    }
    monkeypatch.setattr(sentry, "forward", lambda event: "sentry-abc-123")

    record(trace=FK_VIOLATION_A, service="svc-a")

    event = ErrorEvent.objects.get()
    assert event.trace, "the local row is complete"
    assert event.sentry_event_id == "sentry-abc-123"
    assert Issue.objects.get().sentry_event_id == "sentry-abc-123"


def test_a_missing_sdk_is_not_an_error(settings, monkeypatch):
    settings.STAPEL_ALERTS = {
        "SERVICE": "s", "RATE_LIMIT": 1000, "SENTRY_DSN": "https://k@sentry.example/1",
    }
    import builtins

    real_import = builtins.__import__

    def _no_sentry(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("no sentry_sdk here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_sentry)

    record(trace=FK_VIOLATION_A, service="svc-a")

    assert ErrorEvent.objects.get().sentry_event_id == ""


def test_the_dsn_falls_back_to_the_environment(settings, monkeypatch):
    from stapel_alerts import sentry

    settings.STAPEL_ALERTS = {"SERVICE": "s", "SENTRY_DSN": ""}
    monkeypatch.setenv("SENTRY_DSN", "https://k@sentry.example/2")

    assert sentry.dsn() == "https://k@sentry.example/2"


# ── The service-key command ─────────────────────────────────────────────


def test_the_command_prints_the_key_once_and_stores_only_its_hash():
    out = StringIO()
    call_command("alerts_service", "svc-billing", stdout=out)

    printed = out.getvalue()
    key = [ln.split("key:")[1].strip() for ln in printed.splitlines() if ln.startswith("key:")][0]
    service = Service.objects.get(name="svc-billing")

    assert Service.authenticate(key) == service
    assert key not in service.key_hash


def test_creating_the_same_service_twice_is_refused():
    from django.core.management.base import CommandError

    call_command("alerts_service", "svc-billing", stdout=StringIO())
    with pytest.raises(CommandError):
        call_command("alerts_service", "svc-billing", stdout=StringIO())


def test_rotate_issues_a_new_key_and_kills_the_old_one():
    out = StringIO()
    call_command("alerts_service", "svc-billing", stdout=out)
    old = [ln.split("key:")[1].strip() for ln in out.getvalue().splitlines() if ln.startswith("key:")][0]

    out2 = StringIO()
    call_command("alerts_service", "svc-billing", "--rotate", stdout=out2)
    new = [ln.split("key:")[1].strip() for ln in out2.getvalue().splitlines() if ln.startswith("key:")][0]

    assert new != old
    assert Service.authenticate(old) is None
    assert Service.authenticate(new) is not None
