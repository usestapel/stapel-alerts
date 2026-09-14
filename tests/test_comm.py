"""The two facts this module puts on the bus, and the one it refuses to.

A host subscribes to ``alerts.issue.opened`` to page, open a ticket, or post
to a channel. The contract it subscribes against is ``schemas/emits/*.json``,
validated on every emit, so a payload that drifts from the schema fails here
rather than in the host's handler at 3am.
"""
import json
from pathlib import Path

import pytest

from stapel_alerts.models import Issue, IssueStatus
from stapel_alerts.services import (
    EVENT_ISSUE_OPENED,
    EVENT_ISSUE_REGRESSED,
    mark_fixed,
    record,
)

from .traces import FK_VIOLATION_A, FK_VIOLATION_OTHER_CONSTRAINT, JWT_REFUSAL_A

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


# ── The schemas are shipped and are the ones registered ─────────────────


def test_both_emit_schemas_are_committed():
    for name in (EVENT_ISSUE_OPENED, EVENT_ISSUE_REGRESSED):
        path = ROOT / "schemas" / "emits" / f"{name}.json"
        assert path.exists(), f"schemas/emits/{name}.json is missing"
        schema = json.loads(path.read_text())
        # The filename IS the event name — that is how core's autoloader binds
        # the two, so a rename that touches only one of them is a schema that
        # silently stops being enforced.
        assert schema["title"] == name


def test_the_autoloader_registered_them():
    """Not "a schema file exists" — "the registry is holding it".

    The difference matters: a schema in a directory core never scans validates
    nothing, and the emit tests below would pass just as green.
    """
    from stapel_core.comm import action_registry

    for name in (EVENT_ISSUE_OPENED, EVENT_ISSUE_REGRESSED):
        assert action_registry._schemas.get(name) is not None, (
            f"{name} has no registered schema — autoload_schemas() did not "
            "see schemas/emits/, so nothing validates the payload"
        )


# ── What is emitted ─────────────────────────────────────────────────────


def test_a_new_issue_emits_opened(captured_events, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a", level="error", release="1.4.0")

    assert [e.event_type for e in captured_events] == [EVENT_ISSUE_OPENED]
    payload = captured_events[0].payload
    issue = Issue.objects.get()
    assert payload["issue_id"] == str(issue.id)
    assert payload["fingerprint"] == issue.fingerprint
    assert payload["service"] == "svc-a"
    assert payload["level"] == "error"
    assert payload["status"] == IssueStatus.NEW
    assert payload["release"] == "1.4.0"
    assert payload["count"] == 1


def test_the_event_is_keyed_by_the_issue(captured_events, django_capture_on_commit_callbacks):
    """Ordering per issue, not global: two bugs are independent facts."""
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a")
    issue = Issue.objects.get()
    assert captured_events[0].key == str(issue.id)


def test_a_repeat_occurrence_emits_nothing(
    captured_events, django_capture_on_commit_callbacks
):
    """The red case. An event per occurrence would put a failing loop's whole
    traffic on the bus and make every subscriber re-derive the grouping this
    module already did."""
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a")
    captured_events.clear()

    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a")
        record(trace=FK_VIOLATION_A, service="svc-a")

    assert captured_events == []


def test_a_regression_emits_regressed_and_carries_the_claimed_fix(
    captured_events, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=JWT_REFUSAL_A, service="svc-a")
    issue = Issue.objects.get()
    mark_fixed(issue, version="1.5.0", sha="deadbeef")
    captured_events.clear()

    with django_capture_on_commit_callbacks(execute=True):
        record(trace=JWT_REFUSAL_A, service="svc-a")

    assert [e.event_type for e in captured_events] == [EVENT_ISSUE_REGRESSED]
    payload = captured_events[0].payload
    assert payload["status"] == IssueStatus.REGRESSED
    assert payload["fixed_in_version"] == "1.5.0"
    assert payload["fixed_in_sha"] == "deadbeef"
    # The pair that separates "the fix is wrong" from "the fix is not deployed".
    assert payload["regressed_at"]


def test_two_different_bugs_emit_two_opened(
    captured_events, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a")
        record(trace=FK_VIOLATION_OTHER_CONSTRAINT, service="svc-a")

    assert [e.event_type for e in captured_events] == [
        EVENT_ISSUE_OPENED,
        EVENT_ISSUE_OPENED,
    ]
    assert len({e.payload["issue_id"] for e in captured_events}) == 2


# ── The emit may not take the alert row down with it ────────────────────


def test_a_broken_bus_does_not_lose_the_issue(
    monkeypatch, django_capture_on_commit_callbacks
):
    """The premise of the whole library, as a test.

    ``emit`` marks its transaction rollback-only when it fails, so an emit
    inside the ingest's atomic block would mean a broken outbox DELETES the
    alert. The bus is the thing whose failures this store records; it must
    record them when the bus is the thing that is broken.
    """
    from stapel_alerts import services

    def _explode(*args, **kwargs):
        raise RuntimeError("outbox table is gone")

    monkeypatch.setattr("stapel_core.comm.emit", _explode)

    with django_capture_on_commit_callbacks(execute=True):
        record(trace=FK_VIOLATION_A, service="svc-a")

    assert Issue.objects.count() == 1
    assert Issue.objects.get().status == IssueStatus.NEW
    # And it reported failure honestly rather than claiming it emitted.
    assert services.emit_issue_fact(
        Issue.objects.get(), None, created=True, regressed=False
    ) == ""


def test_a_payload_that_drifts_from_the_schema_is_refused(monkeypatch):
    """The schema is enforced, not decorative.

    If this ever passes with a junk payload, every other test in this file is
    asserting against a validator that is not running.
    """
    from stapel_core.comm import emit
    from stapel_core.comm.exceptions import SchemaValidationError

    with pytest.raises(SchemaValidationError):
        emit(EVENT_ISSUE_OPENED, {"issue_id": "not-a-uuid-and-nothing-else"})
