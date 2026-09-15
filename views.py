"""The API — written for an agent first and a human second.

Owner's ruling: the tracker table is the reconciliation surface. After a fix
wave the foreman and the agents read this API, mark what they fixed, and see
what came back. That makes three things requirements rather than polish:

* **stable ids** — the issue id is a uuid that survives every occurrence,
  every sweep and every status change, so ``alerts:<id>`` in a commit message
  keeps meaning the same bug a month later;
* **ETag** — a poller that asks every minute should get a 304, not a page of
  JSON it already has;
* **JSON only** — no browsable renderer negotiation to trip over.

Authentication is two-sided: a staff session reads and writes the tracker, a
service key writes reports. A service key may NOT read the tracker — a
reporter's key lives in every container in the fleet, and the blast radius of
one leaking must not include everything the store has ever recorded.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.http import Http404
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse
from stapel_core.django.api.permissions import IsStaffUser

from .errors import (
    ERR_400_BATCH_TOO_LARGE,
    ERR_400_INVALID_REPORT,
    ERR_400_STATUS_NOT_SETTABLE,
    ERR_401_SERVICE_KEY_REQUIRED,
    ERR_403_SERVICE_KEY_INVALID,
    ERR_404_ISSUE_NOT_FOUND,
    ERR_422_REPORT_NOT_STORABLE,
)
from .models import ErrorEvent, Issue, IssueStatus, Service
from .serializers import (
    MAX_BATCH,
    SETTABLE_STATUSES,
    ErrorEventSerializer,
    IssueDetailSerializer,
    IssueFixSerializer,
    IssuePageSerializer,
    IssuePatchSerializer,
    IssueSerializer,
    ReportSerializer,
)
from .services import mark_fixed, record, set_status
from .transport import SERVICE_KEY_HEADER

#: This module's own logger. A report that cannot be stored is logged HERE
#: rather than captured, because capturing it would be the store reporting its
#: own ingest failure through its own ingest.
logger = logging.getLogger(__name__)

#: Events returned inline with an issue detail. A page of the last N, not the
#: whole history: the history is what `count` is for.
DETAIL_EVENTS = 20

#: Default page size for the issue list, and the ceiling ``?limit=`` may raise
#: it to. A ceiling rather than "any": the list is a triage surface, and an
#: agent that needs everything pages. Out-of-range values are clamped and the
#: envelope echoes the limit that was applied, so a clamp is never silent.
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class SerializerSeamMixin:
    """Overridable serializer seam (library-standard): a host subclasses a
    view and swaps a serializer without rewriting the method body."""

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


def _bounded_int(raw, *, default: int, floor: int, ceiling: int | None = None) -> int:
    """A query integer clamped into ``[floor, ceiling]``; garbage is the default."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    value = max(floor, value)
    if ceiling is not None:
        value = min(ceiling, value)
    return value


def _etag(payload) -> str:
    """A strong ETag over the rendered body.

    Over the body rather than over ``max(last_seen)``: a status change moves
    no timestamp, and a poller that kept showing "new" for an issue somebody
    had already fixed would be worse than no caching at all.
    """
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return '"%s"' % hashlib.sha256(blob).hexdigest()[:32]


def _conditional(request, payload, *, status=http_status.HTTP_200_OK):
    etag = _etag(payload)
    if request.headers.get("If-None-Match") == etag:
        response = Response(status=http_status.HTTP_304_NOT_MODIFIED)
        response["ETag"] = etag
        return response
    response = Response(payload, status=status)
    response["ETag"] = etag
    return response


class _AlertsView(SerializerSeamMixin, APIView):
    renderer_classes = [JSONRenderer]


class IssueListView(_AlertsView):
    """``GET /alerts/api/v1/issues`` — the triage list, newest activity first."""

    permission_classes = [IsStaffUser]
    response_serializer_class = IssueSerializer
    #: The envelope. Declared as the response AND used to render it, so the
    #: contract and the wire are one object (see IssuePageSerializer).
    page_serializer_class = IssuePageSerializer

    @extend_schema(
        # drf-spectacular names a GET `_list` only when the response is a
        # `many=True` serializer, and a page envelope is one object. Naming it
        # explicitly keeps the id the pair's generated client is keyed on
        # (`alerts_api_v1_issues_list`) and leaves `_retrieve` to the detail.
        operation_id="alerts_api_v1_issues_list",
        parameters=[
            OpenApiParameter("status", str, description="new|fixed|regressed|muted"),
            OpenApiParameter("level", str, description="debug|info|warning|error|fatal"),
            OpenApiParameter("service", str),
            OpenApiParameter("since", str, description="ISO-8601; last_seen >= since"),
            OpenApiParameter("open", bool, description="Only new + regressed"),
            OpenApiParameter("offset", int, description="Rows to skip; default 0"),
            OpenApiParameter(
                "limit", int,
                description=f"Page size, 1..{MAX_PAGE_SIZE}; default {PAGE_SIZE}. "
                "Out-of-range values are clamped and the envelope echoes the "
                "limit that was applied.",
            ),
        ],
        responses=IssuePageSerializer,
    )
    def get(self, request):
        qs = Issue.objects.all()
        params = request.query_params

        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("level"):
            qs = qs.filter(level=params["level"])
        if params.get("service"):
            qs = qs.filter(service=params["service"])
        if params.get("open") in ("1", "true", "True"):
            from .models import OPEN_STATUSES

            qs = qs.filter(status__in=OPEN_STATUSES)
        since = params.get("since")
        if since:
            parsed = parse_datetime(since)
            if parsed is not None:
                qs = qs.filter(last_seen__gte=parsed)

        total = qs.count()
        offset = _bounded_int(params.get("offset", 0), default=0, floor=0)
        limit = _bounded_int(
            params.get("limit", PAGE_SIZE), default=PAGE_SIZE, floor=1, ceiling=MAX_PAGE_SIZE
        )
        rows = qs.order_by("-last_seen")[offset:offset + limit]

        page = self.page_serializer_class(
            {"count": total, "offset": offset, "limit": limit, "results": rows},
            row_serializer_class=self.get_response_serializer_class() or IssueSerializer,
        )
        return _conditional(request, page.data)


class IssueDetailView(_AlertsView):
    """``GET`` one issue with its last events; ``PATCH`` its status/note."""

    permission_classes = [IsStaffUser]
    response_serializer_class = IssueDetailSerializer
    request_serializer_class = IssuePatchSerializer

    def _issue(self, issue_id) -> Issue:
        issue = Issue.objects.filter(id=issue_id).first()
        if issue is None:
            raise Http404
        return issue

    @extend_schema(responses=IssueDetailSerializer)
    def get(self, request, issue_id):
        try:
            issue = self._issue(issue_id)
        except Http404:
            return StapelErrorResponse(404, ERR_404_ISSUE_NOT_FOUND)
        events = ErrorEvent.objects.filter(issue=issue).order_by("-received_at")[:DETAIL_EVENTS]
        payload = (self.get_response_serializer_class() or IssueDetailSerializer)(issue).data
        payload["events"] = ErrorEventSerializer(events, many=True).data
        return _conditional(request, payload)

    @extend_schema(request=IssuePatchSerializer, responses=IssueSerializer)
    def patch(self, request, issue_id):
        try:
            issue = self._issue(issue_id)
        except Http404:
            return StapelErrorResponse(404, ERR_404_ISSUE_NOT_FOUND)

        serializer = (self.get_request_serializer_class() or IssuePatchSerializer)(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        new_status = data.get("status")
        if new_status is None and "muted_until" in data:
            # A deadline alone is a mute: the field means nothing in any other
            # status, and 0.2.0's 200-that-wrote-nothing was a lie.
            new_status = IssueStatus.MUTED
        if new_status is not None and new_status not in [s.value for s in SETTABLE_STATUSES]:
            return StapelErrorResponse(
                400, ERR_400_STATUS_NOT_SETTABLE, {"status": new_status}
            )

        if new_status is not None:
            set_status(
                issue,
                new_status,
                note=data.get("note"),
                muted_until=data.get("muted_until"),
            )
        elif "note" in data:
            issue.note = data["note"]
            issue.save(update_fields=["note"])

        issue.refresh_from_db()
        return Response(IssueSerializer(issue).data)


class IssueFixView(_AlertsView):
    """``POST /issues/{id}/fix`` — close an issue with the release that fixed it.

    The endpoint CI calls on a commit that references ``alerts:<issue-id>``,
    and the one an agent calls after a fix wave. Idempotent: fixing a fixed
    issue overwrites the version, which is what a re-deploy of a better fix
    should do.
    """

    permission_classes = [IsStaffUser]
    request_serializer_class = IssueFixSerializer
    response_serializer_class = IssueSerializer

    @extend_schema(request=IssueFixSerializer, responses=IssueSerializer)
    def post(self, request, issue_id):
        issue = Issue.objects.filter(id=issue_id).first()
        if issue is None:
            return StapelErrorResponse(404, ERR_404_ISSUE_NOT_FOUND)
        serializer = (self.get_request_serializer_class() or IssueFixSerializer)(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        mark_fixed(
            issue,
            version=serializer.validated_data.get("version", ""),
            sha=serializer.validated_data.get("sha", ""),
        )
        issue.refresh_from_db()
        return Response(IssueSerializer(issue).data)


class ReportView(_AlertsView):
    """``POST /alerts/api/v1/report`` — a batch of events from a reporter.

    ``AllowAny`` at the DRF layer and authenticated HERE, by service key,
    because a reporter has no session and no JWT: it is a process, not a
    person. The key is looked up by hash (``Service.authenticate``), so the
    plaintext never reaches a query log.

    A staff session is also accepted, which is what makes the endpoint
    testable and lets an operator replay an event by hand.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    request_serializer_class = ReportSerializer

    @extend_schema(request=ReportSerializer, responses={202: None})
    def post(self, request):
        service = self._authenticate(request)
        if isinstance(service, Response):
            return service

        events = (request.data or {}).get("events")
        if not isinstance(events, list):
            return StapelErrorResponse(400, ERR_400_INVALID_REPORT)
        if len(events) > MAX_BATCH:
            return StapelErrorResponse(400, ERR_400_BATCH_TOO_LARGE, {"max": MAX_BATCH})

        serializer = (self.get_request_serializer_class() or ReportSerializer)(
            data={"events": events}
        )
        if not serializer.is_valid():
            return StapelErrorResponse(
                400, ERR_400_INVALID_REPORT, {"detail": serializer.errors}
            )

        accepted = 0
        ignored = 0
        unstorable = 0
        for item in serializer.validated_data["events"]:
            payload = dict(item)
            # The key names the service. A reporter that claims to be another
            # service in the body does not get to: the key is the identity,
            # the body is a claim.
            if service is not None:
                payload["service"] = service.name
            # A report that cannot be stored must never become a 500. This
            # endpoint's clients are every other service in the fleet, and a
            # 500 here turns each of them into a retrying, logging, noisy
            # client — about the alert store, which is the one component whose
            # own noise nothing is left to record. `record` bounds every field
            # it writes (stapel_alerts.bounds), so reaching this branch means
            # something the ingest did not anticipate; it is logged LOCALLY,
            # on this module's own logger, which the capture handler excludes.
            try:
                if record(**_record_kwargs(payload)) is None:
                    ignored += 1
                else:
                    accepted += 1
            except Exception:
                unstorable += 1
                logger.error(
                    "alerts: refusing a report event that could not be stored "
                    "(service=%r, level=%r)",
                    payload.get("service"), payload.get("level"), exc_info=True,
                )

        if unstorable and not accepted:
            # Nothing in the batch survived: the reporter is sending something
            # this store cannot hold and should be told so, in a shape it can
            # act on, rather than being handed a 500 to retry for ever.
            return StapelErrorResponse(
                422, ERR_422_REPORT_NOT_STORABLE, {"events": unstorable}
            )

        if service is not None:
            from django.utils import timezone

            Service.objects.filter(pk=service.pk).update(last_report_at=timezone.now())

        return Response(
            {"accepted": accepted, "ignored": ignored, "unstorable": unstorable},
            status=http_status.HTTP_202_ACCEPTED,
        )

    def _authenticate(self, request):
        """A valid service key, a staff session, or an error response."""
        raw = request.headers.get(SERVICE_KEY_HEADER, "")
        if raw:
            service = Service.authenticate(raw)
            if service is None:
                return StapelErrorResponse(403, ERR_403_SERVICE_KEY_INVALID)
            return service
        user = getattr(request, "user", None)
        if getattr(user, "is_staff", False):
            return None
        return StapelErrorResponse(401, ERR_401_SERVICE_KEY_REQUIRED)


def _record_kwargs(payload: dict) -> dict:
    return {
        "trace": payload.get("trace") or "",
        "message": payload.get("message") or "",
        "service": payload.get("service") or "unknown",
        "level": payload.get("level") or "error",
        "kind": payload.get("kind") or "manual",
        "environment": payload.get("environment") or "production",
        "release": payload.get("release") or "",
        "context": payload.get("context") or {},
        "request_path": payload.get("request_path") or "",
        "trace_id": payload.get("trace_id") or "",
        "user_id": payload.get("user_id") or None,
        "occurrences": int(payload.get("occurrences") or 1),
        "occurred_at": payload.get("occurred_at"),
        "exc_class": payload.get("exc_class") or "",
    }


__all__ = [
    "IssueListView",
    "IssueDetailView",
    "IssueFixView",
    "ReportView",
    "SerializerSeamMixin",
    "DETAIL_EVENTS",
    "PAGE_SIZE",
    "MAX_PAGE_SIZE",
]
