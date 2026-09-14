"""The blind-spot watchdog, against a Prometheus that is actually served.

A real ``ThreadingHTTPServer`` on loopback rather than a monkeypatched
``urlopen``, because half of what can be wrong here is the request: the query
string encoding, the ``/api/v1/query`` path, the base URL's trailing slash,
the Alertmanager v2 route. A patched ``urlopen`` asserts on the arguments the
test itself constructed and would stay green through every one of those.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from stapel_alerts import monitoring
from stapel_alerts.models import Issue, IssueStatus
from stapel_alerts.monitoring import (
    CHECK_ALERTMANAGER_SILENCED,
    CHECK_ALERTMANAGER_UNREACHABLE,
    CHECK_EXPORTER_DOWN,
    CHECK_HEARTBEAT_MISSING,
    CHECK_METRIC_ABSENT,
    CHECK_PROMETHEUS_UNREACHABLE,
    CHECK_SCRAPE_MISSING,
)


# ── A Prometheus that answers what a test tells it to ───────────────────


class FakePrometheus:
    """An HTTP server with a scriptable ``up``/``absent``/``ALERTS`` world.

    ``vectors`` maps a PromQL expression to the result vector to answer with;
    anything unscripted answers an empty vector, which is what a real
    Prometheus does for a query that matches nothing.
    """

    def __init__(self):
        self.vectors: dict[str, list] = {}
        self.silences: list = []
        self.queries: list[str] = []
        self.paths: list[str] = []
        self.status = 200
        self.body_status = "success"
        self.silences_status = 200
        self._server = None
        self._thread = None

    def start(self) -> str:
        world = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the suite's output clean
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                world.paths.append(parsed.path)
                if parsed.path == "/api/v1/query":
                    expr = parse_qs(parsed.query).get("query", [""])[0]
                    world.queries.append(expr)
                    if world.status != 200:
                        self._send(world.status, {"status": "error"})
                        return
                    self._send(200, {
                        "status": world.body_status,
                        "data": {
                            "resultType": "vector",
                            "result": world.vectors.get(expr, []),
                        },
                    })
                    return
                if parsed.path == "/api/v2/silences":
                    if world.silences_status != 200:
                        self._send(world.silences_status, [])
                        return
                    self._send(200, world.silences)
                    return
                self._send(404, {"status": "error"})

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # Convenience constructors for the sample shapes Prometheus returns.
    @staticmethod
    def sample(value, **labels):
        return {"metric": labels, "value": [1757800000.0, str(value)]}


@pytest.fixture
def prometheus():
    world = FakePrometheus()
    world.url = world.start()
    try:
        yield world
    finally:
        world.stop()


@pytest.fixture
def watch(settings, prometheus):
    """Point the watchdog at the fake and return a configure() helper."""

    def _configure(**overrides):
        block = {
            "PROMETHEUS_URL": prometheus.url,
            "TARGETS": [],
            "METRICS": [],
            "HEARTBEAT_ALERT": "",
            "ALERTMANAGER_URL": "",
            "TIMEOUT_SECONDS": 3.0,
        }
        block.update(overrides)
        settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "RATE_LIMIT": 1000, "MONITORING": block}
        return block

    return _configure


# ── The client itself ───────────────────────────────────────────────────


def test_the_query_reaches_the_canonical_endpoint(prometheus, watch):
    watch()
    monitoring.instant_query('up{job="svc-a"}')

    assert prometheus.paths == ["/api/v1/query"]
    assert prometheus.queries == ['up{job="svc-a"}']


def test_a_trailing_slash_on_the_base_url_does_not_double(prometheus, watch):
    watch(PROMETHEUS_URL=prometheus.url + "/")
    monitoring.instant_query("up")

    assert prometheus.paths == ["/api/v1/query"]


def test_a_non_success_body_is_blindness_not_an_empty_result(prometheus, watch):
    """The red case that matters most.

    If a failed query read as "no series", every check below would answer
    "nothing is wrong" at exactly the moment nothing could be seen — which is
    the class of green this whole module exists to end.
    """
    watch()
    prometheus.status = 503

    with pytest.raises(monitoring.PrometheusUnreachable):
        monitoring.instant_query("up")


def test_a_200_that_says_error_is_also_blindness(prometheus, watch):
    """The subtler half: a proxy (or a partially-degraded Prometheus) answers
    HTTP 200 with ``{"status": "error"}`` and no result. Reading that as an
    empty vector makes every check below report health."""
    watch(TARGETS=["svc-a"])
    prometheus.body_status = "error"

    with pytest.raises(monitoring.PrometheusUnreachable):
        monitoring.instant_query("up")

    findings = monitoring.collect()
    assert [f.check for f in findings] == [CHECK_PROMETHEUS_UNREACHABLE]


def test_a_dead_prometheus_is_a_finding_not_a_crash(watch, settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-test",
        "MONITORING": {
            "PROMETHEUS_URL": "http://127.0.0.1:1",
            "TARGETS": ["svc-a"],
            "TIMEOUT_SECONDS": 1.0,
        },
    }
    findings = monitoring.collect()

    assert [f.check for f in findings] == [CHECK_PROMETHEUS_UNREACHABLE]
    # One line, not one per target: five copies of one outage bury the line
    # that says what happened.
    assert findings[0].level == "fatal"


# ── up == 0 ─────────────────────────────────────────────────────────────


def test_a_target_reporting_up_zero_is_a_finding(prometheus, watch):
    watch(TARGETS=["svc-a"])
    prometheus.vectors['up{job="svc-a"}'] = [
        FakePrometheus.sample(0, job="svc-a", instance="10.0.0.4:8000")
    ]
    findings = monitoring.collect()

    assert [(f.check, f.target) for f in findings] == [(CHECK_EXPORTER_DOWN, "svc-a")]
    assert findings[0].detail["instance"] == "10.0.0.4:8000"


def test_a_target_reporting_up_one_is_not_a_finding(prometheus, watch):
    watch(TARGETS=["svc-a"])
    prometheus.vectors['up{job="svc-a"}'] = [FakePrometheus.sample(1, job="svc-a")]

    assert monitoring.collect() == []


def test_a_target_with_no_up_series_at_all_is_the_worse_finding(prometheus, watch):
    """A target Prometheus has never heard of returns nothing, and "nothing"
    is what a healthy query also returns. Without this check, deleting a job
    from the scrape config silences every alert about it, permanently."""
    watch(TARGETS=["svc-gone"])

    findings = monitoring.collect()
    assert [(f.check, f.target) for f in findings] == [(CHECK_SCRAPE_MISSING, "svc-gone")]
    assert findings[0].level == "fatal"


def test_a_label_matcher_target_is_used_verbatim(prometheus, watch):
    watch(TARGETS=['instance="10.0.0.4:9100"'])
    monitoring.collect()

    assert prometheus.queries == ['up{instance="10.0.0.4:9100"}']


# ── absent() ────────────────────────────────────────────────────────────


def test_an_absent_metric_is_a_finding(prometheus, watch):
    watch(METRICS=["alerts_open_total"])
    prometheus.vectors["absent(alerts_open_total)"] = [FakePrometheus.sample(1)]

    findings = monitoring.collect()
    assert [(f.check, f.target) for f in findings] == [
        (CHECK_METRIC_ABSENT, "alerts_open_total")
    ]


def test_a_present_metric_is_silent(prometheus, watch):
    watch(METRICS=["alerts_open_total"])
    # absent() over a metric that HAS series returns an empty vector.
    assert monitoring.collect() == []
    assert prometheus.queries == ["absent(alerts_open_total)"]


# ── the dead-man's switch ───────────────────────────────────────────────


def test_a_heartbeat_that_stopped_firing_is_a_finding(prometheus, watch):
    watch(HEARTBEAT_ALERT="Watchdog")

    findings = monitoring.collect()
    assert [(f.check, f.target) for f in findings] == [
        (CHECK_HEARTBEAT_MISSING, "Watchdog")
    ]
    assert prometheus.queries == ['ALERTS{alertname="Watchdog",alertstate="firing"}']


def test_a_firing_heartbeat_is_silent(prometheus, watch):
    watch(HEARTBEAT_ALERT="Watchdog")
    prometheus.vectors['ALERTS{alertname="Watchdog",alertstate="firing"}'] = [
        FakePrometheus.sample(1, alertname="Watchdog")
    ]

    assert monitoring.collect() == []


def test_no_heartbeat_configured_asks_nothing(prometheus, watch):
    watch()
    assert monitoring.collect() == []
    assert prometheus.queries == []


# ── Alertmanager ────────────────────────────────────────────────────────


def test_an_active_silence_is_a_finding_keyed_by_its_matchers(prometheus, watch):
    watch(ALERTMANAGER_URL=prometheus.url)
    prometheus.silences = [
        {
            "id": "b2c1-fresh-uuid",
            "status": {"state": "active"},
            "comment": "muting the pager",
            "endsAt": "2026-09-20T00:00:00Z",
            "matchers": [{"name": "alertname", "value": "DiskFull", "isRegex": False}],
        }
    ]
    findings = monitoring.collect()

    assert [(f.check, f.target) for f in findings] == [
        (CHECK_ALERTMANAGER_SILENCED, 'alertname="DiskFull"')
    ]
    assert findings[0].detail["silence_id"] == "b2c1-fresh-uuid"


def test_a_re_silenced_alert_is_the_same_issue(prometheus, watch):
    """The silence id is a fresh uuid every time somebody re-silences the same
    alert. Keying on it would open a new issue each time, and a count that
    never rises is the one number that would have said "this is a habit"."""
    watch(ALERTMANAGER_URL=prometheus.url)
    matchers = [{"name": "alertname", "value": "DiskFull", "isRegex": False}]
    prometheus.silences = [
        {"id": "first", "status": {"state": "active"}, "matchers": matchers},
    ]
    first = monitoring.collect()[0]
    prometheus.silences = [
        {"id": "second-different-uuid", "status": {"state": "active"}, "matchers": matchers},
    ]
    second = monitoring.collect()[0]

    assert first.target == second.target
    assert monitoring.fingerprint_of(first) == monitoring.fingerprint_of(second)


def test_an_expired_silence_is_not_a_finding(prometheus, watch):
    watch(ALERTMANAGER_URL=prometheus.url)
    prometheus.silences = [
        {"id": "x", "status": {"state": "expired"}, "matchers": []},
    ]
    assert monitoring.collect() == []


def test_an_unreachable_alertmanager_is_a_finding(watch, settings, prometheus):
    watch(ALERTMANAGER_URL="http://127.0.0.1:1")
    findings = monitoring.collect()

    assert [f.check for f in findings] == [CHECK_ALERTMANAGER_UNREACHABLE]


def test_alertmanager_is_not_checked_when_not_configured(prometheus, watch):
    watch()
    monitoring.collect()
    assert "/api/v2/silences" not in prometheus.paths


# ── Filing, the fallback channel, and recovery ──────────────────────────


@pytest.mark.django_db
def test_a_finding_becomes_a_monitoring_issue(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    watch(TARGETS=["svc-gone"])
    with django_capture_on_commit_callbacks(execute=True):
        summary = monitoring.run()

    assert summary["reported"] == 1
    issue = Issue.objects.get()
    assert issue.kind == "monitoring"
    assert issue.level == "fatal"
    assert issue.status == IssueStatus.NEW
    event = issue.events.get()
    assert event.context["check"] == CHECK_SCRAPE_MISSING
    assert event.context["target"] == "svc-gone"


@pytest.mark.django_db
def test_the_same_blind_spot_twice_is_one_issue_with_a_rising_count(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    """A scrape gap that recurs every night must be one issue with a count of
    ninety, not ninety issues. That is why the message is fixed per (check,
    target) and the run's specifics live in the context — the normaliser would
    absorb a varying NUMBER, but nothing absorbs a varying word."""
    watch(TARGETS=["svc-gone"])
    for _ in range(3):
        with django_capture_on_commit_callbacks(execute=True):
            monitoring.run()

    assert Issue.objects.count() == 1
    assert Issue.objects.get().count == 3


@pytest.mark.django_db
def test_a_blind_spot_goes_to_telegram_as_well_as_the_tracker(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    """The owner's rule: when monitoring is blind, both channels.

    The notification path speaks on a new issue, a regression or a spike. A
    blind spot on its ninth consecutive run is none of those, and is not less
    urgent than on its first.
    """
    watch(TARGETS=["svc-gone"])
    with django_capture_on_commit_callbacks(execute=True):
        monitoring.run()
    sent_fallback.clear()

    with django_capture_on_commit_callbacks(execute=True):
        summary = monitoring.run()

    assert summary["announced"] is True
    assert sent_fallback, "the second run said nothing — the fallback is on the thresholds"
    subject, body = sent_fallback[-1]
    assert "monitoring blind" in subject
    assert CHECK_SCRAPE_MISSING in body


@pytest.mark.django_db
def test_a_healthy_run_says_nothing_to_anybody(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    watch(TARGETS=["svc-a"])
    prometheus.vectors['up{job="svc-a"}'] = [FakePrometheus.sample(1, job="svc-a")]
    with django_capture_on_commit_callbacks(execute=True):
        summary = monitoring.run()

    assert summary["findings"] == []
    assert summary["announced"] is False
    assert sent_fallback == []
    assert Issue.objects.count() == 0


@pytest.mark.django_db
def test_recovery_closes_the_issue_with_recovered_and_a_timestamp(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    watch(TARGETS=["svc-a"])
    prometheus.vectors['up{job="svc-a"}'] = [FakePrometheus.sample(0, job="svc-a")]
    with django_capture_on_commit_callbacks(execute=True):
        monitoring.run()
    issue = Issue.objects.get()
    assert issue.status == IssueStatus.NEW

    prometheus.vectors['up{job="svc-a"}'] = [FakePrometheus.sample(1, job="svc-a")]
    with django_capture_on_commit_callbacks(execute=True):
        summary = monitoring.run()

    issue.refresh_from_db()
    assert issue.status == IssueStatus.FIXED
    assert issue.fixed_in_version.startswith("recovered ")
    assert [f.check for f in summary["recovered"]] == [CHECK_EXPORTER_DOWN]
    # And it is announced, because "the monitoring can see again" is the other
    # half of the message somebody is waiting for.
    assert summary["announced"] is True
    assert "recovered" in sent_fallback[-1][1]


@pytest.mark.django_db
def test_a_blind_spot_that_comes_back_regresses(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    watch(TARGETS=["svc-a"])
    down = [FakePrometheus.sample(0, job="svc-a")]
    up = [FakePrometheus.sample(1, job="svc-a")]

    for vector in (down, up, down):
        prometheus.vectors['up{job="svc-a"}'] = vector
        with django_capture_on_commit_callbacks(execute=True):
            monitoring.run()

    issue = Issue.objects.get()
    assert issue.status == IssueStatus.REGRESSED
    assert issue.regressed_at is not None


@pytest.mark.django_db
def test_recovery_only_closes_checks_this_run_actually_asked(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    """A target removed from the config must NOT be closed as "recovered".

    Nothing recovered; nobody looked. Closing it would put a lie in the
    tracker that the tracker would then repeat forever.
    """
    watch(TARGETS=["svc-a"])
    prometheus.vectors['up{job="svc-a"}'] = [FakePrometheus.sample(0, job="svc-a")]
    with django_capture_on_commit_callbacks(execute=True):
        monitoring.run()
    assert Issue.objects.get().status == IssueStatus.NEW

    watch(TARGETS=[])  # svc-a is no longer configured at all
    with django_capture_on_commit_callbacks(execute=True):
        summary = monitoring.run()

    assert summary["recovered"] == []
    assert Issue.objects.get().status == IssueStatus.NEW


@pytest.mark.django_db
def test_a_reporter_does_not_try_to_close_anything(
    prometheus, watch, sent_fallback, settings, monkeypatch
):
    """A reporter has no store. Recovery is the owner's half of the job, and
    a reporter that silently did nothing here would look identical to one
    that closed the issue."""
    watch(TARGETS=["svc-gone"])
    settings.STAPEL_ALERTS = {
        **settings.STAPEL_ALERTS,
        "OWNER_URL": "https://owner.example.com",
        "SERVICE_KEY": "k",
    }
    Issue.objects.all().delete()

    assert monitoring.close_recovered([]) == []


# ── Configuration ───────────────────────────────────────────────────────


def test_an_unconfigured_watchdog_is_loud_about_it(settings, caplog):
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test"}
    assert monitoring.is_configured() is False

    summary = monitoring.run()
    assert summary["configured"] is False
    assert any("PROMETHEUS_URL" in r.message for r in caplog.records)


def test_expected_checks_cover_every_configured_question(prometheus, watch):
    watch(
        TARGETS=["svc-a"],
        METRICS=["m"],
        HEARTBEAT_ALERT="Watchdog",
        ALERTMANAGER_URL=prometheus.url,
    )
    checks = {f.check for f in monitoring.expected_checks()}

    assert checks == {
        CHECK_PROMETHEUS_UNREACHABLE,
        CHECK_EXPORTER_DOWN,
        CHECK_SCRAPE_MISSING,
        CHECK_METRIC_ABSENT,
        CHECK_HEARTBEAT_MISSING,
        CHECK_ALERTMANAGER_UNREACHABLE,
    }


# ── The command and the beat entry ──────────────────────────────────────


@pytest.mark.django_db
def test_the_management_command_files_and_reports(
    prometheus, watch, sent_fallback, django_capture_on_commit_callbacks
):
    from io import StringIO

    from django.core.management import call_command

    watch(TARGETS=["svc-gone"])
    out = StringIO()
    with django_capture_on_commit_callbacks(execute=True):
        call_command("alerts_watch_monitoring", stdout=out)

    assert "1 blind spot(s), 1 filed" in out.getvalue()
    assert Issue.objects.count() == 1


@pytest.mark.django_db
def test_dry_run_looks_without_touching(prometheus, watch, sent_fallback):
    from io import StringIO

    from django.core.management import call_command

    watch(TARGETS=["svc-gone"])
    out = StringIO()
    call_command("alerts_watch_monitoring", "--dry-run", stdout=out)

    assert "nothing filed" in out.getvalue()
    assert Issue.objects.count() == 0
    assert sent_fallback == []


def test_the_command_refuses_to_look_busy_when_unconfigured(settings):
    from io import StringIO

    from django.core.management import call_command

    settings.STAPEL_ALERTS = {"SERVICE": "svc-test"}
    err = StringIO()
    call_command("alerts_watch_monitoring", stderr=err)

    assert "PROMETHEUS_URL" in err.getvalue()


def test_the_beat_entry_names_a_task_that_exists():
    from stapel_alerts.beat import (
        WATCH_BEAT_KEY,
        WATCH_INTERVAL_SECONDS,
        WATCH_TASK_NAME,
        alerts_watch_monitoring,
        get_alerts_beat_schedule,
    )

    entry = get_alerts_beat_schedule()[WATCH_BEAT_KEY]
    assert entry["task"] == WATCH_TASK_NAME
    assert entry["schedule"] == 300.0
    assert WATCH_INTERVAL_SECONDS == 300
    # The name in the schedule must resolve to the callable, or beat schedules
    # a task nothing answers and the watchdog is silently never run.
    module_path, _, attr = WATCH_TASK_NAME.rpartition(".")
    import importlib

    assert getattr(importlib.import_module(module_path), attr) is alerts_watch_monitoring


def test_the_cadence_is_overridable():
    from stapel_alerts.beat import WATCH_BEAT_KEY, get_alerts_beat_schedule

    assert get_alerts_beat_schedule(seconds=60)[WATCH_BEAT_KEY]["schedule"] == 60.0


# ── The check that catches a watchdog nobody scheduled ──────────────────


def _watchdog_check(settings, **monitoring):
    from stapel_alerts.checks import check_watchdog_is_scheduled

    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "MONITORING": monitoring}
    return check_watchdog_is_scheduled(None)


def test_a_configured_watchdog_with_no_beat_entry_is_a_warning(settings):
    from stapel_alerts.checks import W_WATCHDOG_NOT_SCHEDULED

    settings.CELERY_BEAT_SCHEDULE = {"something-else": {"task": "app.other", "schedule": 60}}
    issues = _watchdog_check(settings, PROMETHEUS_URL="http://prometheus:9090")

    assert [i.id for i in issues] == [W_WATCHDOG_NOT_SCHEDULED]


def test_the_shipped_beat_entry_satisfies_the_check(settings):
    from stapel_alerts.beat import get_alerts_beat_schedule

    settings.CELERY_BEAT_SCHEDULE = {
        "something-else": {"task": "app.other", "schedule": 60},
        **get_alerts_beat_schedule(),
    }
    assert _watchdog_check(settings, PROMETHEUS_URL="http://prometheus:9090") == []


def test_a_host_with_no_beat_at_all_is_not_scolded(settings):
    """Cron and k8s CronJobs are not this check's business, and a warning that
    fires when the thing is fine is how the real one comes to be ignored."""
    settings.CELERY_BEAT_SCHEDULE = {}
    assert _watchdog_check(settings, PROMETHEUS_URL="http://prometheus:9090") == []


def test_declaring_the_cron_silences_it(settings):
    settings.CELERY_BEAT_SCHEDULE = {"something-else": {"task": "app.other", "schedule": 60}}
    assert _watchdog_check(
        settings, PROMETHEUS_URL="http://prometheus:9090", SCHEDULED=True
    ) == []


def test_an_unconfigured_watchdog_is_not_scolded_for_not_running(settings):
    settings.CELERY_BEAT_SCHEDULE = {"something-else": {"task": "app.other", "schedule": 60}}
    assert _watchdog_check(settings) == []
