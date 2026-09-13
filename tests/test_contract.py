"""The committed contract is the one this code actually serves.

A schema.json that lags the views is worse than none: the frontend pair
generates its client from it, and a client generated against a stale contract
fails at runtime, in a shape nobody's tests cover. So the drift gate lives in
the suite, not only in a Makefile target somebody remembers to run.

Emission needs the pinned 3.12 interpreter (drf-spectacular renders component
descriptions differently across Python minors, so any other one produces false
diffs); the test says so instead of skipping silently on the rest of the
matrix, because a gate that quietly does nothing on three of four CI rows is
the family of green that proves nothing.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ("schema.json", "flows.json", "errors.json")


def test_the_triad_is_committed():
    for name in ARTIFACTS:
        path = ROOT / "docs" / name
        assert path.exists(), f"docs/{name} is missing — run `make contract`"
        json.loads(path.read_text())


def test_the_schema_serves_the_canonical_prefix():
    schema = json.loads((ROOT / "docs" / "schema.json").read_text())
    paths = set(schema["paths"])
    assert "/alerts/api/v1/issues" in paths
    assert "/alerts/api/v1/report" in paths
    assert "/alerts/api/v1/issues/{issue_id}/fix" in paths


def test_every_error_key_this_module_owns_is_in_the_catalog():
    from stapel_alerts.errors import STAPEL_ALERTS_ERRORS

    emitted = json.loads((ROOT / "docs" / "errors.json").read_text())
    keys = {row["code"] for row in emitted}
    missing = set(STAPEL_ALERTS_ERRORS) - keys
    assert not missing, f"error keys missing from docs/errors.json: {sorted(missing)}"
    # And every key the catalog attributes to us is one we declare — an
    # orphaned entry means a key was renamed and the old one left behind.
    ours = {row["code"] for row in emitted if row.get("owner") == "stapel_alerts"}
    assert ours == set(STAPEL_ALERTS_ERRORS)


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="contracts are emitted on the pinned 3.12 interpreter only "
    "(drf-spectacular renders component descriptions per Python minor); "
    "the drift gate runs on the 3.12 matrix row",
)
def test_the_committed_triad_matches_a_fresh_emission():
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [sys.executable, "-m", "stapel_alerts._codegen", "--out", tmp],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        for name in ARTIFACTS:
            fresh = (Path(tmp) / name).read_text()
            committed = (ROOT / "docs" / name).read_text()
            assert fresh == committed, (
                f"docs/{name} is stale — run `make contract` and commit it"
            )
