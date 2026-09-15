"""0.2.3: two things a reporter must not be punished for.

Both were found by adopting 0.2.2 on a live fleet, and both have the same
shape — a mechanism that was correct for a monolith and wrong the moment the
same app ran in a process that does not own the store.
"""
import pytest

from stapel_alerts.models import ErrorEvent, Issue
from stapel_alerts.serializers import ReportEventSerializer

pytestmark = pytest.mark.django_db


# ── The store keeps the record; it does not grade the submission ─────────


def test_an_over_length_field_is_accepted_not_refused():
    """A 4000-character request_path is a URL a CLIENT chose.

    0.2.2 declared the model's max_lengths on the wire serializer too, so a
    batch carrying one was refused whole with a 400 — every genuine alert
    travelling with it lost, to protect a column `bounds` was about to fit.
    """
    serializer = ReportEventSerializer(
        data={
            "message": "a real failure",
            "level": "error",
            "request_path": "/" + "p" * 4000,
            "release": "r" * 400,
            "trace_id": "t" * 400,
            "exc_class": "C" * 400,
            "service": "s" * 400,
            "environment": "e" * 400,
        }
    )
    assert serializer.is_valid(), serializer.errors


def test_what_cannot_be_repaired_is_still_refused():
    """Length is repairable. A uuid that is not one, and a count below 1,
    are not — and silently coercing those would be inventing data."""
    bad_user = ReportEventSerializer(data={"message": "x", "user_id": "not-a-uuid"})
    assert not bad_user.is_valid()
    assert "user_id" in bad_user.errors

    bad_count = ReportEventSerializer(data={"message": "x", "occurrences": 0})
    assert not bad_count.is_valid()
    assert "occurrences" in bad_count.errors


def test_the_long_payload_survives_the_whole_path_and_is_stored_fitted():
    from stapel_alerts.services import record

    serializer = ReportEventSerializer(
        data={
            "message": "LONGTITLE " + "E" * 4000,
            "level": "warning",
            "request_path": "/" + "p" * 4000,
            "release": "r" * 400,
        }
    )
    assert serializer.is_valid(), serializer.errors
    event = record(service="svc-cdn", **{
        k: v for k, v in serializer.validated_data.items() if k != "service"
    })

    assert event is not None
    issue = Issue.objects.get()
    assert len(issue.title) <= 255
    assert issue.title.endswith("…")
    assert len(event.request_path) <= 512
    assert ErrorEvent.objects.count() == 1


# ── A process that cannot erase does not claim it can ────────────────────


def _alerts_providers():
    from stapel_core.gdpr import gdpr_registry

    return [p for p in gdpr_registry.providers if p.section == "alerts"]


def test_the_owner_registers_the_gdpr_provider(settings):
    from stapel_alerts.apps import AlertsConfig
    from stapel_core.gdpr import gdpr_registry

    settings.STAPEL_ALERTS = {"MODE": "owner", "SERVICE": "store"}
    # `providers` is a read-only property over `_providers`; slice-assigning
    # the property mutates a throwaway list and clears nothing.
    gdpr_registry._providers[:] = [
        p for p in gdpr_registry._providers if p.section != "alerts"
    ]
    AlertsConfig.ready(AlertsConfig.__new__(AlertsConfig))
    assert len(_alerts_providers()) == 1


def test_a_reporter_does_not(settings):
    """It holds no rows: registering would make it the LOCAL owner of a store
    that lives somewhere else, and erasure would certify over empty tables."""
    from stapel_alerts.apps import AlertsConfig
    from stapel_core.gdpr import gdpr_registry

    settings.STAPEL_ALERTS = {
        "MODE": "reporter",
        "SERVICE": "svc-auth",
        "OWNER_URL": "http://store:8000",
        "SERVICE_KEY": "k",
    }
    # `providers` is a read-only property over `_providers`; slice-assigning
    # the property mutates a throwaway list and clears nothing.
    gdpr_registry._providers[:] = [
        p for p in gdpr_registry._providers if p.section != "alerts"
    ]
    AlertsConfig.ready(AlertsConfig.__new__(AlertsConfig))
    assert _alerts_providers() == []
