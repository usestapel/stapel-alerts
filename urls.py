"""Root URLconf for stapel-alerts — v1 canon mount (api-versioning.md §2, §6).

Canon: ``/<mod>/api/v1/...``. A host mounts::

    path("alerts/", include("stapel_alerts.urls"))   # -> /alerts/api/v1/...
"""
from django.urls import include, path

from stapel_alerts.urls_v1 import GATE_REGISTRY  # noqa: F401  (re-export)

urlpatterns = [
    path("api/v1/", include("stapel_alerts.urls_v1")),
]
