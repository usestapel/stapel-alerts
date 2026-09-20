"""The inputs — a host gets these by installing the app, not by wiring them.

The capture facade's two invariants are tested first, because everything below
depends on them: it never raises, and it is rate limited per fingerprint.
"""
import logging
import sys

import pytest

from stapel_alerts._capture import capture, reset_rate_limit
from stapel_alerts.inputs import (
    AlertsLogHandler,
    alerts_exception_handler,
    capture_exception,
    connect_dlq,
    install_log_handler,
    level_for,
    on_bus_event_parked,
    on_task_failure,
    remove_log_handler,
    wrap_deliver_to_subscribers,
)
from stapel_alerts.models import ErrorEvent, Issue

pytestmark = pytest.mark.django_db


def _boom(message="boom"):
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


# ── capture() ───────────────────────────────────────────────────────────


def test_capture_stores_an_exception_with_its_trace():
    assert capture(_boom()) is True

    event = ErrorEvent.objects.get()
    assert "ValueError: boom" in event.message
    assert "_boom" in event.trace
    assert Issue.objects.get().exception_class == "ValueError"


def test_capture_stores_a_bare_message():
    assert capture("STT provider exhausted", level="error", context={"provider": "x"}) is True

    event = ErrorEvent.objects.get()
    assert event.message == "STT provider exhausted"
    assert event.context == {"provider": "x"}


def test_capture_never_raises_when_the_store_is_broken(monkeypatch):
    """Every caller is on a failure path. An alert library that can turn a
    handled failure into an unhandled one gets removed after the first time."""
    def _explode(payload):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr("stapel_alerts.transport.send", _explode)

    assert capture(_boom()) is False
    assert ErrorEvent.objects.count() == 0


def test_a_rate_limit_drops_repeats_of_one_fingerprint(settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-test", "RATE_LIMIT": 2, "RATE_LIMIT_WINDOW_SECONDS": 3600,
    }
    reset_rate_limit()

    accepted = [capture(_boom("same")) for _ in range(5)]

    assert accepted == [True, True, False, False, False]
    assert ErrorEvent.objects.count() == 2


def test_what_the_rate_limit_dropped_is_counted_on_the_next_accepted_event(settings):
    """A limiter may lose detail. It may not lose the fact that it happened
    ten thousand times."""
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-test", "RATE_LIMIT": 1, "RATE_LIMIT_WINDOW_SECONDS": 0.05,
    }
    reset_rate_limit()

    capture(_boom("same"))          # accepted, occurrences=1
    for _ in range(4):
        capture(_boom("same"))      # suppressed
    import time
    time.sleep(0.06)                # the window rolls over
    capture(_boom("same"))          # accepted, carrying the 4 it swallowed

    issue = Issue.objects.get()
    assert ErrorEvent.objects.count() == 2
    assert issue.count == 6


def test_the_rate_limit_is_per_fingerprint_not_global(settings):
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-test", "RATE_LIMIT": 1, "RATE_LIMIT_WINDOW_SECONDS": 3600,
    }
    reset_rate_limit()

    assert capture(_boom("one")) is True
    assert capture(_boom("one")) is False
    # A different bug is not silenced by a noisy one.
    assert capture(_boom("two")) is True


# ── The logging handler ─────────────────────────────────────────────────


def test_a_warning_becomes_an_alert():
    """Through the handler AppConfig.ready() put on the ROOT logger — the
    host wired nothing, which is the claim being tested."""
    assert any(
        isinstance(h, AlertsLogHandler) for h in logging.getLogger().handlers
    ), "installing the app must install the handler"

    logging.getLogger("some.host.module").warning("disk is at %d%%", 94)

    event = ErrorEvent.objects.get()
    assert event.message == "disk is at 94%"
    assert event.level == "warning"
    assert event.kind == "log"
    assert event.context["logger"] == "some.host.module"


def test_an_exception_logged_carries_its_traceback():
    try:
        raise KeyError("missing")
    except KeyError:
        logging.getLogger("some.host.module").exception("lookup failed")

    assert "KeyError" in ErrorEvent.objects.get().trace


def test_below_the_level_nothing_is_captured():
    logger = logging.getLogger("some.host.module")
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("routine")
    finally:
        logger.setLevel(logging.NOTSET)

    assert ErrorEvent.objects.count() == 0


def test_this_package_is_never_captured_from_itself():
    """Otherwise a failure to report an alert logs an error which becomes an
    alert which fails to report, forever."""
    logging.getLogger("stapel_alerts.transport").error("owner unreachable")

    assert ErrorEvent.objects.count() == 0


def test_install_and_remove_the_root_handler_are_idempotent():
    try:
        remove_log_handler()
        first = install_log_handler()
        second = install_log_handler()
        assert first is second
        assert sum(
            isinstance(h, AlertsLogHandler) for h in logging.getLogger().handlers
        ) == 1
        remove_log_handler()
        assert not any(
            isinstance(h, AlertsLogHandler) for h in logging.getLogger().handlers
        )
    finally:
        # Every other test in the suite relies on the app-installed handler.
        install_log_handler()


@pytest.mark.parametrize(
    "levelno,expected",
    [
        (logging.CRITICAL, "fatal"),
        (logging.ERROR, "error"),
        (logging.WARNING, "warning"),
        (logging.INFO, "info"),
        (logging.DEBUG, "debug"),
        (1, "debug"),
    ],
)
def test_logging_levels_map_to_store_levels(levelno, expected):
    assert level_for(levelno) == expected


# ── The fleet exception handler ─────────────────────────────────────────


def test_a_5xx_is_captured_and_a_400_is_not():
    from rest_framework.exceptions import ValidationError

    class _Request:
        path = "/billing/api/v1/subscription"
        user = None

    # DRF turns this into a 400 — the API working, not a bug.
    assert alerts_exception_handler(ValidationError("nope"), {"request": _Request()}) is not None
    assert ErrorEvent.objects.count() == 0

    # Anything DRF cannot answer is a 500.
    assert alerts_exception_handler(RuntimeError("kaboom"), {"request": _Request()}) is None
    event = ErrorEvent.objects.get()
    assert event.request_path == "/billing/api/v1/subscription"
    assert event.kind == "exception"


def test_5xx_capture_can_be_switched_off(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "CAPTURE_5XX": False, "RATE_LIMIT": 1000}
    assert capture_exception(_boom()) is False
    assert ErrorEvent.objects.count() == 0


# ── Celery ──────────────────────────────────────────────────────────────


def test_a_celery_task_failure_is_captured():
    class _Task:
        name = "billing.charge_subscriptions"

    on_task_failure(sender=_Task(), task_id="abc-123", exception=_boom("charge failed"))

    event = ErrorEvent.objects.get()
    assert event.context["task"] == "billing.charge_subscriptions"
    assert event.context["task_id"] == "abc-123"


# ── DLQ parks and task-ledger unprocessable records ─────────────────────


class _Event:
    event_type = "workspace.personal.created"
    event_id = "005b0408-fe17-4b0a-b159-90511fe5cb5d"


def test_a_dlq_park_is_an_alert_by_construction():
    try:
        raise ValueError("foreign key violation")
    except ValueError:
        on_bus_event_parked(
            topic="workspace.personal.created",
            event=_Event(),
            reason="handler",
            exc_info=sys.exc_info(),
        )

    event = ErrorEvent.objects.get()
    assert event.kind == "dlq"
    assert event.context["reason"] == "handler"
    assert event.context["topic"] == "workspace.personal.created"
    assert event.context["event_id"] == _Event.event_id
    # The traceback survives the trip: "work was dropped" without the reason
    # is a number, not an alert.
    assert "ValueError: foreign key violation" in event.trace


def test_an_unprocessable_task_ledger_record_is_the_same_input():
    on_bus_event_parked(topic="task.transcribe", event=None, reason="unprocessable")

    event = ErrorEvent.objects.get()
    assert event.kind == "dlq"
    assert event.context["reason"] == "unprocessable"


def test_a_park_reaches_the_store_through_the_core_signal_when_core_has_one():
    """Cores from 0.68.1 announce a park; below that the ERROR line
    record_parked writes is caught by the log handler instead, so this asserts
    the wiring only where the wiring exists."""
    try:
        from stapel_core.signals import bus_event_parked
    except ImportError:
        pytest.skip("stapel-core < 0.68.1 has no bus_event_parked signal")

    # connect_dlq() is idempotent (dispatch_uid) and AppConfig.ready() already
    # called it — deliberately NOT disconnected afterwards, because every other
    # test in this module relies on the app's own wiring being live.
    assert connect_dlq() is True

    bus_event_parked.send(
        sender=None, topic="a.topic", event=_Event(), reason="handler", exc_info=None
    )

    assert ErrorEvent.objects.get().kind == "dlq"


def test_a_park_on_a_core_without_the_signal_is_caught_by_the_log_handler(monkeypatch):
    """record_parked() logs at ERROR on every core version, so a park is never
    invisible — it just arrives as text rather than as fields. Simulated by
    dropping the supersede entry the structured input registers."""
    from stapel_alerts import inputs
    from stapel_core.bus.dlq import record_parked

    monkeypatch.setattr(inputs, "_SUPERSEDED_LOGGERS", set())

    try:
        raise ValueError("handler blew up")
    except ValueError:
        record_parked("workspace.personal.created", _Event(), reason="handler")

    event = ErrorEvent.objects.filter(kind="log").first()
    assert event is not None
    assert "parked in DLQ" in event.message
    assert "ValueError: handler blew up" in event.trace


def test_a_park_is_one_issue_whichever_input_caught_it():
    """One park, one issue — on every core this module supports.

    From core 0.68.1 the park is BOTH announced and logged, and the log line
    would open a second issue with a different fingerprint if the structured
    input did not supersede it. Below 0.68.1 only the log line exists. The
    invariant that must hold either way is the count; which input caught it
    is what `superseded_loggers()` says, so the assertion reads that rather
    than assuming a core version."""
    from stapel_alerts.inputs import superseded_loggers
    from stapel_core.bus.dlq import record_parked

    structured = "stapel_core.bus.dlq" in superseded_loggers()

    try:
        raise ValueError("handler blew up")
    except ValueError:
        record_parked("workspace.personal.created", _Event(), reason="handler")

    assert Issue.objects.count() == 1
    assert ErrorEvent.objects.get().kind == ("dlq" if structured else "log")


def test_dlq_capture_can_be_switched_off(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "svc-test", "CAPTURE_DLQ": False, "RATE_LIMIT": 1000}
    on_bus_event_parked(topic="a.topic", event=_Event(), reason="handler")
    assert ErrorEvent.objects.count() == 0


# ── comm handler failures ───────────────────────────────────────────────


def test_a_comm_handler_failure_is_reported_without_changing_delivery():
    """deliver_to_subscribers RETURNS its exceptions rather than raising them,
    so they are exactly the ones no except clause anywhere will ever see."""
    from stapel_core.comm import actions as comm_actions

    # AppConfig.ready() already wrapped it — that is the mechanism under test.
    assert getattr(comm_actions.deliver_to_subscribers, "_stapel_alerts_wrapped", False)
    if True:
        class _E:
            event_type = "user.deleted"
            event_id = "e-1"
            payload = {}

        def _breaks(event):
            raise RuntimeError("handler exploded")

        errors = comm_actions.deliver_to_subscribers(_E(), [_breaks])

        # Delivery's own contract is untouched: the errors still come back.
        assert len(errors) == 1
        # One issue, not two: core also logs the failure at ERROR, and the
        # structured input supersedes that line.
        assert Issue.objects.count() == 1
        event = ErrorEvent.objects.get()
        assert event.context["event_type"] == "user.deleted"
        assert "RuntimeError: handler exploded" in event.trace


def test_wrapping_delivery_twice_does_not_double_report():
    """Two app registries, two worker reloads, one wrapper."""
    from stapel_core.comm import actions as comm_actions

    already = comm_actions.deliver_to_subscribers
    assert wrap_deliver_to_subscribers() is False
    assert comm_actions.deliver_to_subscribers is already
