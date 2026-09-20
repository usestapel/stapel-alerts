def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in _codegen_settings.py
        # so the test harness and `make contract` can never drift.
        from stapel_alerts._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django
        django.setup()

        # In a real host this happens in core's own AppConfig.ready(); here
        # the single-module instance has to ask for it, or every emit in the
        # suite would validate against a schema that was never registered —
        # a payload gate that passes because nothing is gating.
        from stapel_core.comm.schemas import autoload_schemas

        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def staff_user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        username="ops", email="ops@example.com", password="x", is_staff=True
    )


@pytest.fixture
def plain_user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        username="bob", email="bob@example.com", password="x"
    )


@pytest.fixture
def staff_client(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)
    return api_client


@pytest.fixture(autouse=True)
def _clean_state():
    """A rate limiter and a reporter buffer are PROCESS state, not database
    state, so `db` rollback does not touch them — and a limiter that carried
    over would silently swallow the first capture of the next test."""
    from stapel_alerts._capture import reset_rate_limit
    from stapel_alerts.transport import reset

    reset_rate_limit()
    reset()
    yield
    reset_rate_limit()
    reset()


@pytest.fixture
def captured_events():
    """Subscribe to this module's emits and collect the Event envelopes.

    Delivery is synchronous with the outbox disabled, so the list is populated
    by the time `emit()` returns.
    """
    from stapel_core.comm import action_registry, subscribe_action

    from stapel_alerts.services import EVENT_ISSUE_OPENED, EVENT_ISSUE_REGRESSED

    collected = []

    def _handler(event):
        collected.append(event)

    names = [EVENT_ISSUE_OPENED, EVENT_ISSUE_REGRESSED]
    for name in names:
        subscribe_action(name, _handler)
    try:
        yield collected
    finally:
        for name in names:
            handlers = action_registry._subscribers.get(name, [])
            if _handler in handlers:
                handlers.remove(_handler)


@pytest.fixture
def sent_fallback(monkeypatch):
    """Collect what the NOTIFY seam was asked to send."""
    sent = []

    def _notify(subject, body):
        sent.append((subject, body))
        return True

    monkeypatch.setattr("stapel_alerts.fallback.notify", _notify)
    monkeypatch.setattr("stapel_alerts.transport.notify", _notify, raising=False)
    return sent
