"""Who gets told, and when the store keeps quiet."""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_alerts.models import ErrorEvent, Issue, IssueStatus
from stapel_alerts.notify import is_spiking, notify_issue
from stapel_alerts.services import mark_fixed, record, set_status

from .traces import FK_VIOLATION_A, FK_VIOLATION_B

pytestmark = pytest.mark.django_db


def _issue_and_event(trace=FK_VIOLATION_A):
    event = record(trace=trace, service="svc-recordings")
    return event.issue, event


def test_a_new_issue_notifies():
    issue, event = _issue_and_event()
    assert notify_issue(issue, event, created=True, regressed=False) == "new issue"


def test_a_regression_notifies():
    issue, event = _issue_and_event()
    assert notify_issue(issue, event, created=False, regressed=True) == "regressed"


def test_a_repeat_occurrence_of_a_known_issue_notifies_nobody():
    """An alert store that emails on every occurrence is a mailing list people
    filter — and then the new issue that mattered lands in the filtered folder."""
    issue, event = _issue_and_event()
    assert notify_issue(issue, event, created=False, regressed=False) == ""


def test_a_muted_issue_does_not_notify():
    issue, event = _issue_and_event()
    set_status(issue, IssueStatus.MUTED)
    issue.refresh_from_db()

    assert notify_issue(issue, event, created=True, regressed=False) == ""


def test_an_expired_mute_notifies_again():
    issue, event = _issue_and_event()
    set_status(issue, IssueStatus.MUTED, muted_until=timezone.now() - timedelta(minutes=1))
    issue.refresh_from_db()

    assert notify_issue(issue, event, created=True, regressed=False) == "new issue"


def test_quiet_hours_hold_an_error_and_let_a_fatal_through(settings):
    now_hour = timezone.now().hour
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-test",
        "RATE_LIMIT": 1000,
        # A window that certainly contains the current hour.
        "QUIET_HOURS": {"start": now_hour, "end": (now_hour + 1) % 24},
    }
    issue, event = _issue_and_event()

    assert notify_issue(issue, event, created=True, regressed=False) == ""

    issue.level = "fatal"
    assert notify_issue(issue, event, created=True, regressed=False) == "new issue"


def test_quiet_hours_off_by_default(settings):
    """A deployment that has not said when its people sleep must not have
    alerts withheld on a guess."""
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "RATE_LIMIT": 1000}
    issue, event = _issue_and_event()

    assert notify_issue(issue, event, created=True, regressed=False) == "new issue"


def test_a_wrapping_quiet_window_is_understood(settings):
    from stapel_alerts.notify import _in_quiet_hours

    settings.STAPEL_ALERTS = {"QUIET_HOURS": {"start": 23, "end": 8}}
    midnight = timezone.now().replace(hour=2, minute=0)
    noon = timezone.now().replace(hour=12, minute=0)

    assert _in_quiet_hours(midnight) is True
    assert _in_quiet_hours(noon) is False


def test_a_spike_notifies(settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-test", "RATE_LIMIT": 1000,
        "SPIKE_MIN_COUNT": 5, "SPIKE_FACTOR": 3,
    }
    issue, event = _issue_and_event()
    # Two occurrences in the previous hour...
    ErrorEvent.objects.filter(pk=event.pk).update(
        received_at=timezone.now() - timedelta(minutes=90), occurrences=2
    )
    # ...and twenty in this one.
    record(trace=FK_VIOLATION_B, service="svc-recordings", occurrences=20)
    issue.refresh_from_db()

    assert is_spiking(issue) is True


def test_a_small_number_is_not_a_spike(settings):
    """1 → 5 is a factor of five and means nothing."""
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-test", "RATE_LIMIT": 1000,
        "SPIKE_MIN_COUNT": 20, "SPIKE_FACTOR": 5,
    }
    issue, event = _issue_and_event()
    record(trace=FK_VIOLATION_B, service="svc-recordings", occurrences=5)
    issue.refresh_from_db()

    assert is_spiking(issue) is False


def test_the_notification_body_carries_what_an_agent_needs(settings, sent_fallback):
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "RATE_LIMIT": 1000}
    issue, event = _issue_and_event()

    notify_issue(issue, event, created=True, regressed=False)

    subject, body = sent_fallback[-1]
    assert "svc-recordings" in subject
    assert str(issue.id) in body
    assert issue.fingerprint in body


def test_a_new_issue_recorded_through_the_store_reaches_the_channel(
    settings, sent_fallback, django_capture_on_commit_callbacks
):
    """End to end: nothing calls notify_issue by hand in production.

    Notification runs in ``transaction.on_commit`` on purpose — an alert that
    was rolled back must not have been emailed about — so the test has to run
    the callbacks the way a commit would.
    """
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "RATE_LIMIT": 1000}

    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-recordings")

    assert len(sent_fallback) == 1

    # And a second occurrence of the SAME issue does not.
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_B, service="svc-recordings")

    assert len(sent_fallback) == 1


def test_a_regression_recorded_through_the_store_reaches_the_channel(
    settings, sent_fallback, django_capture_on_commit_callbacks
):
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "RATE_LIMIT": 1000}
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-recordings")
    issue = Issue.objects.get()
    mark_fixed(issue, version="1.0.0")
    sent_fallback.clear()

    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_B, service="svc-recordings")

    assert len(sent_fallback) == 1
    assert "regressed" in sent_fallback[0][0]


def test_a_rolled_back_capture_notifies_nobody(
    settings, sent_fallback, django_capture_on_commit_callbacks
):
    """The hooks hang off the commit, so an alert that was never committed is
    an alert nobody was woken for."""
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "RATE_LIMIT": 1000}

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        record(trace=FK_VIOLATION_A, service="svc-recordings")

    assert len(callbacks) == 1
    assert sent_fallback == []
