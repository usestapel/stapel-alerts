"""Reporting by topology: in-process, over HTTP, into the buffer, to Telegram.

The chain only earns its keep if each link is tested where the one above it is
broken — a fallback nobody has ever seen fail over is a fallback in name.
"""
import time

import pytest

from stapel_alerts import transport
from stapel_alerts._capture import capture, reset_rate_limit
from stapel_alerts.models import ErrorEvent

pytestmark = pytest.mark.django_db


def _reporter(settings, **extra):
    settings.STAPEL_ALERTS = {
        "SERVICE": "svc-billing",
        "RATE_LIMIT": 1000,
        "OWNER_URL": "https://owner.example.com",
        "SERVICE_KEY": "a-key",
        "RETRY_BACKOFF": [],
        "FALLBACK_AFTER_MINUTES": 0,
        **extra,
    }


def _boom(message="boom"):
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


# ── Mode resolution ─────────────────────────────────────────────────────


def test_a_monolith_needs_no_setting_to_be_the_owner(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "monolith"}
    assert transport.resolve_mode() == "owner"


def test_an_owner_url_makes_a_process_a_reporter(settings):
    _reporter(settings)
    assert transport.resolve_mode() == "reporter"


def test_mode_can_be_forced_against_the_inference(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "s", "OWNER_URL": "https://x", "MODE": "owner"}
    assert transport.resolve_mode() == "owner"


# ── Owner mode ──────────────────────────────────────────────────────────


def test_the_owner_writes_in_process(settings):
    settings.STAPEL_ALERTS = {"SERVICE": "monolith", "RATE_LIMIT": 1000}
    assert capture(_boom()) is True
    assert ErrorEvent.objects.count() == 1


# ── Reporter mode ───────────────────────────────────────────────────────


def test_a_reporter_posts_the_batch_with_its_service_key(settings, monkeypatch):
    _reporter(settings)
    posted = []

    def _fake_post(batch):
        posted.append(list(batch))
        return True

    monkeypatch.setattr(transport, "_post", _fake_post)

    assert capture(_boom()) is True
    assert len(posted) == 1 and len(posted[0]) == 1
    assert posted[0][0]["service"] == "svc-billing"
    # Nothing was written here: a reporter has no store.
    assert ErrorEvent.objects.count() == 0


def test_the_service_key_travels_in_the_header(settings, monkeypatch):
    _reporter(settings)
    seen = {}

    class _Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["key"] = request.get_header(transport.SERVICE_KEY_HEADER.capitalize())
        return _Response()

    monkeypatch.setattr(transport.urllib.request, "urlopen", _urlopen)

    assert capture(_boom()) is True
    assert seen["url"] == "https://owner.example.com/alerts/api/v1/report"
    assert seen["key"] == "a-key"


def test_an_unreachable_owner_buffers_instead_of_losing_the_event(settings, monkeypatch):
    _reporter(settings, FALLBACK_AFTER_MINUTES=60)
    monkeypatch.setattr(transport, "_post", lambda batch: False)

    capture(_boom())

    assert len(transport.buffer) == 1
    assert transport.buffer.down_since is not None


def test_the_buffer_is_flushed_on_the_first_successful_report(settings, monkeypatch):
    _reporter(settings, FALLBACK_AFTER_MINUTES=60)
    monkeypatch.setattr(transport, "_post", lambda batch: False)
    capture(_boom("one"))
    capture(_boom("two"))
    assert len(transport.buffer) == 2

    posted = []
    monkeypatch.setattr(transport, "_post", lambda batch: posted.append(list(batch)) or True)
    capture(_boom("three"))

    # All three go up together, and the buffer is empty and the outage over.
    assert len(posted[0]) == 3
    assert len(transport.buffer) == 0
    assert transport.buffer.down_since is None


def test_the_buffer_is_bounded_and_drops_the_oldest(settings, monkeypatch):
    """A reporter that ran out of memory holding alerts about an outage would
    be a second outage."""
    _reporter(settings, BUFFER_MAX=3, FALLBACK_AFTER_MINUTES=60)
    monkeypatch.setattr(transport, "_post", lambda batch: False)

    for i in range(6):
        capture(_boom(f"boom-{i}"))

    assert len(transport.buffer) == 3
    assert transport.buffer.dropped == 3
    kept = [item["message"] for item in transport.buffer.peek()]
    assert "boom-5" in kept[-1]


def test_a_refused_key_is_not_retried(settings, monkeypatch):
    """A 4xx is our fault and does not become right by being repeated."""
    import urllib.error

    _reporter(settings, RETRY_BACKOFF=[0, 0, 0])
    attempts = []

    def _urlopen(request, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(transport.urllib.request, "urlopen", _urlopen)

    assert transport._post([{"message": "x"}]) is False
    assert len(attempts) == 1


def test_a_server_error_is_retried_through_the_backoff(settings, monkeypatch):
    import urllib.error

    _reporter(settings, RETRY_BACKOFF=[0, 0])
    attempts = []

    def _urlopen(request, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, None)

    monkeypatch.setattr(transport.urllib.request, "urlopen", _urlopen)

    assert transport._post([{"message": "x"}]) is False
    assert len(attempts) == 3  # the first try plus two backoff steps


def test_a_reporter_without_a_key_does_not_even_try(settings):
    _reporter(settings, SERVICE_KEY="")
    assert transport._post([{"message": "x"}]) is False


# ── The Telegram fallback ───────────────────────────────────────────────


def test_a_long_outage_digests_the_buffer_to_the_fallback(settings, monkeypatch, sent_fallback):
    _reporter(settings, FALLBACK_AFTER_MINUTES=0)
    monkeypatch.setattr(transport, "_post", lambda batch: False)

    capture(_boom("first"))

    assert len(sent_fallback) == 1
    subject, body = sent_fallback[0]
    assert "cannot reach the alert store" in subject
    assert "svc-billing" in subject
    assert "first" in body


def test_a_short_blip_does_not_shout(settings, monkeypatch, sent_fallback):
    """A deploy is a few seconds of unreachable owner. A channel that shouts
    through every deploy is a channel people mute."""
    _reporter(settings, FALLBACK_AFTER_MINUTES=60)
    monkeypatch.setattr(transport, "_post", lambda batch: False)

    capture(_boom())

    assert sent_fallback == []
    assert len(transport.buffer) == 1


def test_the_digest_is_not_repeated_on_every_event(settings, monkeypatch, sent_fallback):
    _reporter(settings, FALLBACK_AFTER_MINUTES=0.0005)  # ~30ms
    monkeypatch.setattr(transport, "_post", lambda batch: False)

    capture(_boom("one"))
    time.sleep(0.04)
    reset_rate_limit()
    capture(_boom("two"))
    capture(_boom("three"))

    # Two digests at most across three events, never one per event.
    assert 1 <= len(sent_fallback) <= 2


def test_telegram_goes_through_the_notifications_channel_when_it_is_configured(
    settings, monkeypatch
):
    """A deployment that already has a bot in its notification stack must not
    have to configure a second one here."""
    from stapel_alerts import fallback

    settings.STAPEL_ALERTS = {
        "SERVICE": "s",
        "FALLBACK": {"TELEGRAM_CHAT_ID": "-100123"},
    }
    sent = []
    monkeypatch.setattr(
        fallback, "_via_notifications", lambda chat_id, text, thread_id="": sent.append((chat_id, text)) or True
    )

    assert fallback.send_telegram("subject", "body") is True
    assert sent == [("-100123", "body")]


def test_the_direct_bot_api_is_used_when_notifications_cannot_send(settings, monkeypatch):
    from stapel_alerts import fallback

    settings.STAPEL_ALERTS = {
        "SERVICE": "s",
        "FALLBACK": {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100123"},
    }
    monkeypatch.setattr(fallback, "_via_notifications", lambda chat_id, text, thread_id="": False)
    seen = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = request.data
        return _Response()

    monkeypatch.setattr(fallback.urllib.request, "urlopen", _urlopen)

    assert fallback.send_telegram("subject", "body") is True
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert b"-100123" in seen["body"]


def test_an_unconfigured_fallback_says_so_instead_of_pretending(settings, monkeypatch):
    from stapel_alerts import fallback

    settings.STAPEL_ALERTS = {"SERVICE": "s", "FALLBACK": {}}
    monkeypatch.setattr(fallback, "_via_notifications", lambda chat_id, text, thread_id="": False)

    assert fallback.send_telegram("subject", "body") is False


def test_a_custom_notify_seam_is_used_verbatim(settings):
    from stapel_alerts import fallback

    called = []
    settings.STAPEL_ALERTS = {
        "SERVICE": "s",
        "NOTIFY": lambda subject, body: called.append(subject) or True,
    }

    assert fallback.notify("subject", "body") is True
    assert called == ["subject"]


def test_a_notify_seam_that_raises_does_not_escape(settings):
    from stapel_alerts import fallback

    def _explode(subject, body):
        raise RuntimeError("telegram is down too")

    settings.STAPEL_ALERTS = {"SERVICE": "s", "NOTIFY": _explode}

    assert fallback.notify("subject", "body") is False


def test_a_long_digest_is_truncated_to_what_telegram_accepts(settings, monkeypatch):
    from stapel_alerts import fallback

    settings.STAPEL_ALERTS = {"SERVICE": "s", "FALLBACK": {"TELEGRAM_CHAT_ID": "1"}}
    sent = []
    monkeypatch.setattr(
        fallback, "_via_notifications", lambda chat_id, text, thread_id="": sent.append(text) or True
    )

    fallback.send_telegram("s", "x" * 10000)

    assert len(sent[0]) == fallback.MAX_TELEGRAM_CHARS
