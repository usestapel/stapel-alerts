"""Forwarding to Sentry, for deployments that have one.

The premise of this library is that the call site does not change when Sentry
is connected or disconnected: ``capture(...)`` is the same call, the local
store is written either way, and a DSN adds a forward on top. Nothing here is
ever a precondition for an event being recorded.

``sentry_sdk`` is an optional extra (``pip install stapel-alerts[sentry]``).
Absent, or unconfigured, or raising: the forward returns "" and the local row
is exactly as complete as it would have been.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def dsn() -> str:
    """The configured DSN — the module setting, else ``SENTRY_DSN`` in the env.

    The environment is read as a fallback because that is where a DSN
    genuinely lives in most deployments, and requiring it to be copied into a
    Django setting would be this library inventing a second place for a value
    the platform already has one place for.
    """
    from .conf import alerts_settings

    return str(alerts_settings.SENTRY_DSN or os.environ.get("SENTRY_DSN", "") or "")


def forward(event) -> str:
    """Send one :class:`~stapel_alerts.models.ErrorEvent` to Sentry.

    Returns the Sentry event id, or ``""`` for every reason there is not one —
    no DSN, no sdk installed, the sdk refused. Never raises.
    """
    if not dsn():
        return ""
    try:
        import sentry_sdk
    except ImportError:
        logger.debug(
            "alerts: SENTRY_DSN is set but sentry_sdk is not installed "
            "(pip install 'stapel-alerts[sentry]'); storing locally only."
        )
        return ""

    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("service", event.service)
            scope.set_tag("environment", event.environment)
            scope.set_tag("stapel_alerts_issue", str(event.issue_id))
            scope.set_level(_sentry_level(event.level))
            if event.user_id:
                scope.set_user({"id": str(event.user_id)})
            for key, value in (event.context or {}).items():
                scope.set_extra(key, value)
            message = event.message or event.trace[:500]
            return str(sentry_sdk.capture_message(message) or "")
    except Exception:
        logger.warning("alerts: Sentry forward failed", exc_info=True)
        return ""


_SENTRY_LEVELS = {
    "debug": "debug",
    "info": "info",
    "warning": "warning",
    "error": "error",
    "fatal": "fatal",
}


def _sentry_level(level: str) -> str:
    return _SENTRY_LEVELS.get(level, "error")


__all__ = ["dsn", "forward"]
