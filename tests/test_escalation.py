"""The channel is an escalation path, not a mirror of the store.

Owner's ruling, 2026-09-15, after watching the first hour of the tracker in
the channel: announcing every first-seen issue — warnings included — makes
the channel a duplicate of the thing it is supposed to escalate, and a
channel that repeats the store is one people mute.
"""
import pytest

from stapel_alerts.models import Issue
from stapel_alerts.notify import notify_issue

pytestmark = pytest.mark.django_db


def _issue(level="error", **kwargs):
    return Issue.objects.create(
        fingerprint=kwargs.pop("fingerprint", "f" * 64),
        service="iron-cdn",
        level=level,
        title="something",
        **kwargs,
    )


@pytest.fixture
def sent(monkeypatch):
    """Everything the fallback seam was asked to deliver."""
    delivered = []
    monkeypatch.setattr(
        "stapel_alerts.fallback.notify",
        lambda subject, body: delivered.append(subject) or True,
    )
    return delivered


def test_a_new_error_reaches_the_channel(sent):
    issue = _issue(level="error")
    assert notify_issue(issue, None, created=True, regressed=False) == "new issue"
    assert len(sent) == 1


def test_a_new_warning_does_not(sent):
    """Warnings live in the store and are read there."""
    issue = _issue(level="warning")
    assert notify_issue(issue, None, created=True, regressed=False) == ""
    assert sent == []


def test_a_regressed_warning_does_not_either(sent):
    """A warning that came back is still a warning."""
    issue = _issue(level="warning")
    assert notify_issue(issue, None, created=False, regressed=True) == ""
    assert sent == []


def test_a_regression_at_error_does(sent):
    issue = _issue(level="error")
    assert notify_issue(issue, None, created=False, regressed=True) == "regressed"
    assert len(sent) == 1


def test_a_repeat_of_a_known_issue_reaches_nobody(sent):
    issue = _issue(level="error")
    assert notify_issue(issue, None, created=False, regressed=False) == ""
    assert sent == []


def test_the_floor_is_configuration_not_a_constant(sent, settings):
    settings.STAPEL_ALERTS = {"NOTIFY_MIN_LEVEL": "warning"}
    issue = _issue(level="warning")
    assert notify_issue(issue, None, created=True, regressed=False) == "new issue"
    assert len(sent) == 1


def test_each_threshold_can_be_switched_off(sent, settings):
    settings.STAPEL_ALERTS = {"NOTIFY_ON_NEW": False}
    issue = _issue(level="fatal")
    assert notify_issue(issue, None, created=True, regressed=False) == ""
    assert sent == []


def test_a_fatal_is_never_held_back_by_the_default_floor(sent):
    issue = _issue(level="fatal")
    assert notify_issue(issue, None, created=True, regressed=False) == "new issue"
    assert len(sent) == 1
