"""The API an agent uses to read the tracker, fix, and close.

Includes the two walls that decide the blast radius of a leak: the tracker is
staff-only, and a reporter's service key writes reports and nothing else.
"""
import uuid
from urllib.parse import quote

import pytest
from django.urls import reverse
from django.utils import timezone

from stapel_alerts.models import ErrorEvent, Issue, IssueStatus, Service
from stapel_alerts.services import record
from stapel_alerts.views import MAX_PAGE_SIZE, PAGE_SIZE

from .traces import FK_VIOLATION_A, FK_VIOLATION_B, JWT_REFUSAL_A

pytestmark = pytest.mark.django_db


ISSUES = "/alerts/api/v1/issues"
REPORT = "/alerts/api/v1/report"


@pytest.fixture
def service(db):
    svc, key = Service.create_with_key("svc-billing")
    return svc, key


def _an_issue(trace=FK_VIOLATION_A, **kwargs):
    kwargs.setdefault("service", "svc-recordings")
    record(trace=trace, **kwargs)
    return Issue.objects.order_by("-last_seen").first()


# ── The envelope and the walls ──────────────────────────────────────────


def test_the_tracker_is_staff_only(api_client, plain_user):
    api_client.force_authenticate(user=plain_user)
    assert api_client.get(ISSUES).status_code == 403


def test_an_anonymous_caller_cannot_read_the_tracker(api_client):
    assert api_client.get(ISSUES).status_code in (401, 403)


def test_a_service_key_may_write_reports_and_not_read_the_tracker(api_client, service):
    """A reporter's key lives in every container in the fleet. The blast radius
    of one leaking must not include everything the store has recorded."""
    svc, key = service

    reported = api_client.post(
        REPORT, {"events": [{"message": "x"}]}, format="json", HTTP_X_SERVICE_KEY=key
    )
    assert reported.status_code == 202

    read = api_client.get(ISSUES, HTTP_X_SERVICE_KEY=key)
    assert read.status_code in (401, 403)


def test_an_error_is_answered_in_the_fleet_envelope(staff_client):
    response = staff_client.get(f"{ISSUES}/{uuid.uuid4()}")

    assert response.status_code == 404
    body = response.json()
    assert body["localizable_error"] == "error.404.alerts_issue_not_found"
    assert "error" in body


def test_the_api_speaks_json_only(staff_client):
    _an_issue()
    response = staff_client.get(ISSUES, HTTP_ACCEPT="text/html")
    assert response.status_code in (200, 406)
    if response.status_code == 200:
        assert response["Content-Type"].startswith("application/json")


# ── The list ────────────────────────────────────────────────────────────


def test_the_list_is_ordered_by_last_activity(staff_client):
    old = _an_issue(FK_VIOLATION_A, service="svc-a")
    new = _an_issue(JWT_REFUSAL_A, service="svc-b")
    Issue.objects.filter(pk=old.pk).update(last_seen=timezone.now() - timezone.timedelta(days=1))

    rows = staff_client.get(ISSUES).json()["results"]

    assert [r["id"] for r in rows] == [str(new.id), str(old.id)]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("?service=svc-a", 1),
        ("?service=svc-b", 1),
        ("?level=fatal", 1),
        ("?level=error", 1),
        ("?status=new", 2),
        ("?status=fixed", 0),
    ],
)
def test_the_list_filters(staff_client, query, expected):
    _an_issue(FK_VIOLATION_A, service="svc-a", level="error")
    _an_issue(JWT_REFUSAL_A, service="svc-b", level="fatal")

    assert staff_client.get(ISSUES + query).json()["count"] == expected


def test_since_filters_on_last_seen(staff_client):
    old = _an_issue(FK_VIOLATION_A, service="svc-a")
    cutoff = timezone.now()
    Issue.objects.filter(pk=old.pk).update(last_seen=cutoff - timezone.timedelta(hours=2))
    _an_issue(JWT_REFUSAL_A, service="svc-b")

    # quote(): an unencoded "+06:00" offset arrives as a space and the filter
    # is silently skipped — a green test that proved nothing.
    body = staff_client.get(f"{ISSUES}?since={quote(cutoff.isoformat())}").json()

    assert body["count"] == 1


def test_an_unparseable_since_does_not_silently_return_everything(staff_client):
    """A filter that cannot be parsed must not read as "no filter" without the
    caller being able to tell: the list is still ordered and bounded, and the
    ETag differs from the filtered one, so a poller notices."""
    _an_issue(FK_VIOLATION_A, service="svc-a")

    assert staff_client.get(f"{ISSUES}?since=yesterday").status_code == 200


def test_open_returns_new_and_regressed_only(staff_client):
    a = _an_issue(FK_VIOLATION_A, service="svc-a")
    Issue.objects.filter(pk=a.pk).update(status=IssueStatus.FIXED)
    _an_issue(JWT_REFUSAL_A, service="svc-b")

    assert staff_client.get(f"{ISSUES}?open=1").json()["count"] == 1



def test_the_list_is_a_page_envelope_with_a_default_limit(staff_client):
    """The pair reads `{count, offset, limit, results}`; the envelope is the
    wire, and the schema now says so (IssuePage)."""
    _an_issue()

    body = staff_client.get(ISSUES).json()

    assert set(body) == {"count", "offset", "limit", "results"}
    assert body["limit"] == PAGE_SIZE
    assert body["offset"] == 0


def test_a_caller_may_choose_the_page_size(staff_client):
    for i in range(3):
        _an_issue(f"Traceback (most recent call last):\n  File \"a.py\", line {i}\nBug{i}: x", service=f"svc-{i}")

    body = staff_client.get(f"{ISSUES}?limit=2").json()

    assert body["count"] == 3
    assert body["limit"] == 2
    assert len(body["results"]) == 2

    second = staff_client.get(f"{ISSUES}?limit=2&offset=2").json()
    assert second["offset"] == 2
    assert len(second["results"]) == 1


@pytest.mark.parametrize("raw,effective", [
    ("100000", MAX_PAGE_SIZE),
    ("0", 1),
    ("-5", 1),
    ("many", PAGE_SIZE),
])
def test_the_page_size_has_a_ceiling_and_the_envelope_echoes_what_was_applied(
    staff_client, raw, effective
):
    """A clamped limit is not a lie because the envelope says what was used."""
    _an_issue()

    assert staff_client.get(f"{ISSUES}?limit={raw}").json()["limit"] == effective


def test_a_repeat_poll_gets_a_304(staff_client):
    _an_issue()
    first = staff_client.get(ISSUES)
    etag = first["ETag"]

    second = staff_client.get(ISSUES, HTTP_IF_NONE_MATCH=etag)

    assert second.status_code == 304


def test_the_etag_changes_when_a_status_changes(staff_client):
    issue = _an_issue()
    etag = staff_client.get(ISSUES)["ETag"]

    Issue.objects.filter(pk=issue.pk).update(status=IssueStatus.FIXED)

    assert staff_client.get(ISSUES)["ETag"] != etag


# ── The detail ──────────────────────────────────────────────────────────


def test_the_detail_carries_the_last_events(staff_client):
    issue = _an_issue(FK_VIOLATION_A)
    record(trace=FK_VIOLATION_B, service="svc-recordings")

    body = staff_client.get(f"{ISSUES}/{issue.id}").json()

    assert body["count"] == 2
    assert len(body["events"]) == 2
    assert body["events"][0]["trace"]


# ── PATCH ───────────────────────────────────────────────────────────────


def test_a_status_and_a_note_can_be_set(staff_client):
    issue = _an_issue()

    response = staff_client.patch(
        f"{ISSUES}/{issue.id}", {"status": "muted", "note": "known, waiting on upstream"},
        format="json",
    )

    assert response.status_code == 200
    issue.refresh_from_db()
    assert issue.status == IssueStatus.MUTED
    assert issue.note == "known, waiting on upstream"



def test_a_mute_deadline_alone_mutes_the_issue(staff_client):
    """`muted_until` has no meaning in any other status, so a patch that
    carries only the deadline IS a mute. 0.2.0 answered 200 and wrote
    nothing — a 200 that changes nothing is a lie."""
    issue = _an_issue()
    until = timezone.now() + timezone.timedelta(hours=2)

    response = staff_client.patch(
        f"{ISSUES}/{issue.id}", {"muted_until": until.isoformat()}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "muted"
    issue.refresh_from_db()
    assert issue.status == IssueStatus.MUTED
    assert issue.muted_until == until


def test_reopening_a_muted_issue_drops_the_deadline(staff_client):
    issue = _an_issue()
    until = timezone.now() + timezone.timedelta(hours=2)
    staff_client.patch(f"{ISSUES}/{issue.id}", {"muted_until": until.isoformat()}, format="json")

    body = staff_client.patch(f"{ISSUES}/{issue.id}", {"status": "new"}, format="json").json()

    assert body["status"] == "new"
    assert body["muted_until"] is None


def test_regressed_cannot_be_asserted_by_a_caller(staff_client):
    """It is the store's verdict on evidence. A caller who could set it could
    also decline to, and hide a regression."""
    issue = _an_issue()

    response = staff_client.patch(
        f"{ISSUES}/{issue.id}", {"status": "regressed"}, format="json"
    )

    assert response.status_code == 400
    issue.refresh_from_db()
    assert issue.status == IssueStatus.NEW


# ── Close by fix, and the regression flip through the API ───────────────


def test_an_agent_closes_an_issue_with_the_release_that_fixed_it(staff_client):
    issue = _an_issue()

    response = staff_client.post(
        f"{ISSUES}/{issue.id}/fix", {"version": "0.68.1", "sha": "33e94e6"}, format="json"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "fixed"
    assert body["fixed_in_version"] == "0.68.1"
    assert body["fixed_in_sha"] == "33e94e6"
    assert body["fixed_at"]


def test_an_event_after_the_fix_shows_up_as_regressed_in_the_api(staff_client):
    issue = _an_issue(FK_VIOLATION_A)
    staff_client.post(f"{ISSUES}/{issue.id}/fix", {"version": "1.0.0"}, format="json")

    record(trace=FK_VIOLATION_B, service="svc-recordings")

    body = staff_client.get(f"{ISSUES}/{issue.id}").json()
    assert body["status"] == "regressed"
    assert body["regressed_at"]
    assert body["count_since_fix"] == 1


def test_fixing_an_unknown_issue_is_a_404(staff_client):
    response = staff_client.post(f"{ISSUES}/{uuid.uuid4()}/fix", {}, format="json")
    assert response.status_code == 404


# ── POST /report ────────────────────────────────────────────────────────


def test_a_report_without_a_key_is_refused(api_client):
    response = api_client.post(REPORT, {"events": [{"message": "x"}]}, format="json")

    assert response.status_code == 401
    assert response.json()["localizable_error"] == "error.401.alerts_service_key_required"


def test_a_bad_key_is_refused(api_client):
    response = api_client.post(
        REPORT, {"events": [{"message": "x"}]}, format="json", HTTP_X_SERVICE_KEY="not-a-key"
    )

    assert response.status_code == 403
    assert response.json()["localizable_error"] == "error.403.alerts_service_key_invalid"


def test_an_inactive_service_key_stops_working(api_client, service):
    svc, key = service
    Service.objects.filter(pk=svc.pk).update(is_active=False)

    response = api_client.post(
        REPORT, {"events": [{"message": "x"}]}, format="json", HTTP_X_SERVICE_KEY=key
    )

    assert response.status_code == 403


def test_the_plaintext_key_is_not_stored(service):
    svc, key = service
    svc.refresh_from_db()
    assert key not in svc.key_hash
    assert len(svc.key_hash) == 64


def test_a_batch_is_stored_and_grouped(api_client, service):
    svc, key = service

    response = api_client.post(
        REPORT,
        {"events": [
            {"trace": FK_VIOLATION_A, "level": "error"},
            {"trace": FK_VIOLATION_B, "level": "error"},
        ]},
        format="json",
        HTTP_X_SERVICE_KEY=key,
    )

    assert response.status_code == 202
    assert response.json()["accepted"] == 2
    assert Issue.objects.count() == 1
    assert Issue.objects.get().service == "svc-billing"


def test_the_key_names_the_service_not_the_body(api_client, service):
    """The key is the identity; the body is a claim."""
    svc, key = service

    api_client.post(
        REPORT,
        {"events": [{"message": "x", "service": "svc-somebody-else"}]},
        format="json",
        HTTP_X_SERVICE_KEY=key,
    )

    assert Issue.objects.get().service == "svc-billing"


def test_a_monitoring_event_is_accepted_as_an_issue(api_client, service):
    """The watchdog's input path: a blind monitoring stack is itself an issue
    in the tracker, not a thing only Telegram hears about."""
    svc, key = service

    response = api_client.post(
        REPORT,
        {"events": [{
            "message": "prometheus has not scraped svc-billing for 15m",
            "kind": "monitoring",
            "level": "fatal",
            "context": {"check": "scrape_gap", "target": "svc-billing", "gap_seconds": 900},
        }]},
        format="json",
        HTTP_X_SERVICE_KEY=key,
    )

    assert response.status_code == 202
    issue = Issue.objects.get()
    assert issue.kind == "monitoring"
    assert issue.level == "fatal"
    assert ErrorEvent.objects.get().context["check"] == "scrape_gap"


def test_an_oversized_batch_is_refused(api_client, service):
    svc, key = service

    response = api_client.post(
        REPORT,
        {"events": [{"message": f"x{i}"} for i in range(200)]},
        format="json",
        HTTP_X_SERVICE_KEY=key,
    )

    assert response.status_code == 400
    assert response.json()["localizable_error"] == "error.400.alerts_batch_too_large"


def test_a_malformed_report_is_refused(api_client, service):
    svc, key = service

    response = api_client.post(
        REPORT, {"events": "not a list"}, format="json", HTTP_X_SERVICE_KEY=key
    )

    assert response.status_code == 400
    assert response.json()["localizable_error"] == "error.400.alerts_invalid_report"


def test_reporting_stamps_the_service_last_seen(api_client, service):
    svc, key = service
    assert svc.last_report_at is None

    api_client.post(REPORT, {"events": [{"message": "x"}]}, format="json", HTTP_X_SERVICE_KEY=key)

    svc.refresh_from_db()
    assert svc.last_report_at is not None


def test_a_staff_session_may_also_report(staff_client):
    response = staff_client.post(REPORT, {"events": [{"message": "by hand"}]}, format="json")

    assert response.status_code == 202


def test_rotating_a_key_invalidates_the_old_one(api_client, service):
    svc, old = service
    new = svc.rotate_key()

    assert api_client.post(
        REPORT, {"events": [{"message": "x"}]}, format="json", HTTP_X_SERVICE_KEY=old
    ).status_code == 403
    assert api_client.post(
        REPORT, {"events": [{"message": "x"}]}, format="json", HTTP_X_SERVICE_KEY=new
    ).status_code == 202


def test_urls_resolve_by_name():
    assert reverse("alerts-issues") == ISSUES
    assert reverse("alerts-report") == REPORT
