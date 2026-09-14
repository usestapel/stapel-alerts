"""stapel-alerts capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli

#: The DEFAULTS keys that are CTO-facing axes rather than tuning knobs.
#:
#: MODE is the one axis that changes what the module IS (a store, or a client
#: of one). NOTIFY is the channel a deployment actually reads at 3am. The
#: three CAPTURE_* switches decide which classes of failure are collected at
#: all, and MONITORING decides whether the blind-spot watchdog exists. A
#: retention window or a similarity threshold is a knob: it changes how much,
#: never whether.
_AXES = {
    "MODE",
    "NOTIFY",
    "CAPTURE_5XX",
    "CAPTURE_CELERY",
    "CAPTURE_DLQ",
    "MONITORING",
}


def main(argv=None):
    from stapel_alerts._codegen import _configure

    _configure()
    from stapel_alerts.conf import DEFAULTS
    from stapel_alerts.urls import GATE_REGISTRY

    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/alerts/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda key: key in _AXES,
        axis_group=axis_group_rules(
            exact={
                "MODE": "alerts.topology",
                "NOTIFY": "alerts.fallback",
                "CAPTURE_5XX": "alerts.inputs",
                "CAPTURE_CELERY": "alerts.inputs",
                "CAPTURE_DLQ": "alerts.inputs",
                "MONITORING": "alerts.monitoring",
            }
        ),
        prog="stapel-alerts-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
