"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema`` annotations,
and an annotation is a CLAIM: it says what the view returns, and the generator
has no way to check it against the method body. 0.2.0 shipped ``GET /issues``
declared as ``Issue[]`` while the wire carried ``{count, offset, limit,
results}`` — the drift gate was green, because the committed schema matched
the fresh emission exactly; both were wrong in the same way. The frontend
pair generated a client against the claim and found out at runtime.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response, and validates the body it
gets against the schema it was promised. A new operation with a response body
and no request recipe below fails loudly rather than being skipped — a gate
that quietly covers three of four rows is the family of green that proves
nothing.

Runs on every interpreter: it reads the committed schema and never emits.
"""
import copy
import json
from pathlib import Path

import jsonschema
import pytest

from stapel_alerts.models import Issue
from stapel_alerts.services import record

from .traces import FK_VIOLATION_A, FK_VIOLATION_B

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs" / "schema.json").read_text())

#: How to perform each write operation the schema declares with a response
#: body, keyed by ``(METHOD, path template)``. GETs need no recipe: they are
#: performed as declared, with a seeded issue filling ``{issue_id}``.
WRITE_RECIPES = {
    ("PATCH", "/alerts/api/v1/issues/{issue_id}"): {"note": "reconciled"},
    ("POST", "/alerts/api/v1/issues/{issue_id}/fix"): {"version": "0.2.1"},
}


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the one divergence that matters here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null. Everything
    else drf-spectacular emits (``$ref``, ``allOf``, ``enum``, ``required``,
    ``readOnly``) is JSON Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 200-ish JSON schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            for code, response in op.get("responses", {}).items():
                body = response.get("content", {}).get("application/json", {}).get("schema")
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return ops


OPERATIONS = _operations()


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


@pytest.mark.parametrize(
    "method,path,code,body_schema", OPERATIONS, ids=[f"{m} {p}" for m, p, _, _ in OPERATIONS]
)
def test_the_wire_matches_the_declared_response(staff_client, method, path, code, body_schema):
    record(trace=FK_VIOLATION_A, service="svc-recordings")
    record(trace=FK_VIOLATION_B, service="svc-recordings")
    issue = Issue.objects.get()

    url = path.replace("{issue_id}", str(issue.id))
    assert "{" not in url, (
        f"{method} {path}: a path parameter this gate does not know how to fill "
        "— teach it here, or the operation goes unchecked"
    )

    if method == "GET":
        response = staff_client.get(url)
    else:
        assert (method, path) in WRITE_RECIPES, (
            f"{method} {path} declares a response body and has no request "
            "recipe in WRITE_RECIPES — an unchecked operation is a schema nobody proves"
        )
        response = getattr(staff_client, method.lower())(
            url, WRITE_RECIPES[(method, path)], format="json"
        )

    assert response.status_code == code, response.content
    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
    )
    # A list that came back empty validates against any item schema, so the
    # seeded issue must actually be in the page for the check to mean anything.
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        assert body["results"], f"{method} {path}: the seeded issue is not in the page"
