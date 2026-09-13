"""Models for stapel-alerts.

Two tables, because they answer two different questions (owner's ruling,
2026-09-13):

``Issue``    the TRACKER. One row per bug. What an agent reads, fixes and
             closes. Fingerprint, title, service, status, counters,
             first/last seen, the version that fixed it, when it came back.
``ErrorEvent`` one OCCURRENCE, with the full trace and context. Many per
             issue, capped by retention. What a human reads once they know
             which issue they are looking at.

Plus ``Service`` — the registry of who may report. A reporter authenticates
with a key that exists here only as a hash; the plaintext is shown once, at
creation, and never again.

The user id is a plain ``UUIDField``, not an FK: an alert store must be able
to record a failure involving an account this database has never heard of
(that is precisely the failure class the 2026-09-13 investigation was about),
and it must not need a shadow row to do it.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid

from django.db import models
from django.utils import timezone


class IssueStatus(models.TextChoices):
    """Where an issue is in its life.

    Members:
        NEW: Seen, not yet fixed. The default.
        FIXED: A version/sha claims to have fixed it (``POST /issues/{id}/fix``).
        REGRESSED: A FIXED issue received a new event. Set by the store, never
            by a caller — it is evidence, not an opinion.
        MUTED: Known, accepted, and deliberately not notified about.
    """

    NEW = "new", "New"
    FIXED = "fixed", "Fixed"
    REGRESSED = "regressed", "Regressed"
    MUTED = "muted", "Muted"


#: Statuses that mean "this bug is live". `alerts_open_total` counts these.
OPEN_STATUSES = (IssueStatus.NEW, IssueStatus.REGRESSED)


class Level(models.TextChoices):
    DEBUG = "debug", "Debug"
    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"
    FATAL = "fatal", "Fatal"


class EventKind(models.TextChoices):
    """Where an event came from — the input, not the severity.

    Members:
        EXCEPTION: An unhandled exception (the fleet exception handler, Celery).
        LOG: A log record at or above the configured level.
        DLQ: An event or task parked in a dead-letter queue — work dropped.
        MONITORING: A watchdog's report that the monitoring stack itself has a
            blind spot (a scrape gap, an exporter down, a silence). A blind
            monitoring stack is an issue in this tracker like any other.
        MANUAL: An explicit ``alerts.capture(...)``.
    """

    EXCEPTION = "exception", "Exception"
    LOG = "log", "Log"
    DLQ = "dlq", "DLQ park"
    MONITORING = "monitoring", "Monitoring"
    MANUAL = "manual", "Manual"


def hash_key(raw: str) -> str:
    """The at-rest form of a service key. sha256, hex.

    Not a password hash: this is a 32-byte random token, not a memorable
    secret, so there is no dictionary to slow down and no salt to add — the
    token's own entropy is the whole defence. What matters is that a database
    dump does not hand an attacker the ability to write into the tracker.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """A fresh service key. Shown once; only :func:`hash_key` of it is stored."""
    return secrets.token_urlsafe(32)


class Service(models.Model):
    """A process allowed to report into this store.

    One row per service in the fleet. ``key_hash`` is the only form of the key
    that exists here; ``Service.issue_key()`` returns the plaintext exactly
    once, to whoever created the row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64, unique=True)
    key_hash = models.CharField(max_length=64, db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_report_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "alerts_service"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @classmethod
    def create_with_key(cls, name: str) -> tuple["Service", str]:
        """Create a service and return it with its plaintext key, once."""
        raw = generate_key()
        return cls.objects.create(name=name, key_hash=hash_key(raw)), raw

    def rotate_key(self) -> str:
        raw = generate_key()
        self.key_hash = hash_key(raw)
        self.save(update_fields=["key_hash"])
        return raw

    @classmethod
    def authenticate(cls, raw_key: str) -> "Service | None":
        """The service this key belongs to, or None.

        Looked up BY HASH, so the query itself never carries the plaintext
        into a slow-query log.
        """
        if not raw_key:
            return None
        return cls.objects.filter(key_hash=hash_key(raw_key), is_active=True).first()


class Issue(models.Model):
    """One bug, as the tracker knows it.

    The fingerprint is unique per issue and stable across occurrences — see
    ``normalise.fingerprint``. ``normalised_trace`` is kept because it is what
    the next event is compared against: recomputing it from a stored raw trace
    on every capture would put the normaliser on the failure path twice.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    fingerprint = models.CharField(max_length=64, unique=True, db_index=True)
    service = models.CharField(max_length=64, db_index=True)
    environment = models.CharField(max_length=32, default="production", db_index=True)
    level = models.CharField(
        max_length=16, choices=Level.choices, default=Level.ERROR, db_index=True
    )
    kind = models.CharField(
        max_length=16, choices=EventKind.choices, default=EventKind.EXCEPTION
    )

    title = models.CharField(max_length=255)
    culprit = models.CharField(max_length=255, blank=True, default="")
    exception_class = models.CharField(max_length=128, blank=True, default="", db_index=True)
    #: The normalised frames of the trace that opened this issue, newline
    #: joined. The comparison surface for ``should_group``.
    normalised_trace = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=16, choices=IssueStatus.choices, default=IssueStatus.NEW, db_index=True
    )
    note = models.TextField(blank=True, default="")

    count = models.PositiveIntegerField(default=0)
    #: Occurrences seen since the issue was last marked fixed. Zeroed by a
    #: fix; it is what makes "has this regressed since the fix" answerable
    #: without reading the event table.
    count_since_fix = models.PositiveIntegerField(default=0)

    first_seen = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)

    #: The release/sha that claims the fix. Written by ``POST /issues/{id}/fix``
    #: or by CI on a commit that references ``alerts:<issue-id>``.
    fixed_in_version = models.CharField(max_length=64, blank=True, default="")
    fixed_in_sha = models.CharField(max_length=64, blank=True, default="")
    fixed_at = models.DateTimeField(null=True, blank=True)
    #: When a FIXED issue received a new event. Evidence that the fix did not
    #: hold — or that it was never deployed.
    regressed_at = models.DateTimeField(null=True, blank=True)
    muted_until = models.DateTimeField(null=True, blank=True)

    #: The Sentry event id of the most recent forwarded occurrence, when a DSN
    #: is configured. Null is the normal state — this store does not need one.
    sentry_event_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "alerts_issue"
        ordering = ("-last_seen",)
        indexes = [
            models.Index(fields=["service", "status", "-last_seen"], name="alerts_issue_triage"),
            models.Index(fields=["status", "level"], name="alerts_issue_open"),
        ]

    def __str__(self) -> str:
        return f"[{self.service}] {self.title}"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def frames(self) -> list[str]:
        return self.normalised_trace.splitlines() if self.normalised_trace else []

    def is_muted_now(self, now=None) -> bool:
        """Muted, and the mute has not expired.

        A mute with no ``muted_until`` is permanent; one whose deadline has
        passed is over, and the next event un-mutes the issue rather than
        leaving a stale status nobody notices.
        """
        if self.status != IssueStatus.MUTED:
            return False
        if self.muted_until is None:
            return True
        return (now or timezone.now()) < self.muted_until


class ErrorEvent(models.Model):
    """One occurrence: the full trace and the context around it.

    ``context`` passes through the core redaction seam before it is stored —
    an alert store that keeps a bearer token because it was in a request
    header is a breach with a nice UI.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="events")

    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    #: When the reporting process saw it. Differs from received_at by the
    #: buffer's age when the owner was unreachable, which is exactly the
    #: number that says how long the store was blind.
    occurred_at = models.DateTimeField(default=timezone.now)

    service = models.CharField(max_length=64, db_index=True)
    environment = models.CharField(max_length=32, default="production")
    release = models.CharField(max_length=64, blank=True, default="")
    level = models.CharField(max_length=16, choices=Level.choices, default=Level.ERROR)
    kind = models.CharField(
        max_length=16, choices=EventKind.choices, default=EventKind.EXCEPTION
    )

    message = models.TextField(blank=True, default="")
    trace = models.TextField(blank=True, default="")
    context = models.JSONField(default=dict, blank=True)

    request_path = models.CharField(max_length=512, blank=True, default="")
    trace_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    #: Plain uuid, deliberately not an FK — see the module docstring.
    user_id = models.UUIDField(null=True, blank=True, db_index=True)

    #: How many occurrences this row stands for. A rate-limited reporter sends
    #: one row for a window and says how many it swallowed, so the counters
    #: stay true while the event table stays small.
    occurrences = models.PositiveIntegerField(default=1)

    sentry_event_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "alerts_error_event"
        ordering = ("-received_at",)
        indexes = [
            models.Index(fields=["issue", "-received_at"], name="alerts_event_by_issue"),
        ]

    def __str__(self) -> str:
        return f"{self.service} {self.level} @ {self.received_at:%Y-%m-%d %H:%M:%S}"


__all__ = [
    "IssueStatus",
    "OPEN_STATUSES",
    "Level",
    "EventKind",
    "Service",
    "Issue",
    "ErrorEvent",
    "hash_key",
    "generate_key",
]
