"""The tracker: grouping on ingest, the regression flip, counters, retention."""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_alerts.models import ErrorEvent, Issue, IssueStatus
from stapel_alerts.services import erase_subject, mark_fixed, record, redact, set_status

from .traces import (
    FK_VIOLATION_A,
    FK_VIOLATION_B,
    FK_VIOLATION_C,
    FK_VIOLATION_OTHER_CONSTRAINT,
    JWT_REFUSAL_A,
    JWT_REFUSAL_B,
)

pytestmark = pytest.mark.django_db


def _record(trace, **kwargs):
    kwargs.setdefault("service", "svc-recordings")
    return record(trace=trace, **kwargs)


# ── Grouping, through the store ─────────────────────────────────────────


def test_three_occurrences_of_one_bug_are_one_issue_with_a_count_of_three():
    for trace in (FK_VIOLATION_A, FK_VIOLATION_B, FK_VIOLATION_C):
        _record(trace)

    assert Issue.objects.count() == 1
    issue = Issue.objects.get()
    assert issue.count == 3
    assert ErrorEvent.objects.count() == 3


def test_a_different_constraint_opens_a_second_issue():
    _record(FK_VIOLATION_A)
    _record(FK_VIOLATION_OTHER_CONSTRAINT)

    assert Issue.objects.count() == 2


def test_two_stale_cookies_are_one_issue():
    _record(JWT_REFUSAL_A, service="svc-billing")
    _record(JWT_REFUSAL_B, service="svc-billing")

    assert Issue.objects.filter(service="svc-billing").count() == 1


def test_the_same_bug_in_two_services_is_two_issues():
    """They are fixed, deployed and closed separately."""
    _record(FK_VIOLATION_A, service="svc-a")
    _record(FK_VIOLATION_A, service="svc-b")

    assert Issue.objects.count() == 2


def test_a_batched_event_moves_the_count_by_its_occurrences():
    """A rate-limited reporter sends one row for a window and says how many it
    swallowed — the issue's count must stay true."""
    _record(FK_VIOLATION_A, occurrences=17)

    assert Issue.objects.get().count == 17
    assert ErrorEvent.objects.get().occurrences == 17


def test_first_and_last_seen_bracket_the_occurrences():
    early = timezone.now() - timedelta(hours=3)
    late = timezone.now()
    _record(FK_VIOLATION_A, occurred_at=late)
    _record(FK_VIOLATION_B, occurred_at=early)

    issue = Issue.objects.get()
    assert issue.first_seen == early
    assert issue.last_seen == late


def test_an_issue_takes_the_worst_level_it_has_been_seen_at():
    _record(FK_VIOLATION_A, level="warning")
    _record(FK_VIOLATION_B, level="fatal")
    _record(FK_VIOLATION_C, level="info")

    assert Issue.objects.get().level == "fatal"


# ── The regression flip ─────────────────────────────────────────────────


def test_a_fixed_issue_that_happens_again_regresses():
    _record(FK_VIOLATION_A)
    issue = Issue.objects.get()
    mark_fixed(issue, version="1.4.2", sha="deadbeef")
    issue.refresh_from_db()
    assert issue.status == IssueStatus.FIXED
    assert issue.count_since_fix == 0

    _record(FK_VIOLATION_B)

    issue.refresh_from_db()
    assert issue.status == IssueStatus.REGRESSED
    assert issue.regressed_at is not None
    assert issue.count_since_fix == 1
    # The fixing release stays on the row: "1.4.2 was supposed to fix this"
    # is the first question asked when a bug comes back.
    assert issue.fixed_in_version == "1.4.2"
    assert issue.fixed_in_sha == "deadbeef"


def test_a_new_issue_does_not_start_regressed():
    _record(FK_VIOLATION_A)
    issue = Issue.objects.get()
    assert issue.status == IssueStatus.NEW
    assert issue.regressed_at is None


def test_fixing_a_regressed_issue_clears_the_regression():
    _record(FK_VIOLATION_A)
    issue = Issue.objects.get()
    mark_fixed(issue, version="1.0.0")
    _record(FK_VIOLATION_B)
    issue.refresh_from_db()
    assert issue.status == IssueStatus.REGRESSED

    mark_fixed(issue, version="1.0.1")

    issue.refresh_from_db()
    assert issue.status == IssueStatus.FIXED
    assert issue.regressed_at is None
    assert issue.fixed_in_version == "1.0.1"


def test_an_expired_mute_is_over_on_the_next_event():
    _record(FK_VIOLATION_A)
    issue = Issue.objects.get()
    set_status(issue, IssueStatus.MUTED, muted_until=timezone.now() - timedelta(minutes=1))

    _record(FK_VIOLATION_B)

    issue.refresh_from_db()
    assert issue.status == IssueStatus.NEW
    assert issue.muted_until is None


def test_a_live_mute_survives_a_new_event():
    _record(FK_VIOLATION_A)
    issue = Issue.objects.get()
    set_status(issue, IssueStatus.MUTED, muted_until=timezone.now() + timedelta(hours=1))

    _record(FK_VIOLATION_B)

    issue.refresh_from_db()
    assert issue.status == IssueStatus.MUTED
    # A muted issue still COUNTS — muting silences the notification, not the
    # measurement.
    assert issue.count == 2


# ── Events are capped; counters are not ─────────────────────────────────


def test_events_are_capped_per_issue_and_the_count_survives(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "RATE_LIMIT": 1000, "EVENTS_PER_ISSUE": 3}
    for _ in range(10):
        _record(FK_VIOLATION_A)

    issue = Issue.objects.get()
    assert ErrorEvent.objects.filter(issue=issue).count() == 3
    assert issue.count == 10


# ── Redaction ───────────────────────────────────────────────────────────


def test_a_secret_in_the_context_is_not_stored():
    _record(
        FK_VIOLATION_A,
        context={
            "headers": {"authorization": "Bearer abc.def", "accept": "application/json"},
            "password": "hunter2",
            "safe": "keep me",
        },
    )

    stored = ErrorEvent.objects.get().context
    assert stored["password"] == "***"
    assert stored["headers"]["authorization"] == "***"
    assert stored["headers"]["accept"] == "application/json"
    assert stored["safe"] == "keep me"


def test_redact_walks_a_list_of_dicts():
    assert redact({"items": [{"token": "x", "id": 1}]}) == {
        "items": [{"token": "***", "id": 1}]
    }


# ── GDPR ────────────────────────────────────────────────────────────────


def test_erasing_a_subject_drops_the_link_and_keeps_the_failure():
    user_id = uuid.uuid4()
    _record(FK_VIOLATION_A, user_id=user_id, context={"actor": str(user_id), "path": "/x"})

    assert erase_subject(user_id) == 1

    event = ErrorEvent.objects.get()
    assert event.user_id is None
    assert event.context["actor"] == "<erased>"
    # The failure itself is still on the record.
    assert event.trace
    assert Issue.objects.count() == 1
