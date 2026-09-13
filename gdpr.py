"""GDPR handler for stapel-alerts.

This module holds one piece of PII and holds it deliberately: the ``user_id``
on an event, because "which user was this failing for" is often the whole
diagnosis. It is a plain uuid — no name, no email, no FK.

Erasure removes the LINK, not the record. An alert store's rows are the
history of a system's failures, and a system does not un-fail because a user
left; what it must not keep is which person it failed for. So ``delete``
nulls ``user_id`` and scrubs any context value that repeats it, and the event
stays — the same treatment an audit log gets, for the same reason.
"""
from stapel_core.gdpr import GDPRProvider


class AlertsGDPRProvider(GDPRProvider):
    section = "alerts"

    def export(self, user_id) -> dict:
        from .models import ErrorEvent

        events = list(
            ErrorEvent.objects.filter(user_id=user_id).values(
                "id", "received_at", "service", "level", "kind",
                "message", "request_path",
            )[:1000]
        )
        return {"events": [{k: str(v) for k, v in row.items()} for row in events]}

    def delete(self, user_id) -> None:
        from .services import erase_subject

        erase_subject(user_id)

    def anonymize(self, user_id) -> None:
        # Identical to delete: the only subject data here IS the identifier,
        # so there is nothing to anonymize that erasure does not already do.
        self.delete(user_id)


__all__ = ["AlertsGDPRProvider"]
