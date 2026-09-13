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


class IssuePatchSerializer(serializers.Serializer):
    """``PATCH /issues/{id}`` — status and/or note."""

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

    message = serializers.CharField(allow_blank=True, required=False, default="")
    trace = serializers.CharField(allow_blank=True, required=False, default="")
    service = serializers.CharField(max_length=64, required=False, allow_blank=True)
    level = serializers.CharField(max_length=16, required=False, default="error")
    kind = serializers.CharField(max_length=16, required=False, default="manual")
    environment = serializers.CharField(max_length=32, required=False, default="production")
    release = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    context = serializers.JSONField(required=False, default=dict)
    request_path = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")
    trace_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    user_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    occurrences = serializers.IntegerField(required=False, min_value=1, default=1)
    occurred_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    exc_class = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")


class ReportSerializer(serializers.Serializer):
    events = ReportEventSerializer(many=True, max_length=MAX_BATCH)


__all__ = [
    "SETTABLE_STATUSES",
    "MAX_BATCH",
    "IssueSerializer",
    "IssueDetailSerializer",
    "ErrorEventSerializer",
    "IssuePatchSerializer",
    "IssueFixSerializer",
    "ReportEventSerializer",
    "ReportSerializer",
]
