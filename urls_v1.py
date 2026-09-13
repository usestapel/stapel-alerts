"""v1 URL set for stapel-alerts.

No prefix here — ``urls.py`` mounts this under ``api/v1/`` and the host mounts
that under ``alerts/``.
"""
from typing import NamedTuple

from django.urls import path

from .views import IssueDetailView, IssueFixView, IssueListView, ReportView

urlpatterns = [
    path("issues", IssueListView.as_view(), name="alerts-issues"),
    path("issues/<uuid:issue_id>", IssueDetailView.as_view(), name="alerts-issue-detail"),
    path("issues/<uuid:issue_id>/fix", IssueFixView.as_view(), name="alerts-issue-fix"),
    path("report", ReportView.as_view(), name="alerts-report"),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns.

    ``flags`` compose with OR; empty flags = always on.
    """

    name: str
    flags: tuple
    patterns: tuple


#: Gate registry (capability-config.md §2 p.2). The whole surface is one
#: always-on block: this module's axes (MODE, the input switches) change what
#: it CAPTURES, never which endpoints exist — a reporter simply never has the
#: urlconf mounted. Declared rather than left implicit so the capabilities
#: emitter has a uniform mechanism to read.
GATE_REGISTRY: dict = {
    "alerts.api": GateEntry("alerts.api", (), tuple(urlpatterns)),
}
