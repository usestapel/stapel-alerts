"""Enum components in docs/schema.json are named, and follow the model.

drf-spectacular names a choice set after its property; when one property
name carries several sets it falls back to collision names (``Status295Enum``)
that change with unrelated edits and generator versions, renaming the react
pair's generated types, and the pair reads the issue status type by name.
The harness pins them (``_codegen_settings.enum_name_overrides``).
"""
import json
import re
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "schema.json"
COLLISION = re.compile(r"[0-9A-F][0-9a-f]{2}Enum$")


def _schemas():
    return json.loads(SCHEMA.read_text())["components"]["schemas"]


def test_no_enum_component_carries_a_collision_name():
    assert [name for name in _schemas() if COLLISION.search(name)] == []


def test_the_issue_status_set_has_one_name_and_is_the_model_choices():
    from stapel_alerts.models import IssueStatus
    from stapel_alerts.serializers import SETTABLE_STATUSES

    schemas = _schemas()
    assert schemas["IssueStatusEnum"]["enum"] == list(IssueStatus.values)
    assert schemas["IssuePatchStatusEnum"]["enum"] == [s.value for s in SETTABLE_STATUSES]
    refs = json.dumps(schemas)
    assert refs.count('"#/components/schemas/IssueStatusEnum"') >= 2
