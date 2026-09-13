from django.apps import AppConfig


class AlertsConfig(AppConfig):
    name = "stapel_alerts"
    label = "alerts"
    verbose_name = "Alert store"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from .inputs import connect_celery, connect_dlq, install_log_handler
        from .inputs import wrap_deliver_to_subscribers

        # Every input is wired here, so a host gets the store by INSTALLING
        # the app — the premise of the library. Each connector is individually
        # settings-gated and each one is a no-op when its subject is absent.
        install_log_handler()
        connect_dlq()
        connect_celery()
        wrap_deliver_to_subscribers()

        # GDPR: register the per-app data handler (monolith in-process mode).
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import AlertsGDPRProvider

        if not any(p.section == "alerts" for p in gdpr_registry.providers):
            gdpr_registry.register(AlertsGDPRProvider())
