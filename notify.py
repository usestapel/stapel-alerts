"""Who gets told, and when the store keeps quiet.

Three thresholds, and nothing else:

* a **new issue** — a bug nobody has seen before;
* a **regression** — an issue somebody marked fixed came back, which is worse
  news than a new one and is deliberately not subject to quiet hours' full
  silence at ``fatal``;
* a **count spike** — an issue already known is now happening far more often
  than it was. The alert is not the bug, it is the change of rate.

Repeat occurrences of a known issue notify NOBODY. An alert store that emails
on every occurrence is a mailing list people filter, and then the new issue
that mattered arrives in the filtered folder.

Delivery goes to stapel-notifications when it is installed (email/chat, the
recipients' own preferences honoured) and to the Telegram fallback seam when
it is not — the same seam a reporter uses when the owner is unreachable, so a
deployment configures one channel, not two.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


def notify_issue(issue, event, *, created: bool, regressed: bool) -> str:
    """Decide and send. Returns the reason it notified, or ``""``."""
    reason = _reason(issue, created=created, regressed=regressed)
    if not reason:
        return ""
    if not _worth_waking_somebody(issue):
        return ""
    if issue.is_muted_now():
        return ""
    if _in_quiet_hours() and issue.level != "fatal":
        return ""

    subject = f"[{issue.service}] {reason}: {issue.title}"
    body = "\n".join([
        subject,
        "",
        f"issue:       {issue.id}",
        f"fingerprint: {issue.fingerprint}",
        f"level:       {issue.level}",
        f"count:       {issue.count}",
        f"first seen:  {issue.first_seen:%Y-%m-%d %H:%M:%S%z}",
        f"last seen:   {issue.last_seen:%Y-%m-%d %H:%M:%S%z}",
        f"culprit:     {issue.culprit}",
    ])
    if not _via_notifications(subject, body, issue):
        from .fallback import notify

        notify(subject, body)
    return reason


#: Severity order, so a configured floor can be compared against a level.
#: Local to this module rather than imported from ``services`` because this is
#: the notification policy's own ordering, and the two happening to agree today
#: is not a reason for one to break when the other changes.
_LEVEL_ORDER = ("debug", "info", "warning", "error", "fatal")


def _worth_waking_somebody(issue) -> bool:
    """Is this issue above the escalation floor?

    Owner's ruling, 2026-09-15, after watching the first hour of the tracker in
    the channel: **the notification channel is not a mirror of the store.**
    Announcing every first-seen issue, warnings included, makes the channel a
    duplicate of the thing it exists to escalate — and a channel that repeats
    the store is one people mute, which is how the `fatal` that mattered
    arrives in a muted channel.

    So the store keeps everything and the channel carries only what should
    wake a person. The floor is ``NOTIFY_MIN_LEVEL`` (``"error"`` by default)
    and it applies to EVERY reason, not just to a new issue: a warning that
    regressed is still a warning, and "warnings never go to Telegram" is the
    rule as it was given. A deployment that wants its warnings paged sets the
    floor to ``"warning"``; one that wants only outages sets ``"fatal"``.
    """
    from .conf import alerts_settings

    floor = str(alerts_settings.NOTIFY_MIN_LEVEL or "error").lower()
    try:
        return _LEVEL_ORDER.index(str(issue.level)) >= _LEVEL_ORDER.index(floor)
    except ValueError:
        # An unknown level on either side: escalate rather than swallow. A
        # misconfigured floor must not be a silent "notify nobody".
        logger.warning(
            "alerts: NOTIFY_MIN_LEVEL=%r or issue level %r is not one of %s; "
            "notifying anyway.", floor, issue.level, _LEVEL_ORDER,
        )
        return True


def _reason(issue, *, created: bool, regressed: bool) -> str:
    """Which of the three thresholds fired, if any — each one switchable.

    The three are configuration rather than constants because a deployment
    that pages on regressions but triages new issues in the store is a
    legitimate posture, and so is the reverse.
    """
    from .conf import alerts_settings

    if regressed and alerts_settings.NOTIFY_ON_REGRESSION:
        return "regressed"
    if created and alerts_settings.NOTIFY_ON_NEW:
        return "new issue"
    if alerts_settings.NOTIFY_ON_SPIKE and is_spiking(issue):
        return "count spike"
    return ""


def is_spiking(issue) -> bool:
    """Is this issue happening far more often than it was an hour ago?

    Measured over occurrences, not rows, so a rate-limited reporter's batched
    events still read as the rate they represent. ``SPIKE_MIN_COUNT`` is the
    floor that keeps 1 → 5 from being "a factor of five".
    """
    from django.db.models import Sum

    from .conf import alerts_settings
    from .models import ErrorEvent

    factor = float(alerts_settings.SPIKE_FACTOR)
    minimum = int(alerts_settings.SPIKE_MIN_COUNT)
    now = timezone.now()

    def total(since, until):
        return (
            ErrorEvent.objects.filter(
                issue=issue, received_at__gte=since, received_at__lt=until
            ).aggregate(n=Sum("occurrences"))["n"]
            or 0
        )

    recent = total(now - timedelta(hours=1), now)
    if recent < minimum:
        return False
    previous = total(now - timedelta(hours=2), now - timedelta(hours=1))
    # A first busy hour with no history to compare against is a spike: the
    # alternative is staying silent through exactly the hour that matters.
    if previous == 0:
        return True
    return recent >= previous * factor


def _in_quiet_hours(now=None) -> bool:
    """Are we inside the configured quiet window?

    ``{"start": 23, "end": 8, "tz": "Europe/Berlin"}`` — hours, local to the
    named zone, wrapping midnight. ``None`` (the default) disables the whole
    mechanism, which is the right default for a library: a deployment that has
    not said when its people sleep must not have alerts withheld on a guess.
    """
    from .conf import alerts_settings

    window = alerts_settings.QUIET_HOURS
    if not window:
        return False
    start = int(window.get("start", 0))
    end = int(window.get("end", 0))
    if start == end:
        return False
    now = now or timezone.now()
    tz_name = window.get("tz")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            now = now.astimezone(ZoneInfo(tz_name))
        except Exception:
            logger.warning("alerts: unknown QUIET_HOURS timezone %r", tz_name)
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _via_notifications(subject: str, body: str, issue) -> bool:
    """Send through stapel-notifications when it is installed and addressed.

    Returns False when the library is absent or ``NOTIFY_USER_IDS`` is empty —
    both normal — so the caller falls through to the Telegram seam rather than
    believing an alert was delivered to nobody.
    """
    from .conf import alerts_settings

    recipients = list(alerts_settings.NOTIFY_USER_IDS or [])
    if not recipients:
        return False
    try:
        from stapel_notifications.services import process_notification
    except Exception:
        return False

    sent = False
    for user_id in recipients:
        try:
            process_notification(
                notification_type=alerts_settings.NOTIFY_TYPE,
                user_id=str(user_id),
                variables={
                    "subject": subject,
                    "body": body,
                    "issue_id": str(issue.id),
                    "service": issue.service,
                    "level": issue.level,
                    "title": issue.title,
                    "count": issue.count,
                },
            )
            sent = True
        except Exception:
            logger.warning("alerts: notification to %s failed", user_id, exc_info=True)
    return sent


__all__ = ["notify_issue", "is_spiking"]
