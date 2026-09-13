"""The one call every input and every library makes.

    from stapel_alerts import capture

    capture(exc)                                    # an exception
    capture("STT provider exhausted", level="error", context={"provider": "x"})

Where it goes is a deployment question this module answers, not a caller
question: :func:`capture` builds the payload and hands it to the transport the
process is configured for — the local store when this process is the alerts
owner, HTTP-with-a-service-key when it is a reporter. A library calling
``capture`` does not learn the topology, which is what makes the same call
correct in a monolith and in a microservice.

Two invariants:

* **it never raises.** Every caller is on a failure path. An alert store that
  turns a handled failure into an unhandled one has made the outage worse, and
  the library would be removed after the first time it did.
* **it is rate limited per fingerprint.** A loop that fails ten thousand times
  a minute must not become ten thousand rows or ten thousand HTTP calls. What
  the limiter drops is counted and carried on the next accepted event's
  ``occurrences``, so the issue's ``count`` stays true.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

from . import normalise as norm

logger = logging.getLogger(__name__)

#: Set while this thread is inside capture(), so a failure in the alert path
#: that logs cannot be captured as an alert and recurse.
_reentrant = threading.local()


class _RateLimiter:
    """Per-fingerprint token window, with the suppressed count kept.

    Process-local on purpose: a reporter cannot reach a shared counter when
    the thing that is broken may be the network, and coordinating a limiter
    across processes to save rows in a table that is already capped would be
    machinery bought with the wrong currency.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # fingerprint -> [window_start, accepted_in_window, suppressed_since]
        self._state: dict[str, list] = {}

    def check(self, fingerprint: str, limit: int, window: float) -> tuple[bool, int]:
        """``(allowed, occurrences_this_event_stands_for)``.

        On an allowed event the suppressed backlog is folded into the return
        value and cleared: one row, honest count.
        """
        now = time.monotonic()
        with self._lock:
            state = self._state.get(fingerprint)
            if state is None or now - state[0] >= window:
                self._state[fingerprint] = [now, 1, 0]
                carried = state[2] if state else 0
                return True, 1 + carried
            if state[1] < limit:
                state[1] += 1
                carried = state[2]
                state[2] = 0
                return True, 1 + carried
            state[2] += 1
            return False, 0

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


_limiter = _RateLimiter()


def reset_rate_limit() -> None:
    """Drop the limiter's state. For tests and for a reloaded worker."""
    _limiter.reset()


def _trace_of(exc: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )


def capture(
    exc_or_message,
    *,
    level: str = "error",
    kind: str = "manual",
    context: dict | None = None,
    service: str | None = None,
    request_path: str = "",
    trace_id: str = "",
    user_id=None,
    exc_info=None,
) -> bool:
    """Record one occurrence. Returns whether it was accepted (not dropped).

    ``exc_or_message`` is an exception instance (its traceback is formatted)
    or a string. ``exc_info`` is the ``sys.exc_info()`` tuple when the caller
    has one and no exception object — the shape a logging handler is handed.
    """
    if getattr(_reentrant, "busy", False):
        return False
    _reentrant.busy = True
    try:
        return _capture(
            exc_or_message,
            level=level,
            kind=kind,
            context=context,
            service=service,
            request_path=request_path,
            trace_id=trace_id,
            user_id=user_id,
            exc_info=exc_info,
        )
    except Exception:
        # Deliberately swallowed and logged at WARNING on this module's own
        # logger, which the handler excludes by default. The alternative is an
        # alert library that can take down the service it is watching.
        logger.warning("alerts: capture failed", exc_info=True)
        return False
    finally:
        _reentrant.busy = False


def _capture(
    exc_or_message,
    *,
    level,
    kind,
    context,
    service,
    request_path,
    trace_id,
    user_id,
    exc_info,
) -> bool:
    from .conf import alerts_settings
    from .transport import send

    if isinstance(exc_or_message, BaseException):
        trace = _trace_of(exc_or_message)
        message = f"{type(exc_or_message).__name__}: {exc_or_message}"
        exc_class = type(exc_or_message).__name__
    else:
        message = str(exc_or_message)
        if exc_info and exc_info[0] is not None:
            trace = "".join(traceback.format_exception(*exc_info))
            exc_class = exc_info[0].__name__
        else:
            trace = ""
            exc_class = ""

    service = service or alerts_settings.SERVICE or "unknown"
    fingerprint = norm.fingerprint(
        trace or message, service=service, exc_class=exc_class, message=message
    )

    allowed, occurrences = _limiter.check(
        fingerprint,
        int(alerts_settings.RATE_LIMIT),
        float(alerts_settings.RATE_LIMIT_WINDOW_SECONDS),
    )
    if not allowed:
        return False

    if not trace_id:
        trace_id = _current_trace_id()

    payload = {
        "trace": trace,
        "message": message,
        "service": service,
        "level": level,
        "kind": kind,
        "environment": alerts_settings.ENVIRONMENT,
        "release": alerts_settings.RELEASE,
        "context": context or {},
        "request_path": request_path,
        "trace_id": trace_id,
        "user_id": str(user_id) if user_id else None,
        "occurrences": occurrences,
        "exc_class": exc_class,
    }
    send(payload)
    return True


def _current_trace_id() -> str:
    """The in-flight trace id, when the host runs core's trace context.

    Correlation is the difference between "something 500'd" and "this request
    500'd", and it costs one lookup that cannot fail.
    """
    try:
        from stapel_core.observability import trace_ids

        return trace_ids().get("trace_id", "") or ""
    except Exception:
        return ""


__all__ = ["capture", "reset_rate_limit"]
