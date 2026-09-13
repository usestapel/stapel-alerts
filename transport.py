"""Where a captured payload goes — and what happens when it cannot get there.

Topology, per the owner's ruling (2026-09-13): a microservice reports to the
alerts owner **by HTTP with a per-service API key**; a monolith calls the
store in process. Not the bus, in either case — the bus is the thing whose
failures this store is supposed to record, and a reporting path that dies
with it is a smoke detector wired to the burning fuse box.

So there are three layers, and each one exists because the one above it can
be unavailable:

1. **in-process** (owner mode) — a function call into ``services.record``.
2. **HTTP** (reporter mode) — ``POST {OWNER_URL}/alerts/api/v1/report`` with
   ``X-Service-Key``, retried with backoff.
3. **buffer + fallback** — when the owner cannot be reached, the payload is
   held in a bounded in-memory buffer and retried on the next capture; once
   the owner has been down for ``FALLBACK_AFTER_MINUTES``, the buffer is
   digested to the NOTIFY seam (Telegram by default), so a dead alert store
   is loud instead of silent.

The buffer is bounded and drops OLDEST first. A reporter that ran out of
memory holding alerts about an outage would be a second outage.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque

logger = logging.getLogger(__name__)

REPORT_PATH = "/alerts/api/v1/report"
SERVICE_KEY_HEADER = "X-Service-Key"


def resolve_mode() -> str:
    """``"owner"`` or ``"reporter"`` for this process.

    ``MODE="auto"`` (the default) means: reporter if an ``OWNER_URL`` is
    configured, owner otherwise. A monolith therefore configures nothing, and
    a microservice configures the two settings it obviously must have anyway.
    """
    from .conf import alerts_settings

    mode = (alerts_settings.MODE or "auto").lower()
    if mode in ("owner", "reporter"):
        return mode
    return "reporter" if alerts_settings.OWNER_URL else "owner"


class _Buffer:
    """Bounded FIFO of payloads waiting for an owner that is not answering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque = deque()
        self._dropped = 0
        #: When the owner first failed in the current outage. None = healthy.
        self.down_since: float | None = None
        #: When the last fallback digest was sent, so it is not re-sent per event.
        self.last_fallback: float = 0.0

    def push(self, payload: dict, maximum: int) -> None:
        with self._lock:
            self._items.append(payload)
            while len(self._items) > maximum:
                self._items.popleft()
                self._dropped += 1

    def drain(self) -> list[dict]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def peek(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def dropped(self) -> int:
        return self._dropped

    def reset(self) -> None:
        with self._lock:
            self._items.clear()
            self._dropped = 0
            self.down_since = None
            self.last_fallback = 0.0


buffer = _Buffer()


def send(payload: dict) -> bool:
    """Route one captured payload. Returns whether it reached a store."""
    if resolve_mode() == "owner":
        return _store_locally([payload])
    return _report(payload)


def _store_locally(payloads: list[dict]) -> bool:
    from .services import record

    ok = True
    for payload in payloads:
        try:
            record(**_as_record_kwargs(payload))
        except Exception:
            logger.warning("alerts: local store failed", exc_info=True)
            ok = False
    return ok


def _as_record_kwargs(payload: dict) -> dict:
    """The report wire shape, mapped onto ``services.record``'s signature.

    One place, used by the in-process path and by the HTTP endpoint, so the
    two can never diverge into two definitions of what an event is.
    """
    from django.utils.dateparse import parse_datetime

    occurred_at = payload.get("occurred_at")
    if isinstance(occurred_at, str):
        occurred_at = parse_datetime(occurred_at)
    return {
        "trace": payload.get("trace", "") or "",
        "message": payload.get("message", "") or "",
        "service": payload.get("service", "") or "",
        "level": payload.get("level", "error") or "error",
        "kind": payload.get("kind", "manual") or "manual",
        "environment": payload.get("environment", "production") or "production",
        "release": payload.get("release", "") or "",
        "context": payload.get("context") or {},
        "request_path": payload.get("request_path", "") or "",
        "trace_id": payload.get("trace_id", "") or "",
        "user_id": payload.get("user_id") or None,
        "occurrences": int(payload.get("occurrences", 1) or 1),
        "exc_class": payload.get("exc_class", "") or "",
        "occurred_at": occurred_at,
    }


def _report(payload: dict) -> bool:
    """Reporter mode: try the owner, with everything the buffer has been holding."""
    from .conf import alerts_settings

    batch = buffer.drain() + [payload]
    if _post(batch):
        _mark_up()
        return True

    _mark_down()
    maximum = int(alerts_settings.BUFFER_MAX)
    for item in batch:
        buffer.push(item, maximum)
    _maybe_fallback()
    return False


def _post(batch: list[dict]) -> bool:
    """POST the batch to the owner, retrying with backoff. Never raises."""
    from .conf import alerts_settings

    url = (alerts_settings.OWNER_URL or "").rstrip("/") + REPORT_PATH
    key = alerts_settings.SERVICE_KEY or ""
    if not alerts_settings.OWNER_URL or not key:
        # A reporter with no address or no key is a misconfiguration the
        # system check reports at boot; here it is simply "cannot deliver",
        # which is what sends the payload to the buffer and then to Telegram.
        return False

    body = json.dumps({"events": batch}).encode("utf-8")
    timeout = float(alerts_settings.TIMEOUT_SECONDS)
    backoff = list(alerts_settings.RETRY_BACKOFF or [])

    for attempt in range(len(backoff) + 1):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                SERVICE_KEY_HEADER: key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return True
                logger.warning("alerts: owner answered %s", response.status)
        except urllib.error.HTTPError as exc:
            # 4xx is OUR fault and will not become right by being retried —
            # a rejected key stays rejected. Stop and let the buffer + the
            # fallback carry it, which is also what makes the misconfiguration
            # visible instead of turning it into a retry storm.
            if 400 <= exc.code < 500:
                logger.warning("alerts: owner refused the report (%s)", exc.code)
                return False
            logger.warning("alerts: owner error %s", exc.code)
        except Exception as exc:
            logger.warning("alerts: owner unreachable (%s)", exc)

        if attempt < len(backoff):
            time.sleep(backoff[attempt])
    return False


def _mark_up() -> None:
    buffer.down_since = None
    buffer.last_fallback = 0.0


def _mark_down() -> None:
    if buffer.down_since is None:
        buffer.down_since = time.monotonic()


def _maybe_fallback() -> None:
    """Digest the buffer to the NOTIFY seam once the owner has been down long enough.

    Not on the first failure: a restart or a deploy is a few seconds of
    unreachable owner, and a channel that shouts through every deploy is a
    channel people mute. ``FALLBACK_AFTER_MINUTES`` is how long silence is
    allowed to be normal.
    """
    from .conf import alerts_settings
    from .fallback import notify

    if buffer.down_since is None:
        return
    after = float(alerts_settings.FALLBACK_AFTER_MINUTES) * 60
    now = time.monotonic()
    if now - buffer.down_since < after:
        return
    if buffer.last_fallback and now - buffer.last_fallback < after:
        return

    items = buffer.peek()
    if not items:
        return
    buffer.last_fallback = now
    subject = (
        f"alerts: {alerts_settings.SERVICE or 'a service'} cannot reach the alert "
        f"store ({len(items)} event(s) buffered"
        + (f", {buffer.dropped} dropped" if buffer.dropped else "")
        + ")"
    )
    lines = [subject, ""]
    for item in items[-10:]:
        lines.append(
            f"[{item.get('level')}] {item.get('service')}: "
            f"{(item.get('message') or '')[:200]}"
        )
    notify(subject, "\n".join(lines))


def reset() -> None:
    """Forget the buffer and the outage clock. For tests and worker reload."""
    buffer.reset()


__all__ = [
    "send",
    "resolve_mode",
    "buffer",
    "reset",
    "REPORT_PATH",
    "SERVICE_KEY_HEADER",
]
