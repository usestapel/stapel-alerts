"""stapel-alerts contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{alerts + core}`` Django instance mounted at the canonical
``alerts/api/v1/`` prefix:

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact ([] — no @flow_step here)
  docs/errors.json   generate_error_keys registry

The *mechanism* is stapel_tools.codegen (shared, unchanged); this file is the
thin per-module *config* that wires the module's settings + canonical mount.

Usage:
    python -m stapel_alerts._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != repo_root]

    from django.conf import settings

    if not settings.configured:
        from stapel_alerts._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_alerts.codegen_urls", contract=True)
        )

    import django

    django.setup()

    from drf_spectacular.settings import spectacular_settings

    from stapel_alerts._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # Register drf-spectacular's JWT cookie-auth extension explicitly (a real
    # host triggers this as a side effect of wiring its Swagger URLs). Without
    # it, reviews' protected endpoints (every view requires IsAuthenticated)
    # would emit without their `security: [{"JWTCookieAuth": []}]` entry — a
    # real contract gap. Applied per stapel-calendar's precedent.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort emission if not running the pinned 3.12 interpreter.

    drf-spectacular's rendering of component descriptions (``Optional[X]`` vs
    ``X | None``) depends on the Python minor version — contracts emitted on
    anything other than 3.12 (the CI/monolith pin) produce false diffs.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-alerts contract emission ABORTED: running Python {got}, "
            "but contracts must be emitted on Python 3.12 (the CI/monolith "
            "pin). drf-spectacular renders component descriptions (Optional[X] "
            "vs X | None) differently across Python minor versions, so emitting "
            "on any other minor produces false diffs against the committed "
            "docs/*.json. Re-run under a 3.12 interpreter."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-alerts-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /alerts/api/v1/ prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-alerts contract: {paths} paths, {flows} flows, {errors} error keys "
        f"→ {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
