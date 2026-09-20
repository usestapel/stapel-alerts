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

NO EXPORT MAY SHARE ITS NAME WITH A SUBMODULE. PEP 562's ``__getattr__`` runs
only when normal attribute lookup FAILS, and importing ``pkg.name`` anywhere in
the process binds the submodule object onto ``pkg.name`` permanently. So an
export that collides with a submodule resolves to whichever the import order
reached first: the function when the caller imported it first, the MODULE when
anything imported the submodule first — and the call site then raises
``TypeError: 'module' object is not callable``.

That is not theoretical. Until 0.2.4 the function ``capture`` lived in a
submodule named ``capture``, and the log handler this package installs does
``from ._capture import capture`` inside ``emit()``. Every service that
installed the app therefore bound the SUBMODULE onto the package on its first
WARNING record, and from that moment ``from stapel_alerts import capture``
handed every library in the fleet a module. Callers are on failure paths and
guard the call, so the fleet's alerts simply stopped existing, silently. The
suite never saw it because conftest imported the submodule first too.

The submodule is now ``._capture`` and the public name ``stapel_alerts.capture``
is the callable under every import order. ``tests/test_import_surface.py``
fails if any future export takes a submodule's name back.
"""

__all__ = [
    "alerts_settings",
    "capture",
]

#: export name -> the PRIVATE module that defines it. The values must never be
#: a module whose basename equals its key (see the module docstring).
_LAZY_EXPORTS = {
    "alerts_settings": ".conf",
    "capture": "._capture",
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
