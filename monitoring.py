"""The watchdog that watches the watchman.

Every other input in this library reports a failure the fleet *had*. This one
reports a failure the fleet cannot see — a scrape target Prometheus has
stopped collecting, a metric that no longer exists, an Alertmanager that is
silenced or unreachable, a dead-man's-switch alert that has stopped firing.

The reason it belongs in the alert store and not in a dashboard: a monitoring
stack with a blind spot produces exactly the same picture as a healthy fleet —
no alerts. "Nothing is firing" is the observable state of both, and the only
way to tell them apart is for something outside the stack to ask. So the blind
spot becomes a row in the tracker, with a status, a count and a fix, like any
other bug.

Two channels, deliberately, per the owner's rule: a finding is filed as a
``kind="monitoring"`` report **and** pushed to the Telegram fallback seam
directly. The normal notification path only speaks on a new issue, a
regression or a spike; a blind spot that has been open for six hours would be
silent on exactly the run where somebody should have looked. And the fallback
seam is the one channel this library guarantees does not depend on the
infrastructure being watched.

Recovery is the other half. A finding that stops appearing closes its issue
with ``fixed_in_version = "recovered <ts>"`` — a watchdog that can only open
issues produces a tracker full of blind spots that were fixed weeks ago, and
a tracker nobody trusts is a tracker nobody reads.

    STAPEL_ALERTS = {
        "MONITORING": {
            "PROMETHEUS_URL": "http://prometheus:9090",
            "TARGETS": ["svc-billing", "svc-api"],
            "METRICS": ["alerts_open_total", "bus_dlq_total"],
            "ALERTMANAGER_URL": "http://alertmanager:9093",
            "HEARTBEAT_ALERT": "Watchdog",
        },
    }

Nothing here raises at the caller: the watchdog runs on a schedule, and a
crashing watchdog is a blind spot of its own.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import normalise as norm

logger = logging.getLogger(__name__)

QUERY_PATH = "/api/v1/query"
SILENCES_PATH = "/api/v2/silences"

#: The watchdog's own names for what it looked at. They travel in the event
#: context as ``check`` and they are part of the message, so an issue is one
#: (check, target) pair for as long as it keeps failing.
CHECK_PROMETHEUS_UNREACHABLE = "prometheus_unreachable"
CHECK_EXPORTER_DOWN = "exporter_down"
CHECK_SCRAPE_MISSING = "scrape_missing"
CHECK_METRIC_ABSENT = "metric_absent"
CHECK_ALERTMANAGER_UNREACHABLE = "alertmanager_unreachable"
CHECK_ALERTMANAGER_SILENCED = "alertmanager_silenced"
CHECK_HEARTBEAT_MISSING = "heartbeat_missing"

#: `fatal` is for the checks where the stack has gone BLIND — nothing is
#: being collected, so no other alert can fire either. `error` is for the
#: checks where the stack can still see: a target that is down is visible,
#: and a silence is somebody's deliberate act that has outlived its reason.
_LEVELS = {
    CHECK_PROMETHEUS_UNREACHABLE: "fatal",
    CHECK_SCRAPE_MISSING: "fatal",
    CHECK_METRIC_ABSENT: "fatal",
    CHECK_HEARTBEAT_MISSING: "fatal",
    CHECK_ALERTMANAGER_UNREACHABLE: "fatal",
    CHECK_EXPORTER_DOWN: "error",
    CHECK_ALERTMANAGER_SILENCED: "error",
}

MONITORING_KIND = "monitoring"


class PrometheusUnreachable(Exception):
    """Prometheus did not answer, or did not answer with a result."""


@dataclass(frozen=True)
class Finding:
    """One blind spot, in the shape the tracker groups on.

    ``message`` carries no varying number on purpose. The fingerprint is
    computed over the normalised message, and a message that said "down for
    900s" would open a new issue on every run — ninety issues for one outage,
    which is the failure mode grouping exists to prevent. The numbers live in
    ``detail``, where they are visible on the event and invisible to the
    fingerprint.
    """

    check: str
    target: str
    #: Out of `compare` (and therefore out of the generated `__hash__`) so a
    #: Finding is identified by the question it answers, not by this run's
    #: numbers — which is what lets a set of them be compared across runs.
    detail: dict = field(default_factory=dict, compare=False)

    @property
    def level(self) -> str:
        return _LEVELS.get(self.check, "error")

    @property
    def message(self) -> str:
        return f"monitoring blind spot: {self.check} on {self.target}"

    def context(self) -> dict:
        return {"check": self.check, "target": self.target, **self.detail}

    def line(self) -> str:
        extra = ", ".join(f"{k}={v}" for k, v in sorted(self.detail.items()))
        return f"[{self.level}] {self.check}: {self.target}" + (f" ({extra})" if extra else "")


# ── Settings ────────────────────────────────────────────────────────────


def monitoring_settings() -> dict:
    from .conf import alerts_settings

    return dict(alerts_settings.MONITORING or {})


def is_configured() -> bool:
    """Has a host pointed this at a Prometheus?

    Unconfigured is the normal state of a deployment that has not asked for
    the watchdog, and it is a no-op rather than an error — but an explicitly
    scheduled run says so out loud, because a watchdog that silently does
    nothing is the thing this module exists to catch.
    """
    return bool(str(monitoring_settings().get("PROMETHEUS_URL", "") or "").strip())


def _service() -> str:
    from .conf import alerts_settings

    return monitoring_settings().get("SERVICE") or alerts_settings.SERVICE or "unknown"


def _timeout() -> float:
    return float(monitoring_settings().get("TIMEOUT_SECONDS", 5.0) or 5.0)


# ── The Prometheus client ───────────────────────────────────────────────


def _get_json(url: str) -> dict | list:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_timeout()) as response:
        if not 200 <= response.status < 300:
            raise PrometheusUnreachable(f"HTTP {response.status} from {url}")
        return json.loads(response.read().decode("utf-8"))


def instant_query(expr: str) -> list[dict]:
    """Run one instant query and return its result vector.

    Raises :class:`PrometheusUnreachable` for a transport failure, a non-2xx,
    or a body whose ``status`` is not ``success`` — all three mean the same
    thing to a watchdog: this question was not answered, so treat it as blind
    rather than as "no results", which would read as healthy.
    """
    base = str(monitoring_settings().get("PROMETHEUS_URL", "")).rstrip("/")
    url = f"{base}{QUERY_PATH}?" + urllib.parse.urlencode({"query": expr})
    try:
        body = _get_json(url)
    except PrometheusUnreachable:
        raise
    except Exception as exc:
        raise PrometheusUnreachable(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(body, dict) or body.get("status") != "success":
        raise PrometheusUnreachable(
            f"prometheus answered status={(body or {}).get('status') if isinstance(body, dict) else '?'}"
        )
    result = ((body.get("data") or {}).get("result")) or []
    return list(result) if isinstance(result, list) else []


def _selector(target: str) -> str:
    """``up{job="svc-a"}`` for a bare name, ``up{<matchers>}`` for a matcher.

    A fleet whose targets are distinguished by ``instance`` rather than
    ``job`` configures ``'instance="10.0.0.4:9100"'`` and gets the same
    checks, without this module owning a second setting for it.
    """
    target = str(target).strip()
    if "=" in target:
        return f"up{{{target}}}"
    return f'up{{job="{target}"}}'


def _sample_value(sample: dict) -> str:
    value = sample.get("value") or []
    return str(value[1]) if len(value) > 1 else ""


# ── The checks ──────────────────────────────────────────────────────────


def check_targets() -> list[Finding]:
    """``up == 0``, and the harder case: ``up`` with no series at all.

    A target reporting ``up 0`` is a known-down exporter. A target with NO
    ``up`` series has been dropped from the scrape config or renamed, and
    that is strictly worse: an expression over its metrics returns no data,
    an alert on no data does not fire, and the fleet reads as healthy.
    """
    findings: list[Finding] = []
    for target in monitoring_settings().get("TARGETS") or ():
        samples = instant_query(_selector(target))
        if not samples:
            findings.append(
                Finding(CHECK_SCRAPE_MISSING, str(target), {"selector": _selector(target)})
            )
            continue
        for sample in samples:
            if _sample_value(sample) != "0":
                continue
            labels = sample.get("metric") or {}
            findings.append(
                Finding(
                    CHECK_EXPORTER_DOWN,
                    str(target),
                    {"instance": labels.get("instance", ""), "job": labels.get("job", "")},
                )
            )
    return findings


def check_metrics() -> list[Finding]:
    """``absent(<metric>)`` on each named metric.

    ``absent()`` returns a series when the metric has NO series, which is the
    only way to alert on a metric that stopped existing: every other
    expression over it returns nothing, and nothing does not fire.
    """
    findings: list[Finding] = []
    for metric in monitoring_settings().get("METRICS") or ():
        name = str(metric).strip()
        if not name:
            continue
        if instant_query(f"absent({name})"):
            findings.append(Finding(CHECK_METRIC_ABSENT, name, {"expr": f"absent({name})"}))
    return findings


def check_heartbeat() -> list[Finding]:
    """The dead-man's switch: an alert that is supposed to be firing always.

    ``HEARTBEAT_ALERT`` names an always-firing alert whose delivery proves
    the whole chain works — rule evaluation, Alertmanager, the receiver. If
    it has stopped firing, the chain is broken, and every other alert in it
    is broken the same way while showing nothing.
    """
    name = str(monitoring_settings().get("HEARTBEAT_ALERT", "") or "").strip()
    if not name:
        return []
    expr = f'ALERTS{{alertname="{name}",alertstate="firing"}}'
    if instant_query(expr):
        return []
    return [Finding(CHECK_HEARTBEAT_MISSING, name, {"expr": expr})]


def check_alertmanager() -> list[Finding]:
    """Active silences, and an Alertmanager that does not answer.

    A silence is somebody muting a page at 3am. It is a blind spot from the
    moment they go back to sleep, and the thing nobody does is come back and
    remove it — which is why it is reported as an issue with a count rather
    than left to a UI nobody opens.
    """
    base = str(monitoring_settings().get("ALERTMANAGER_URL", "") or "").strip().rstrip("/")
    if not base:
        return []
    try:
        body = _get_json(f"{base}{SILENCES_PATH}")
    except Exception as exc:
        return [
            Finding(
                CHECK_ALERTMANAGER_UNREACHABLE,
                base,
                {"error": f"{type(exc).__name__}: {exc}"[:500]},
            )
        ]

    findings: list[Finding] = []
    for silence in body if isinstance(body, list) else ():
        if not isinstance(silence, dict):
            continue
        if (silence.get("status") or {}).get("state") != "active":
            continue
        findings.append(
            Finding(
                CHECK_ALERTMANAGER_SILENCED,
                _matcher_key(silence.get("matchers") or ()),
                {
                    "silence_id": str(silence.get("id", "")),
                    "comment": str(silence.get("comment", ""))[:200],
                    "ends_at": str(silence.get("endsAt", "")),
                },
            )
        )
    return findings


def _matcher_key(matchers) -> str:
    """A silence's matchers, rendered stably.

    The silence *id* is a fresh uuid every time somebody re-silences the same
    alert, so an issue keyed on it would be a new issue each time. The
    matchers are what the silence is ABOUT, and they are stable across
    re-silencing — which is exactly the recurrence worth a rising count.
    """
    parts = []
    for matcher in matchers or ():
        if not isinstance(matcher, dict):
            continue
        operator = "=~" if matcher.get("isRegex") else "="
        parts.append(f'{matcher.get("name", "")}{operator}"{matcher.get("value", "")}"')
    return ",".join(sorted(parts)) or "<no matchers>"


# ── The universe of possible findings, for recovery ─────────────────────


def expected_checks() -> list[Finding]:
    """Every (check, target) this configuration can produce.

    Recovery needs it. A finding that is absent from a run is only evidence
    of recovery if the run actually ASKED the question — otherwise removing a
    target from the config would close its issue as "recovered", which is a
    lie the tracker would then repeat forever.
    """
    settings = monitoring_settings()
    expected: list[Finding] = [
        Finding(CHECK_PROMETHEUS_UNREACHABLE, str(settings.get("PROMETHEUS_URL", "")).rstrip("/"))
    ]
    for target in settings.get("TARGETS") or ():
        expected.append(Finding(CHECK_EXPORTER_DOWN, str(target)))
        expected.append(Finding(CHECK_SCRAPE_MISSING, str(target)))
    for metric in settings.get("METRICS") or ():
        if str(metric).strip():
            expected.append(Finding(CHECK_METRIC_ABSENT, str(metric).strip()))
    heartbeat = str(settings.get("HEARTBEAT_ALERT", "") or "").strip()
    if heartbeat:
        expected.append(Finding(CHECK_HEARTBEAT_MISSING, heartbeat))
    alertmanager = str(settings.get("ALERTMANAGER_URL", "") or "").strip().rstrip("/")
    if alertmanager:
        expected.append(Finding(CHECK_ALERTMANAGER_UNREACHABLE, alertmanager))
    return expected


def fingerprint_of(finding: Finding) -> str:
    """The fingerprint ``capture()`` will compute for this finding.

    Reproduced here rather than looked up, so recovery can find an issue by
    its id instead of by matching text. It is the same call ``capture`` and
    ``services.record`` both make; a change to either would break the
    recovery test, which is the point of writing it as one expression.
    """
    return norm.fingerprint(
        finding.message, service=_service(), exc_class="", message=finding.message
    )


# ── The run ─────────────────────────────────────────────────────────────


def collect() -> list[Finding]:
    """Ask every configured question once. Never raises.

    A transport failure against Prometheus is itself the first finding, and
    it short-circuits the target/metric/heartbeat checks: they all go through
    the same endpoint, and reporting five copies of one outage would bury the
    one line that says what happened.
    """
    try:
        findings = check_targets() + check_metrics() + check_heartbeat()
    except PrometheusUnreachable as exc:
        base = str(monitoring_settings().get("PROMETHEUS_URL", "")).rstrip("/")
        findings = [Finding(CHECK_PROMETHEUS_UNREACHABLE, base, {"error": str(exc)[:500]})]
    except Exception as exc:  # pragma: no cover - a watchdog may not crash
        logger.warning("alerts: monitoring watchdog failed (%s)", exc, exc_info=True)
        base = str(monitoring_settings().get("PROMETHEUS_URL", "")).rstrip("/")
        findings = [Finding(CHECK_PROMETHEUS_UNREACHABLE, base, {"error": str(exc)[:500]})]

    try:
        findings += check_alertmanager()
    except Exception as exc:  # pragma: no cover - already guarded inside
        logger.warning("alerts: alertmanager check failed (%s)", exc, exc_info=True)
    return findings


def report(findings: list[Finding]) -> int:
    """File each finding as a ``kind="monitoring"`` event. Returns how many."""
    from .capture import capture

    filed = 0
    for finding in findings:
        if capture(
            finding.message,
            level=finding.level,
            kind=MONITORING_KIND,
            service=_service(),
            context=finding.context(),
        ):
            filed += 1
    return filed


def close_recovered(findings: list[Finding]) -> list[Finding]:
    """Close the issues of every expected check that did NOT fire this run.

    Only in owner mode: a reporter has no store to close anything in. The
    ``fixed_in_version`` is ``recovered <ts>`` rather than a release, because
    nobody deployed anything — the thing that was blind can see again, and
    the tracker should say so in words that cannot be mistaken for a fix.
    """
    from django.utils import timezone

    from .models import OPEN_STATUSES, Issue
    from .services import mark_fixed
    from .transport import resolve_mode

    if resolve_mode() != "owner":
        logger.debug("alerts: monitoring recovery needs the owner's store; skipped")
        return []

    failing = {(f.check, f.target) for f in findings}
    stamp = timezone.now().isoformat(timespec="seconds")
    recovered: list[Finding] = []
    for expected in expected_checks():
        if (expected.check, expected.target) in failing:
            continue
        issue = Issue.objects.filter(
            fingerprint=fingerprint_of(expected), kind=MONITORING_KIND
        ).first()
        if issue is None or issue.status not in OPEN_STATUSES:
            continue
        mark_fixed(issue, version=f"recovered {stamp}"[:64])
        recovered.append(expected)
    return recovered


def announce(findings: list[Finding], recovered: list[Finding]) -> bool:
    """Push the run's verdict at the Telegram fallback seam, directly.

    Directly, and on EVERY run that has something to say, because the normal
    notification thresholds (new / regressed / spike) are the wrong ones
    here: a blind spot on its ninth consecutive run is not less urgent than
    on its first, and "the monitoring is still blind" is the message somebody
    needs at the moment they look at their phone. If the cadence is too loud,
    the cadence is a setting — silence is not.
    """
    from .fallback import notify

    if not findings and not recovered:
        return False

    service = _service()
    if findings:
        subject = f"[{service}] monitoring blind: {len(findings)} check(s) failing"
    else:
        subject = f"[{service}] monitoring recovered"

    lines = [subject, ""]
    for finding in findings:
        lines.append(finding.line())
    if recovered:
        lines.append("")
        lines.append("recovered:")
        for finding in recovered:
            lines.append(f"  {finding.check}: {finding.target}")
    return bool(notify(subject, "\n".join(lines)))


def run() -> dict:
    """One watchdog pass. Returns a summary; never raises.

    The order is deliberate: collect, file, close, announce. Filing before
    closing means a check that flapped inside one run (down, then back) is
    recorded as an occurrence AND closed, rather than silently dropped.
    """
    if not is_configured():
        logger.warning(
            "alerts: the monitoring watchdog ran with no PROMETHEUS_URL "
            'configured. Set STAPEL_ALERTS["MONITORING"]["PROMETHEUS_URL"], or '
            "unschedule it — a watchdog that checks nothing reads exactly like "
            "a fleet with nothing wrong."
        )
        return {"configured": False, "findings": [], "reported": 0, "recovered": []}

    findings = collect()
    reported = report(findings)
    recovered = close_recovered(findings)
    announced = announce(findings, recovered)
    return {
        "configured": True,
        "findings": findings,
        "reported": reported,
        "recovered": recovered,
        "announced": announced,
    }


__all__ = [
    "CHECK_ALERTMANAGER_SILENCED",
    "CHECK_ALERTMANAGER_UNREACHABLE",
    "CHECK_EXPORTER_DOWN",
    "CHECK_HEARTBEAT_MISSING",
    "CHECK_METRIC_ABSENT",
    "CHECK_PROMETHEUS_UNREACHABLE",
    "CHECK_SCRAPE_MISSING",
    "MONITORING_KIND",
    "Finding",
    "PrometheusUnreachable",
    "announce",
    "check_alertmanager",
    "check_heartbeat",
    "check_metrics",
    "check_targets",
    "close_recovered",
    "collect",
    "expected_checks",
    "fingerprint_of",
    "instant_query",
    "is_configured",
    "monitoring_settings",
    "report",
    "run",
]
