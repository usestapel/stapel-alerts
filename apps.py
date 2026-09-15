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

        # GDPR: registered ONLY where the store actually lives.
        #
        # Until 0.2.3 this ran in every process that installed the app, which
        # in a split deployment means every REPORTER too — and a reporter has
        # no rows. stapel-gdpr infers an owner's kind rather than taking a
        # declaration (LOCAL iff a provider with that section is registered in
        # THIS process), so a reporter registering here made itself the local
        # owner of a store held somewhere else. An account closure would then
        # run erase_subject over the reporter's own empty alerts tables,
        # return a receipt, and leave the real rows — the ones carrying
        # user_id — untouched in the owner's database. A certification over an
        # empty store, with no symptom at all.
        #
        # Found while adopting 0.2.2 on a client fleet: the fleet's GDPR
        # coordinator was a reporter, `manage.py check` refused its boot with
        # gdpr.E010, and the only honest answers available to the DEPLOYMENT
        # were all wrong — name it and certify nothing, opt out and claim it
        # holds no personal data, or list it as a remote owner nobody answers
        # and have erasure time out in silence. The defect is here, so the fix
        # is here: a process that cannot erase does not claim it can.
        from .transport import resolve_mode

        if resolve_mode() == "owner":
            from stapel_core.gdpr import gdpr_registry

            from .gdpr import AlertsGDPRProvider

            if not any(p.section == "alerts" for p in gdpr_registry.providers):
                gdpr_registry.register(AlertsGDPRProvider())
