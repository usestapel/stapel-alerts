"""Everything that produces an alert without a host writing a line of code.

The premise of the library is that a deployment gets an alert store by
installing it, not by instrumenting for it. So each of these is a subscription
to something the fleet already does:

``AlertsLogHandler``      a ``logging.Handler`` at WARNING+ — the broadest net,
                          and the only input that needs no cooperation at all
                          from what it is watching.
``capture_exception``     the hook the fleet exception handler calls for 5xx.
``on_task_failure``       Celery's ``task_failure`` signal.
``on_bus_event_parked``   core's ``bus_event_parked`` signal: a DLQ park or a
                          task-ledger ``unprocessable``, with the traceback
                          still attached. Work that was dropped is an alert BY
                          CONSTRUCTION, never a judgement call.
``wrap_deliver_to_subscribers``  comm handler failures, at the one place the
                          fleet delivers an Action to its handlers.

Every one of these runs on a path that is already failing, so every one of
them goes through :func:`stapel_alerts.capture`, which cannot raise.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Loggers whose records the log handler must NOT capture because a STRUCTURED
#: input in this process already reports the same failure, with more context.
#:
#: Core logs a DLQ park at ERROR and a comm handler failure at ERROR, and it
#: does so IN ADDITION to announcing them — the line is written for the human
#: reading container output, the signal for a listener. Once this process has
#: connected the structured input, the log line is the SAME failure a second
#: time, and because the two carry different messages they would fingerprint
#: apart and open two issues for one bug. The structured input wins.
_SUPERSEDED_LOGGERS: set[str] = set()


def superseded_loggers() -> frozenset:
    """Loggers currently covered by a structured input in this process."""
    return frozenset(_SUPERSEDED_LOGGERS)


#: Level names as this module stores them, from python logging levels.
_LEVEL_BY_LOGGING = (
    (logging.CRITICAL, "fatal"),
    (logging.ERROR, "error"),
    (logging.WARNING, "warning"),
    (logging.INFO, "info"),
    (logging.DEBUG, "debug"),
)


def level_for(levelno: int) -> str:
    for threshold, name in _LEVEL_BY_LOGGING:
        if levelno >= threshold:
            return name
    return "debug"


class AlertsLogHandler(logging.Handler):
    """A logging handler that files records into the alert store.

    Added to the root logger by ``AppConfig.ready()`` unless the host has
    disabled it. Two refusals are structural, not configuration:

    * records from this package (and from the loggers in ``LOG_EXCLUDE``) are
      never captured — otherwise a failure to report an alert logs an error
      which becomes an alert which fails to report, forever;
    * ``handleError`` is silent. The stdlib's default prints to stderr, and a
      handler that writes to stderr about its own failure inside a log call is
      how a logging chain turns into a loop.
    """

    def __init__(self, level: int | str | None = None) -> None:
        super().__init__(level if level is not None else logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        from ._capture import capture
        from .conf import alerts_settings

        if _excluded(record.name, alerts_settings.LOG_EXCLUDE):
            return
        if _excluded(record.name, _SUPERSEDED_LOGGERS):
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)

        capture(
            message,
            level=level_for(record.levelno),
            kind="log",
            exc_info=record.exc_info,
            context={
                "logger": record.name,
                "module": record.module,
                "func": record.funcName,
                "line": record.lineno,
            },
            trace_id=getattr(record, "trace_id", "") or "",
        )

    def handleError(self, record) -> None:  # pragma: no cover - stderr silence
        pass


def _excluded(logger_name: str, excluded) -> bool:
    """Is this logger (or any parent of it) on the exclusion list?"""
    for name in excluded or ():
        if logger_name == name or logger_name.startswith(f"{name}."):
            return True
    return False


def install_log_handler() -> AlertsLogHandler | None:
    """Attach the handler to the root logger, once. Returns it, or None."""
    from .conf import alerts_settings

    level = alerts_settings.LOG_LEVEL
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, AlertsLogHandler):
            return existing
    handler = AlertsLogHandler(level)
    root.addHandler(handler)
    return handler


def remove_log_handler() -> None:
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, AlertsLogHandler):
            root.removeHandler(existing)


# ── The fleet exception handler ─────────────────────────────────────────


def capture_exception(exc, context: dict | None = None) -> bool:
    """Called by the DRF exception hook for anything that became a 5xx.

    Only 5xx: a 400 is the API working. An alert store that files validation
    errors is a log, and nobody reads it.
    """
    from ._capture import capture
    from .conf import alerts_settings

    if not alerts_settings.CAPTURE_5XX:
        return False

    request = (context or {}).get("request")
    return capture(
        exc,
        level="error",
        kind="exception",
        request_path=_path_of(request),
        user_id=_user_of(request),
        context={"view": _view_name(context)},
    )


def alerts_exception_handler(exc, response_context):
    """Drop-in replacement for ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``.

    Delegates to core's ``stapel_exception_handler`` — the envelope is core's
    and stays core's — and captures whatever it could not turn into a
    response, plus anything it answered with a 5xx. A host wires this instead
    of core's handler; the response a client sees does not change.
    """
    from stapel_core.django.api.errors import stapel_exception_handler

    response = stapel_exception_handler(exc, response_context)
    if response is None or response.status_code >= 500:
        capture_exception(exc, response_context)
    return response


def _path_of(request) -> str:
    return getattr(request, "path", "") or ""


def _user_of(request):
    user = getattr(request, "user", None)
    user_id = getattr(user, "id", None)
    return str(user_id) if user_id else None


def _view_name(context: dict | None) -> str:
    view = (context or {}).get("view")
    return type(view).__name__ if view is not None else ""


# ── Celery ──────────────────────────────────────────────────────────────


def on_task_failure(sender=None, task_id=None, exception=None, einfo=None, **kwargs):
    """Celery's ``task_failure`` receiver.

    A failed task is work that did not happen and that nobody is waiting on —
    the same class as a DLQ park, and the class most likely to be silent.
    """
    from ._capture import capture
    from .conf import alerts_settings

    if not alerts_settings.CAPTURE_CELERY:
        return
    capture(
        exception if exception is not None else str(einfo),
        level="error",
        kind="exception",
        context={
            "task": getattr(sender, "name", str(sender)),
            "task_id": str(task_id) if task_id else "",
        },
    )


def connect_celery() -> bool:
    """Connect the Celery signal if celery is importable. Returns whether it was."""
    try:
        from celery.signals import task_failure
    except Exception:
        return False
    task_failure.connect(on_task_failure, weak=False, dispatch_uid="stapel_alerts")
    return True


# ── Bus DLQ parks and task-ledger unprocessable records ─────────────────


def on_bus_event_parked(sender=None, topic="", event=None, reason="", exc_info=None, **kwargs):
    """Receiver for ``stapel_core.signals.bus_event_parked``.

    A park is work the system gave up on. ``bus_dlq_total`` says how much;
    this says what, with the traceback, which is the difference between an
    alert you can act on and a number that goes up.
    """
    from ._capture import capture
    from .conf import alerts_settings

    if not alerts_settings.CAPTURE_DLQ:
        return
    event_type = getattr(event, "event_type", "") or "unknown"
    capture(
        f"bus: {reason} park on {topic} ({event_type})",
        level="error",
        kind="dlq",
        exc_info=exc_info,
        context={
            "topic": topic,
            "reason": reason,
            "event_type": event_type,
            "event_id": str(getattr(event, "event_id", "") or ""),
        },
    )


def connect_dlq() -> bool:
    """Connect to core's park signal. Unconditional — the floor guarantees it.

    Until 0.2 this was wrapped in a try/except that fell back to reading the
    ERROR line ``record_parked`` writes, because ``bus_event_parked`` only
    arrived in core 0.68.1 and the floor was 0.67.0. The floor is 0.68.1 now,
    so the fallback was a branch that could not be reached by any supported
    core and could not be tested against one — a path that is present, untried
    and believed to work is the shape of a gate that proves nothing.

    Note that ``record_parked`` still writes its ERROR line *next to* the
    signal, so the supersession below is not vestigial: without it a park
    opens two issues with two fingerprints, one from the signal and one from
    the text.
    """
    from stapel_core.signals import bus_event_parked

    bus_event_parked.connect(on_bus_event_parked, weak=False, dispatch_uid="stapel_alerts")
    _SUPERSEDED_LOGGERS.add("stapel_core.bus.dlq")
    return True


# ── comm handler failures ───────────────────────────────────────────────


def capture_handler_failure(event, handler, exc) -> None:
    """One comm Action handler raised while an event was being delivered.

    This is reported separately from the park that may follow it: several
    handlers are delivered the same event, the failures are different bugs,
    and by the time the event is parked only one reason survives.
    """
    from ._capture import capture

    capture(
        exc,
        level="error",
        kind="exception",
        context={
            "event_type": getattr(event, "event_type", ""),
            "event_id": str(getattr(event, "event_id", "") or ""),
            "handler": getattr(handler, "__qualname__", str(handler)),
        },
    )


def wrap_deliver_to_subscribers() -> bool:
    """Report every handler failure ``deliver_to_subscribers`` collects.

    That function is the fleet's single delivery point for an Action, and it
    RETURNS the exceptions rather than raising them (the consumer decides
    whether to retry or park). Returned exceptions are exactly the ones no
    ``except`` clause anywhere will see, so they are invisible unless
    something reads the return value — which is what this does, without
    changing it.
    """
    from stapel_core.comm import actions as comm_actions

    original = getattr(comm_actions, "deliver_to_subscribers", None)
    if original is None or getattr(original, "_stapel_alerts_wrapped", False):
        return False

    def wrapped(event, handlers):
        errors = original(event, handlers)
        for exc in errors or ():
            capture_handler_failure(event, None, exc)
        return errors

    wrapped._stapel_alerts_wrapped = True
    wrapped.__name__ = original.__name__
    wrapped.__doc__ = original.__doc__
    comm_actions.deliver_to_subscribers = wrapped
    _SUPERSEDED_LOGGERS.add("stapel_core.comm.actions")
    return True


__all__ = [
    "AlertsLogHandler",
    "superseded_loggers",
    "alerts_exception_handler",
    "capture_exception",
    "capture_handler_failure",
    "connect_celery",
    "connect_dlq",
    "install_log_handler",
    "level_for",
    "on_bus_event_parked",
    "on_task_failure",
    "remove_log_handler",
    "wrap_deliver_to_subscribers",
]
