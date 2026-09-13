def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in _codegen_settings.py
        # so the test harness and `make contract` can never drift.
        from stapel_alerts._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django
        django.setup()


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
    from stapel_alerts.capture import reset_rate_limit
    from stapel_alerts.transport import reset

    reset_rate_limit()
    reset()
    yield
    reset_rate_limit()
    reset()


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
