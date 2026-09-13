"""stapel-alerts — an alert store for a fleet that has no Sentry.

The whole public surface is one function::

    from stapel_alerts import capture

    capture(exc)
    capture("STT provider exhausted", level="error", context={"provider": "x"})

Where it lands is a deployment's decision, not the call site's: the local
store when this process owns it, HTTP with a service key when it does not,
Telegram when the owner is unreachable, and Sentry as well when a DSN is set.

Lazily exported (PEP 562) — importing this package never pulls in Django or
requires configured settings.
"""

__all__ = [
    "alerts_settings",
    "capture",
]

_LAZY_EXPORTS = {
    "alerts_settings": ".conf",
    "capture": ".capture",
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        from importlib import import_module

        value = getattr(import_module(_LAZY_EXPORTS[name], __name__), name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
