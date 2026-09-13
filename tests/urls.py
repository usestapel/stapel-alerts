"""Bare test mount — the same include a host writes."""
from django.urls import include, path

urlpatterns = [
    path("alerts/", include("stapel_alerts.urls")),
]
