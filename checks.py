"""Boot-time checks — the misconfigurations that make this module silent.

Every one of these describes a deployment that looks installed and reports
nothing. That is the failure mode worth a check: a store that is loudly broken
gets fixed on the first day, a store that quietly records nothing is found
during the next outage.
"""
from django.core.checks import Error, Warning, register

E_NO_SERVICE = "stapel_alerts.E001"
E_REPORTER_NO_KEY = "stapel_alerts.E002"
W_NO_FALLBACK = "stapel_alerts.W001"
W_OWNER_URL_IN_OWNER_MODE = "stapel_alerts.W002"


@register()
def check_alerts_configuration(app_configs, **kwargs):
    from .conf import alerts_settings
    from .transport import resolve_mode

    issues = []
    mode = resolve_mode()

    if not alerts_settings.SERVICE:
        issues.append(
            Error(
                'STAPEL_ALERTS["SERVICE"] is empty — every alert this process '
                'files will be attributed to "unknown".',
                hint='Set STAPEL_ALERTS = {"SERVICE": "svc-name"}. The name is '
                "how an issue is routed, filtered and closed; a tracker whose "
                "rows nobody can attribute is a log file with a database bill.",
                id=E_NO_SERVICE,
            )
        )

    if mode == "reporter":
        if not alerts_settings.SERVICE_KEY:
            issues.append(
                Error(
                    'STAPEL_ALERTS["OWNER_URL"] is set but SERVICE_KEY is empty '
                    "— every report will be refused and every alert will go to "
                    "the fallback channel, or nowhere.",
                    hint="Create the service row on the owner "
                    "(manage.py alerts_service <name>) and put the key it "
                    "prints into STAPEL_ALERTS[\"SERVICE_KEY\"]. It is shown once.",
                    id=E_REPORTER_NO_KEY,
                )
            )
        if not _fallback_configured(alerts_settings):
            issues.append(
                Warning(
                    "This process reports to an alerts owner over HTTP and has "
                    "no fallback channel configured. When the owner is "
                    "unreachable its alerts will be buffered and then dropped, "
                    "silently — which is the exact situation an alert store "
                    "exists to prevent.",
                    hint='Set STAPEL_ALERTS["FALLBACK"] = '
                    '{"TELEGRAM_BOT_TOKEN": ..., "TELEGRAM_CHAT_ID": ...}, or '
                    'point STAPEL_ALERTS["NOTIFY"] at your own '
                    "notify(subject, body) callable.",
                    id=W_NO_FALLBACK,
                )
            )
    elif alerts_settings.OWNER_URL:
        issues.append(
            Warning(
                'MODE is "owner" but OWNER_URL is set. The URL is ignored: this '
                "process writes to its own database.",
                hint="Remove OWNER_URL here, or set MODE to \"reporter\" if this "
                "process was meant to report elsewhere.",
                id=W_OWNER_URL_IN_OWNER_MODE,
            )
        )

    return issues


def _fallback_configured(alerts_settings) -> bool:
    notify = alerts_settings.NOTIFY
    if callable(notify):
        return True
    if isinstance(notify, str) and notify not in ("telegram", "none", ""):
        return True
    fallback = alerts_settings.FALLBACK or {}
    return bool(fallback.get("TELEGRAM_BOT_TOKEN") and fallback.get("TELEGRAM_CHAT_ID"))


__all__ = [
    "check_alerts_configuration",
    "E_NO_SERVICE",
    "E_REPORTER_NO_KEY",
    "W_NO_FALLBACK",
    "W_OWNER_URL_IN_OWNER_MODE",
]
