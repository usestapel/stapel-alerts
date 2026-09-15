"""i18n error keys of stapel-alerts.

Only ``error.<status>.<slug>`` keys leave this package — human strings are
translations, never literals in a response body.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_INVALID_REPORT = "error.400.alerts_invalid_report"
ERR_400_BATCH_TOO_LARGE = "error.400.alerts_batch_too_large"
# `regressed` is the store's verdict on evidence, never a caller's assertion:
# a caller who could set it could also decline to, and hide a regression.
ERR_400_STATUS_NOT_SETTABLE = "error.400.alerts_status_not_settable"
ERR_401_SERVICE_KEY_REQUIRED = "error.401.alerts_service_key_required"
ERR_403_SERVICE_KEY_INVALID = "error.403.alerts_service_key_invalid"
ERR_404_ISSUE_NOT_FOUND = "error.404.alerts_issue_not_found"
# A syntactically valid report the store could not write. 422 rather than 400:
# the body parsed and the key was accepted, so this is not "malformed" — it is
# "understood and unprocessable", which is the distinction a reporter needs to
# decide between dropping the batch and retrying it. Never a 500: the clients
# of this endpoint are every other service in the fleet, and a 500 turns each
# of them into a retrying, logging client complaining about the one component
# whose complaints nothing else records.
ERR_422_REPORT_NOT_STORABLE = "error.422.alerts_report_not_storable"

STAPEL_ALERTS_ERRORS = {
    ERR_400_INVALID_REPORT: "The report payload is malformed",
    ERR_400_BATCH_TOO_LARGE: "Too many events in one report (max {max})",
    ERR_400_STATUS_NOT_SETTABLE: (
        "Status {status} is set by the store from evidence and cannot be assigned"
    ),
    ERR_401_SERVICE_KEY_REQUIRED: "A service key or a staff session is required",
    ERR_403_SERVICE_KEY_INVALID: "This service key is not valid",
    ERR_404_ISSUE_NOT_FOUND: "Issue not found",
    ERR_422_REPORT_NOT_STORABLE: (
        "{events} event(s) in this report could not be stored and were dropped"
    ),
}

register_service_errors(STAPEL_ALERTS_ERRORS)

__all__ = [
    "STAPEL_ALERTS_ERRORS",
    "ERR_400_INVALID_REPORT",
    "ERR_400_BATCH_TOO_LARGE",
    "ERR_400_STATUS_NOT_SETTABLE",
    "ERR_401_SERVICE_KEY_REQUIRED",
    "ERR_403_SERVICE_KEY_INVALID",
    "ERR_404_ISSUE_NOT_FOUND",
    "ERR_422_REPORT_NOT_STORABLE",
]
