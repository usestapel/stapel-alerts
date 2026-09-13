"""Ingest: turning an occurrence into a row in the tracker.

One entry point, :func:`record`, used by every input and by the HTTP report
endpoint. It does four things, in this order and for these reasons:

1. **Normalise and fingerprint** the trace (``normalise``). Everything below
   is about the fingerprint, never the raw text.
2. **Find the issue.** The fingerprint is the fast path — an exact hit is a
   single indexed lookup. Only on a miss does the similarity search run, over
   a bounded window of recent open issues of the same service and exception
   class; it is on the failure path, so it may not be a table scan.
3. **Flip the status if the evidence says so.** A ``fixed`` issue receiving a
   new event becomes ``regressed``, with ``regressed_at`` stamped. The store
   decides this; a caller cannot assert it.
4. **Write the event, cap the events, move the counters.**

Nothing here raises at the caller. Every input is on a failure path already,
and an alert store that turns a handled 500 into an unhandled one has made
the outage worse.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from . import normalise as norm
from .models import ErrorEvent, EventKind, Issue, IssueStatus, Level

logger = logging.getLogger(__name__)

#: Context values are truncated to this before storage. A trace with a 4 MB
#: serialized payload attached is not more diagnostic than one with 2 kB of
#: it, and the store must not become the biggest table in the database.
MAX_CONTEXT_VALUE = 2000
MAX_TRACE = 60000


def redact(context: dict | None) -> dict:
    """Strip secret-bearing keys out of *context* before it is stored.

    The key list is core's (``STAPEL_OBSERVABILITY["REDACT_FIELDS"]``), read
    at call time — a host that adds its own secret field name gets it honoured
    here without this module knowing the name. Nested dicts are walked; lists
    of dicts are walked one level, which is where request/response bodies put
    the things worth redacting.
    """
    if not context:
        return {}
    try:
        from stapel_core.observability.conf import observability_settings

        secret = frozenset(
            str(f).lower() for f in (observability_settings.REDACT_FIELDS or ())
        )
    except Exception:  # pragma: no cover - a library must not need the setting
        secret = frozenset()

    def _walk(value, depth=0):
        if depth > 4:
            return "<deep>"
        if isinstance(value, dict):
            return {
                k: ("***" if str(k).lower() in secret else _walk(v, depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_walk(v, depth + 1) for v in value[:50]]
        if isinstance(value, str):
            return value[:MAX_CONTEXT_VALUE]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:MAX_CONTEXT_VALUE]

    return _walk(context)


def _candidates(service: str, exc_class: str, limit: int):
    """Recent OPEN issues this trace could belong to.

    Narrowed by service and exception class before similarity is computed:
    a ``ForeignKeyViolation`` is never the same issue as a ``TimeoutError``
    however alike their frames read, and comparing across services would merge
    two bugs that are fixed and deployed separately.
    """
    qs = Issue.objects.filter(service=service).exclude(status=IssueStatus.MUTED)
    if exc_class:
        qs = qs.filter(exception_class=exc_class)
    return list(qs.order_by("-last_seen")[:limit])


def find_issue(
    *,
    fingerprint: str,
    service: str,
    exc_class: str,
    frames: list[str],
) -> Issue | None:
    """The issue this occurrence belongs to, by fingerprint then by similarity."""
    from .conf import alerts_settings

    exact = Issue.objects.filter(fingerprint=fingerprint).first()
    if exact is not None:
        return exact

    threshold = float(alerts_settings.SIMILARITY_THRESHOLD)
    limit = int(alerts_settings.SIMILARITY_CANDIDATES)
    best, best_score = None, 0.0
    for issue in _candidates(service, exc_class, limit):
        existing = issue.frames()
        if not norm.should_group(frames, existing, threshold=threshold):
            continue
        score = norm.similarity(frames, existing)
        if score > best_score:
            best, best_score = issue, score
    return best


@transaction.atomic
def record(
    *,
    trace: str = "",
    message: str = "",
    service: str = "",
    level: str = Level.ERROR,
    kind: str = EventKind.EXCEPTION,
    environment: str = "production",
    release: str = "",
    context: dict | None = None,
    request_path: str = "",
    trace_id: str = "",
    user_id=None,
    occurrences: int = 1,
    occurred_at=None,
    exc_class: str = "",
) -> ErrorEvent:
    """Store one occurrence. Returns the written :class:`ErrorEvent`.

    Creates or updates the :class:`Issue` it belongs to as a side effect —
    which is the whole point: the tracker is maintained BY the ingest, never
    by a human keeping it in step.
    """
    from .conf import alerts_settings

    now = timezone.now()
    occurred_at = occurred_at or now
    trace = (trace or "")[:MAX_TRACE]
    exc_class = exc_class or norm.exception_class(trace)
    frames = norm.normalise_trace(trace) if trace else [norm.normalise_line(message)]
    fingerprint = norm.fingerprint(
        trace or message, service=service, exc_class=exc_class, message=message
    )

    issue = find_issue(
        fingerprint=fingerprint, service=service, exc_class=exc_class, frames=frames
    )
    created = issue is None
    regressed = False

    if created:
        issue = Issue.objects.create(
            fingerprint=fingerprint,
            service=service,
            environment=environment,
            level=level,
            kind=kind,
            title=norm.title_for(trace, message=message),
            culprit=frames[0] if frames else "",
            exception_class=exc_class,
            normalised_trace="\n".join(frames),
            first_seen=occurred_at,
            last_seen=occurred_at,
            count=occurrences,
            count_since_fix=occurrences,
        )
    else:
        issue.count += occurrences
        issue.count_since_fix += occurrences
        if occurred_at > issue.last_seen:
            issue.last_seen = occurred_at
        if occurred_at < issue.first_seen:
            issue.first_seen = occurred_at
        # A fixed issue that produces an event has not been fixed — or the fix
        # has not been deployed. Either way the tracker must stop saying it is
        # closed, and it must say WHEN it came back, because the interesting
        # number is the gap between fixed_at and regressed_at.
        if issue.status == IssueStatus.FIXED:
            issue.status = IssueStatus.REGRESSED
            issue.regressed_at = now
            regressed = True
        elif issue.status == IssueStatus.MUTED and not issue.is_muted_now(now):
            # An expired mute is over. Leaving it would be a status nobody
            # revisits, which is how a muted issue outlives its reason.
            issue.status = IssueStatus.NEW
            issue.muted_until = None
        # An issue's level is the WORST it has been seen at: a bug that is
        # usually a warning and once took the process down is a fatal.
        if _severity(level) > _severity(issue.level):
            issue.level = level
        issue.save()

    event = ErrorEvent.objects.create(
        issue=issue,
        received_at=now,
        occurred_at=occurred_at,
        service=service,
        environment=environment,
        release=release,
        level=level,
        kind=kind,
        message=(message or "")[:MAX_TRACE],
        trace=trace,
        context=redact(context),
        request_path=(request_path or "")[:512],
        trace_id=(trace_id or "")[:64],
        user_id=user_id or None,
        occurrences=max(1, int(occurrences)),
    )

    _cap_events(issue, int(alerts_settings.EVENTS_PER_ISSUE))
    _export_to_sentry(issue, event)
    _after_record(issue, event, created=created, regressed=regressed)
    return event


_SEVERITY = {
    Level.DEBUG: 0,
    Level.INFO: 1,
    Level.WARNING: 2,
    Level.ERROR: 3,
    Level.FATAL: 4,
}


def _severity(level: str) -> int:
    return _SEVERITY.get(level, 3)


def _cap_events(issue: Issue, keep: int) -> None:
    """Keep the last *keep* events of an issue and drop the rest.

    The issue's counters are NOT touched: a hundred-thousand-occurrence issue
    stays a hundred-thousand-occurrence issue after its events are swept. The
    events are the sample; the count is the fact.
    """
    if keep <= 0:
        return
    ids = list(
        ErrorEvent.objects.filter(issue=issue)
        .order_by("-received_at")
        .values_list("id", flat=True)[keep:]
    )
    if ids:
        ErrorEvent.objects.filter(id__in=ids).delete()


def _export_to_sentry(issue: Issue, event: ErrorEvent) -> None:
    """Forward to Sentry when a DSN is configured, and store the id back.

    Storing locally is never conditional on this. The interface is the same
    whether or not Sentry is connected — that is the whole premise of this
    library — and the forward is best-effort on top.
    """
    from .sentry import forward

    sentry_id = forward(event)
    if not sentry_id:
        return
    event.sentry_event_id = sentry_id
    event.save(update_fields=["sentry_event_id"])
    issue.sentry_event_id = sentry_id
    issue.save(update_fields=["sentry_event_id"])


def _after_record(issue: Issue, event: ErrorEvent, *, created: bool, regressed: bool) -> None:
    """Metrics and notifications, after commit, never in the caller's way."""
    from .metrics import observe_event
    from .notify import notify_issue

    def _run():
        try:
            observe_event(issue, created=created)
            notify_issue(issue, event, created=created, regressed=regressed)
        except Exception:
            logger.warning("alerts: post-record hooks failed", exc_info=True)

    transaction.on_commit(_run)


# ── Tracker operations (what an agent calls) ────────────────────────────


def mark_fixed(issue: Issue, *, version: str = "", sha: str = "") -> Issue:
    """Close an issue with the release that fixed it.

    ``count_since_fix`` is zeroed here, so "has anything happened since the
    fix" is a column and not a query over events that may already be swept.
    """
    issue.status = IssueStatus.FIXED
    issue.fixed_in_version = (version or "")[:64]
    issue.fixed_in_sha = (sha or "")[:64]
    issue.fixed_at = timezone.now()
    issue.regressed_at = None
    issue.count_since_fix = 0
    issue.save(
        update_fields=[
            "status", "fixed_in_version", "fixed_in_sha",
            "fixed_at", "regressed_at", "count_since_fix",
        ]
    )
    from .metrics import refresh_open_gauges

    refresh_open_gauges()
    return issue


def set_status(issue: Issue, status: str, *, note: str | None = None, muted_until=None) -> Issue:
    """Set a status a caller is allowed to assert.

    ``regressed`` is not one of them: it is the store's verdict on evidence,
    and a caller who could set it could also hide a regression by not setting
    it. The API layer refuses it before this is reached.
    """
    issue.status = status
    if note is not None:
        issue.note = note
    if status == IssueStatus.MUTED:
        issue.muted_until = muted_until
    issue.save(update_fields=["status", "note", "muted_until"])
    from .metrics import refresh_open_gauges

    refresh_open_gauges()
    return issue


def erase_subject(user_id) -> int:
    """Forget a data subject. Returns the number of events changed.

    The user id is dropped and any context entry that could name them goes
    with it — but the EVENT stays, because an alert store's rows are the
    record of a failure, not a record about a person. What is erased is the
    link; what remains is "this crashed".
    """
    qs = ErrorEvent.objects.filter(user_id=user_id)
    changed = 0
    for event in qs.iterator():
        event.user_id = None
        event.context = _strip_subject(event.context, str(user_id))
        event.save(update_fields=["user_id", "context"])
        changed += 1
    return changed


def _strip_subject(context, subject: str):
    if isinstance(context, dict):
        return {
            k: ("<erased>" if _names_subject(v, subject) else _strip_subject(v, subject))
            for k, v in context.items()
        }
    if isinstance(context, list):
        return [_strip_subject(v, subject) for v in context]
    if isinstance(context, str) and subject and subject in context:
        return context.replace(subject, "<erased>")
    return context


def _names_subject(value, subject: str) -> bool:
    return isinstance(value, str) and bool(subject) and value == subject


__all__ = [
    "record",
    "find_issue",
    "redact",
    "mark_fixed",
    "set_status",
    "erase_subject",
    "MAX_CONTEXT_VALUE",
    "MAX_TRACE",
]
