"""The digest has to land in the TOPIC, not in the group's General tab.

Telegram routes a message into a forum topic only when the send carries
``message_thread_id``. Without it the API answers 200 and posts to General —
a send that succeeded and went to the wrong place, which is the same class of
defect as a schema that does not match the wire: everything reports success
and the destination is wrong.

Found on a client fleet, 2026-09-15. The deployment's Grafana contact point
had been posting into the topic all along because it sends the parameter;
this library's fallback did not, so the alert digests went to General and the
owner reported them as "the wrong channel". The chat id was never wrong.
"""
import json

import pytest

from stapel_alerts.fallback import send_telegram

CHAT_ID = "-1001234567890"
THREAD_ID = "4242"


@pytest.fixture
def sent(monkeypatch):
    """The decoded body of every request the sender would have made."""
    bodies = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    # stapel-notifications is not installed in the store's service, so the
    # direct Bot API sender is the path under test — assert that rather than
    # assume it.
    monkeypatch.setattr(
        "stapel_alerts.fallback._via_notifications",
        lambda chat_id, text, thread_id="": False,
    )
    return bodies


def _configure(settings, **fallback):
    settings.STAPEL_ALERTS = {
        "NOTIFY": "telegram",
        "FALLBACK": {"TELEGRAM_BOT_TOKEN": "test-token", **fallback},
    }


def test_without_a_thread_id_the_send_carries_none(settings, sent):
    """The state before this fix: chat only, and Telegram posts to General."""
    _configure(settings, TELEGRAM_CHAT_ID=CHAT_ID)

    assert send_telegram("subject", "body") is True
    assert len(sent) == 1
    assert sent[0]["chat_id"] == CHAT_ID
    assert "message_thread_id" not in sent[0]


def test_a_configured_thread_id_reaches_the_wire(settings, sent):
    """The fix: the topic the deployment configured is actually sent."""
    _configure(settings, TELEGRAM_CHAT_ID=CHAT_ID, TELEGRAM_THREAD_ID=THREAD_ID)

    assert send_telegram("subject", "body") is True
    assert len(sent) == 1
    assert sent[0]["chat_id"] == CHAT_ID
    assert sent[0]["message_thread_id"] == 4242


def test_the_thread_id_goes_out_as_a_number(settings, sent):
    """Telegram's own field is an integer; `"4242"` is not the same value."""
    _configure(settings, TELEGRAM_CHAT_ID=CHAT_ID, TELEGRAM_THREAD_ID=THREAD_ID)
    send_telegram("subject", "body")
    assert isinstance(sent[0]["message_thread_id"], int)


def test_a_non_numeric_thread_id_is_passed_through_rather_than_dropped(
    settings, sent
):
    """Dropping a configured value is the defect; guessing is not the fix."""
    _configure(settings, TELEGRAM_CHAT_ID=CHAT_ID, TELEGRAM_THREAD_ID="general")
    send_telegram("subject", "body")
    assert sent[0]["message_thread_id"] == "general"


def test_the_notifications_channel_is_offered_the_thread_when_it_takes_one(
    settings, monkeypatch
):
    """Feature-detected by signature, so an older channel keeps working."""
    import stapel_alerts.fallback as fallback

    calls = []

    def _channel(chat_id, text, thread_id=""):
        calls.append({"chat_id": chat_id, "thread_id": thread_id})

    monkeypatch.setattr(fallback, "_accepts", lambda fn, name: name == "thread_id")
    monkeypatch.setattr(
        fallback,
        "_via_notifications",
        fallback._via_notifications.__wrapped__
        if hasattr(fallback._via_notifications, "__wrapped__")
        else fallback._via_notifications,
    )
    _configure(settings, TELEGRAM_CHAT_ID=CHAT_ID, TELEGRAM_THREAD_ID=THREAD_ID)

    # Stand the channel up under the import path _via_notifications reaches.
    import sys
    import types

    module = types.ModuleType("stapel_notifications.channels.telegram")
    module.send_telegram = _channel
    package = types.ModuleType("stapel_notifications.channels")
    root = types.ModuleType("stapel_notifications")
    monkeypatch.setitem(sys.modules, "stapel_notifications", root)
    monkeypatch.setitem(sys.modules, "stapel_notifications.channels", package)
    monkeypatch.setitem(sys.modules, "stapel_notifications.channels.telegram", module)

    assert send_telegram("subject", "body") is True
    assert calls == [{"chat_id": CHAT_ID, "thread_id": THREAD_ID}]
