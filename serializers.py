"""Request/response serializers for the alerts API.

Machine-first, per the owner's ruling: an agent must be able to read the
tracker, fix a bug, and close the issue over this API. That shapes the
choices here — stable string ids, no HTML, no nested prose, every field named
the way the model names it.
"""
from rest_framework import serializers

from .models import ErrorEvent, Issue, IssueStatus

#: Statuses a CALLER may assign. `regressed` is absent on purpose: the store
#: sets it from evidence (see services.record), and a caller able to set it
#: would be able to withhold it.
SETTABLE_STATUSES = (IssueStatus.NEW, IssueStatus.FIXED, IssueStatus.MUTED)

#: Events accepted in one POST /report. Bounded because the endpoint is
#: reached with a service key from a process that may be looping.
MAX_BATCH = 100


class IssueSerializer(serializers.ModelSerializer):
    """The tracker row. What an agent lists, reads and reconciles against."""

    id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Issue
        fields = (
            "id", "fingerprint", "service", "environment", "level", "kind",
            "title", "culprit", "exception_class", "status", "note",
            "count", "count_since_fix", "first_seen", "last_seen",
            "fixed_in_version", "fixed_in_sha", "fixed_at", "regressed_at",
            "muted_until", "sentry_event_id",
        )
        read_only_fields = fields


class ErrorEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ErrorEvent
        fields = (
            "id", "issue", "received_at", "occurred_at", "service",
            "environment", "release", "level", "kind", "message", "trace",
            "context", "request_path", "trace_id", "user_id", "occurrences",
            "sentry_event_id",
        )
        read_only_fields = fields


class IssueDetailSerializer(IssueSerializer):
    events = ErrorEventSerializer(many=True, read_only=True)

    class Meta(IssueSerializer.Meta):
        fields = IssueSerializer.Meta.fields + ("events",)
        read_only_fields = fields


class IssuePageSerializer(serializers.Serializer):
    """The envelope ``GET /issues`` returns: ``{count, offset, limit, results}``.

    Both the wire and the contract. The list view renders its response THROUGH
    this serializer and declares it as the response, so the schema cannot say
    ``Issue[]`` while the body carries a page — which is what 0.2.0 did, with
    a hand-written ``responses=IssueSerializer(many=True)`` that the generator
    had no way to check against the method body.

    ``results`` is the row serializer the view resolved through its seam
    (``SerializerSeamMixin``), so a host that swaps the row shape gets the same
    envelope around its own rows.
    """

    count = serializers.IntegerField(min_value=0)
    offset = serializers.IntegerField(min_value=0)
    limit = serializers.IntegerField(min_value=1)
    results = IssueSerializer(many=True)

    def __init__(self, *args, row_serializer_class=None, **kwargs):
        super().__init__(*args, **kwargs)
        if row_serializer_class is not None and row_serializer_class is not IssueSerializer:
            self.fields["results"] = row_serializer_class(many=True)


class IssuePatchSerializer(serializers.Serializer):
    """``PATCH /issues/{id}`` — status, note, and/or the mute deadline.

    ``muted_until`` on its own is a mute: the field has no meaning in any other
    status, so a patch that carries only the deadline sets ``status=muted``
    (``null`` for a mute with no deadline). With any status other than
    ``muted`` the deadline is ignored and cleared.
    """

    status = serializers.ChoiceField(
        choices=[s.value for s in SETTABLE_STATUSES], required=False
    )
    note = serializers.CharField(required=False, allow_blank=True)
    muted_until = serializers.DateTimeField(required=False, allow_null=True)


class IssueFixSerializer(serializers.Serializer):
    """``POST /issues/{id}/fix`` — the release that claims the fix.

    Both fields are optional and both are recorded: a CI caller has a sha, a
    human closing an issue after a hotfix may only have a version, and an
    issue closed with neither is still closed — just less useful when it
    regresses and somebody asks which deploy was supposed to have fixed it.
    """

    version = serializers.CharField(required=False, allow_blank=True, max_length=64)
    sha = serializers.CharField(required=False, allow_blank=True, max_length=64)


class ReportEventSerializer(serializers.Serializer):
    """One event in a ``POST /report`` batch — the reporter wire shape.

    ``kind="monitoring"`` is the watchdog's entry: a report that the
    monitoring stack itself has a blind spot (a scrape gap, an exporter down,
    an alertmanager silence). It carries no traceback, so ``message`` is what
    groups it, and ``context`` carries the specifics
    (``{"check": "...", "target": "...", "last_scrape": "..."}``).
    """

    #: No `max_length` on ANY of these, and that is the deliberate half of
    #: the 0.2.2 bounding work rather than an omission.
    #:
    #: The first cut declared the model's limits here too, and a live fleet
    #: showed within minutes why that is wrong: a reporter whose
    #: `request_path` was 4000 characters — a URL, i.e. something a CLIENT
    #: chose, not something the reporter did — had its ENTIRE BATCH refused
    #: with a 400 listing `request_path` and `release`. Every genuine alert
    #: travelling with it was lost, to protect a column that
    #: `stapel_alerts.bounds` was about to fit anyway.
    #:
    #: A reporter is not a user agent filling in a form. It is a process on a
    #: failure path handing over the only record of a defect, and the store's
    #: job is to KEEP that record, not to grade the submission. Length is the
    #: store's problem and the store solves it by truncating, once, at the
    #: boundary where the payload becomes model kwargs. Validation here is
    #: for what cannot be repaired — a batch that is not a list, an
    #: `occurrences` below 1, a `user_id` that is not a uuid.
    message = serializers.CharField(allow_blank=True, required=False, default="")
    trace = serializers.CharField(allow_blank=True, required=False, default="")
    service = serializers.CharField(required=False, allow_blank=True)
    level = serializers.CharField(required=False, default="error")
    kind = serializers.CharField(required=False, default="manual")
    environment = serializers.CharField(required=False, default="production")
    release = serializers.CharField(required=False, allow_blank=True, default="")
    context = serializers.JSONField(required=False, default=dict)
    request_path = serializers.CharField(required=False, allow_blank=True, default="")
    trace_id = serializers.CharField(required=False, allow_blank=True, default="")
    user_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    occurrences = serializers.IntegerField(required=False, min_value=1, default=1)
    occurred_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    exc_class = serializers.CharField(required=False, allow_blank=True, default="")


class ReportSerializer(serializers.Serializer):
    events = ReportEventSerializer(many=True, max_length=MAX_BATCH)


__all__ = [
    "SETTABLE_STATUSES",
    "MAX_BATCH",
    "IssueSerializer",
    "IssueDetailSerializer",
    "ErrorEventSerializer",
    "IssuePageSerializer",
    "IssuePatchSerializer",
    "IssueFixSerializer",
    "ReportEventSerializer",
    "ReportSerializer",
]
