"""The channel that works when the alert store does not.

``STAPEL_ALERTS["NOTIFY"]`` is the seam: a dotted path to
``notify(subject, body) -> bool``, or the shipped name ``"telegram"``.

The telegram sender resolves in this order, and the order is the point:

1. **stapel-notifications' telegram channel**, if that library is installed
   and its ``TELEGRAM_PROVIDER`` is configured. A deployment that already has
   a bot wired into its notification stack should not configure a second one
   here, and this module should not own a delivery identity that already has
   an owner.
2. **the direct Bot API sender**, from ``STAPEL_ALERTS["FALLBACK"]``
   (``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``). Minimal by design: one
   POST to ``api.telegram.org``, stdlib only, no dependency.

Why a direct sender exists at all, when stapel-notifications ships the
channel: this is the path for the case where the owner is unreachable, and in
a small fleet the notifications module lives IN the owner. A fallback that
requires the thing that is down is not a fallback.

Nothing here raises. It is the last link in a chain whose every earlier link
has already failed.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 5.0

#: Telegram's hard limit on a message body. A digest of a buffered outage can
#: exceed it easily, and a 400 from the API would lose the whole message.
MAX_TELEGRAM_CHARS = 4000


def notify(subject: str, body: str) -> bool:
    """Send through the configured NOTIFY seam. Returns whether it went out."""
    from .conf import alerts_settings

    target = alerts_settings.NOTIFY
    if not target or target == "none":
        return False
    if callable(target):
        return _guard(target, subject, body)
    if target == "telegram":
        return send_telegram(subject, body)
    if isinstance(target, str) and "." in target:
        try:
            from django.utils.module_loading import import_string

            resolved = import_string(target)
        except Exception:
            logger.error(
                'STAPEL_ALERTS["NOTIFY"] = %r cannot be imported; the fallback '
                "channel is dead, which means a dead alert store is silent.",
                target,
            )
            return False
        return _guard(resolved, subject, body)
    logger.error(
        'STAPEL_ALERTS["NOTIFY"] = %r is neither "telegram", "none", a '
        "callable, nor a dotted path.", target,
    )
    return False


def _guard(fn, subject: str, body: str) -> bool:
    try:
        return bool(fn(subject, body))
    except Exception:
        logger.warning("alerts: NOTIFY seam raised", exc_info=True)
        return False


def send_telegram(subject: str, body: str) -> bool:
    """stapel-notifications' channel if it is configured, else the direct API."""
    text = (body or subject)[:MAX_TELEGRAM_CHARS]
    chat_id = _fallback_setting("TELEGRAM_CHAT_ID")

    if chat_id and _via_notifications(chat_id, text):
        return True
    return _via_bot_api(chat_id, text)


def _fallback_setting(key: str) -> str:
    from .conf import alerts_settings

    return str((alerts_settings.FALLBACK or {}).get(key, "") or "")


def _via_notifications(chat_id: str, text: str) -> bool:
    """Try stapel-notifications' telegram channel.

    Returns False — not an exception — when the library is absent or its
    provider is the shipped ``unconfigured`` one, because both are the normal
    state of a deployment that has not asked for it.
    """
    try:
        from stapel_notifications.channels.telegram import send_telegram as send
    except Exception:
        return False
    try:
        send(chat_id, text)
        return True
    except Exception as exc:
        logger.debug("alerts: notifications telegram channel unavailable (%s)", exc)
        return False


def _via_bot_api(chat_id: str, text: str) -> bool:
    """One POST to the Bot API with the token from STAPEL_ALERTS["FALLBACK"]."""
    token = _fallback_setting("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        logger.warning(
            "alerts: the owner is unreachable and no Telegram fallback is "
            'configured — set STAPEL_ALERTS["FALLBACK"] = '
            '{"TELEGRAM_BOT_TOKEN": ..., "TELEGRAM_CHAT_ID": ...}. '
            "Until then a dead alert store is a silent one."
        )
        return False

    request = urllib.request.Request(
        TELEGRAM_API.format(token=token),
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("alerts: Telegram refused the fallback (%s)", exc.code)
    except Exception as exc:
        logger.warning("alerts: Telegram fallback failed (%s)", exc)
    return False


__all__ = ["notify", "send_telegram", "MAX_TELEGRAM_CHARS", "TELEGRAM_API"]
