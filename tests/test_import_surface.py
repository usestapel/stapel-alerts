"""The package's exported names must survive any import order.

WHAT THIS CLOSES
    ``stapel_alerts`` exports its names lazily (PEP 562). ``__getattr__`` runs
    only when normal attribute lookup FAILS — so the moment anything in the
    process imports a SUBMODULE whose name equals an export, Python binds the
    module object onto the package attribute and the export is gone for the
    life of the process. ``from stapel_alerts import capture`` then hands the
    caller a module and the call raises ``TypeError: 'module' object is not
    callable``.

    Until 0.2.4 that was exactly the shape of the public surface: the function
    ``capture`` and a submodule ``capture``. Every caller of an alert library
    is on a failure path and guards the call, so the breakage never surfaced as
    a crash — the alerts just silently stopped being filed.

    The tests below are deliberately two: one reproduces the ORDER (and would
    have failed on 0.2.3), the other forbids the SHAPE, so a future export
    cannot reintroduce it under a different name.

WHY THE ORDER TESTS RUN IN SUBPROCESSES
    Binding is per-process and permanent. A conftest, another test module or
    the log handler will already have imported the private module by the time
    this file runs, which is precisely what made the defect invisible to a
    suite that shared one interpreter. Each order gets a clean interpreter.
"""
from __future__ import annotations

import pkgutil
import subprocess
import sys

import pytest

#: Import order A — the export first. This one worked even when broken.
ORDER_A = "from stapel_alerts import capture"

#: Import order B — a submodule first, then the export. THE REGRESSION: on
#: 0.2.3 this bound the submodule ``stapel_alerts.capture`` onto the package
#: and ``capture`` came back as a module.
ORDER_B = "import stapel_alerts._capture\nfrom stapel_alerts import capture"

#: Order C — how it actually happened on a stand: the installed log handler
#: imports the private module on the first WARNING record, long before any
#: library reaches for the export.
ORDER_C = (
    "import importlib\n"
    "importlib.import_module('stapel_alerts._capture')\n"
    "importlib.import_module('stapel_alerts.inputs')\n"
    "from stapel_alerts import capture"
)

_ASSERT_CALLABLE = (
    "\nimport types\n"
    "assert not isinstance(capture, types.ModuleType), (\n"
    "    'stapel_alerts.capture resolved to a MODULE, not the function'\n"
    ")\n"
    "assert callable(capture), type(capture)\n"
    "print('OK')\n"
)


@pytest.mark.parametrize(
    "order", [ORDER_A, ORDER_B, ORDER_C], ids=["export-first", "submodule-first", "handler-first"]
)
def test_capture_is_callable_under_every_import_order(order):
    """``stapel_alerts.capture`` is the function no matter what loaded first."""
    proc = subprocess.run(
        [sys.executable, "-c", order + _ASSERT_CALLABLE],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"import order failed:\n---\n{order}\n---\n{proc.stderr}"
    )
    assert "OK" in proc.stdout


def test_no_export_shares_its_name_with_a_submodule():
    """ARCHITECTURE GATE. Reusable as-is by any package with lazy exports.

    An export that collides with a submodule resolves to whichever the import
    order reached first. There is no way to write the package so both work, so
    the only fix is to not have the collision — which means catching it here,
    at the moment the name is added, rather than on a production host two days
    into an outage nobody was paged about.
    """
    import stapel_alerts

    submodules = {m.name for m in pkgutil.iter_modules(stapel_alerts.__path__)}
    clashes = sorted(set(stapel_alerts.__all__) & submodules)
    assert not clashes, (
        f"{sorted(clashes)}: exported by stapel_alerts/__init__.py AND the name "
        f"of a submodule. Importing the submodule anywhere in the process "
        f"shadows the export with a module object for the life of that process. "
        f"Rename the submodule (a leading underscore says it is not the public "
        f"name) and point the export at the new path."
    )


def test_every_lazy_export_resolves():
    """Each name in ``__all__`` is reachable and is not a module."""
    import types

    import stapel_alerts

    for name in stapel_alerts.__all__:
        value = getattr(stapel_alerts, name)
        assert not isinstance(value, types.ModuleType), (
            f"stapel_alerts.{name} is a module, not the exported object"
        )
