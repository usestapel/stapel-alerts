"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

stapel-alerts' own ``urls.py`` bakes ``api/v1/`` into every path; this harness
urlconf reproduces the documented host mount so drf-spectacular emits
``/alerts/api/v1/...``.
"""
from django.urls import include, path

urlpatterns = [
    path("alerts/", include("stapel_alerts.urls")),
]
