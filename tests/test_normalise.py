"""The grouping rule, on real traces.

Every assertion here is about a decision production actually forced:
* three occurrences of one FK violation are ONE issue (the uuid varies);
* a different constraint on the same table, from the same frames, is a
  DIFFERENT issue (the fix is different);
* two JWT refusals with different user ids and different trace ids are ONE;
* two short traces that are merely similar are TWO.
"""
import pytest

from stapel_alerts.normalise import (
    ADDRESS_PLACEHOLDER,
    INT_PLACEHOLDER,
    SHORT_TRACE_FRAMES,
    TIMESTAMP_PLACEHOLDER,
    UUID_PLACEHOLDER,
    exception_class,
    fingerprint,
    is_generated_frame,
    normalise_line,
    normalise_path,
    normalise_trace,
    should_group,
    similarity,
    title_for,
)

from .traces import (
    FK_VIOLATION_A,
    FK_VIOLATION_B,
    FK_VIOLATION_C,
    FK_VIOLATION_OTHER_CONSTRAINT,
    JWT_REFUSAL_A,
    JWT_REFUSAL_B,
    SHORT_A,
    SHORT_B,
)


# ── The substitutions ───────────────────────────────────────────────────


def test_a_uuid_becomes_a_placeholder():
    assert normalise_line("Key (user_id)=(13484e5c-6652-4547-b0ac-a196393b4708)") == (
        f"Key (user_id)=({UUID_PLACEHOLDER})"
    )


def test_a_bare_hex_uuid_becomes_the_same_placeholder():
    assert UUID_PLACEHOLDER in normalise_line("id=13484e5c66524547b0aca196393b4708")


def test_a_memory_address_becomes_a_placeholder():
    assert normalise_line("<Job object at 0x104f2a390>") == (
        f"<Job object at {ADDRESS_PLACEHOLDER}>"
    )


def test_a_timestamp_becomes_a_placeholder():
    assert normalise_line("started 2026-09-13T06:32:46.616Z") == (
        f"started {TIMESTAMP_PLACEHOLDER}"
    )


def test_a_bare_integer_becomes_a_placeholder():
    assert normalise_line("retry 3 of 12") == (
        f"retry {INT_PLACEHOLDER} of {INT_PLACEHOLDER}"
    )


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/opt/venv/lib/python3.12/site-packages/django/db/models/query.py",
         "site-packages/django/db/models/query.py"),
        ("/home/ci/.venv/lib/python3.13/site-packages/django/db/models/query.py",
         "site-packages/django/db/models/query.py"),
        ("/app/recordings_service/actions.py", "app/recordings_service/actions.py"),
        ("relative/module.py", "relative/module.py"),
    ],
)
def test_a_deploy_specific_path_head_is_stripped(path, expected):
    assert normalise_path(path) == expected


def test_the_same_frame_under_two_interpreters_normalises_alike():
    a = '  File "/opt/venv/lib/python3.12/site-packages/django/db/models/query.py", line 949, in get_or_create'
    b = '  File "/usr/local/lib/python3.13/site-packages/django/db/models/query.py", line 951, in get_or_create'
    assert normalise_trace(a) == normalise_trace(b)


def test_a_generated_frame_is_dropped():
    assert is_generated_frame("<frozen importlib._bootstrap>")
    assert is_generated_frame("<string>")
    assert not is_generated_frame("/app/svc/tasks.py")
    trace = (
        'Traceback (most recent call last):\n'
        '  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load\n'
        '  File "/app/svc/tasks.py", line 12, in run\n'
        'RuntimeError: boom\n'
    )
    frames = normalise_trace(trace)
    assert not any("frozen" in f for f in frames)
    assert "app/svc/tasks.py:run" in frames


def test_a_line_number_is_not_part_of_a_frame():
    """One edit above a raise moves every line number below it. An issue that
    re-opens on an unrelated commit is a tracker nobody trusts."""
    a = '  File "/app/svc/tasks.py", line 12, in run'
    b = '  File "/app/svc/tasks.py", line 900, in run'
    assert normalise_trace(a) == normalise_trace(b) == ["app/svc/tasks.py:run"]


# ── Family 1: the FK violation ──────────────────────────────────────────


def test_three_accounts_one_foreign_key_bug_is_one_issue():
    assert should_group(FK_VIOLATION_A, FK_VIOLATION_B)
    assert should_group(FK_VIOLATION_A, FK_VIOLATION_C)
    assert should_group(FK_VIOLATION_B, FK_VIOLATION_C)


def test_three_accounts_one_foreign_key_bug_share_a_fingerprint():
    a = fingerprint(FK_VIOLATION_A, service="svc-recordings")
    b = fingerprint(FK_VIOLATION_B, service="svc-recordings")
    c = fingerprint(FK_VIOLATION_C, service="svc-recordings")
    assert a == b == c


def test_the_uuids_really_do_differ_in_the_fixtures():
    """Guards the test above from being green because the fixtures are equal."""
    assert FK_VIOLATION_A != FK_VIOLATION_B != FK_VIOLATION_C
    assert "13484e5c" in FK_VIOLATION_A
    assert "7d8224fd" in FK_VIOLATION_B


def test_a_different_constraint_is_a_different_issue():
    """Same frames, same exception class, same length — a different bug."""
    assert not should_group(FK_VIOLATION_A, FK_VIOLATION_OTHER_CONSTRAINT)
    assert fingerprint(FK_VIOLATION_A, service="s") != fingerprint(
        FK_VIOLATION_OTHER_CONSTRAINT, service="s"
    )


def test_the_similarity_of_the_family_clears_the_threshold_and_the_other_does_not():
    same = similarity(FK_VIOLATION_A, FK_VIOLATION_B)
    other = similarity(FK_VIOLATION_A, FK_VIOLATION_OTHER_CONSTRAINT)
    assert same == pytest.approx(1.0)
    assert other < 0.9


# ── Family 2: the JWT refusal ───────────────────────────────────────────


def test_two_stale_cookies_are_one_issue():
    assert should_group(JWT_REFUSAL_A, JWT_REFUSAL_B)
    assert fingerprint(JWT_REFUSAL_A, service="svc-billing") == fingerprint(
        JWT_REFUSAL_B, service="svc-billing"
    )


def test_the_two_families_are_not_each_other():
    assert not should_group(FK_VIOLATION_A, JWT_REFUSAL_A)


# ── Short traces ────────────────────────────────────────────────────────


def test_a_short_trace_groups_only_on_an_exact_match():
    assert len(normalise_trace(SHORT_A)) < SHORT_TRACE_FRAMES
    # Similar — same exception, same file, same message — and still two issues.
    assert similarity(SHORT_A, SHORT_B) > 0.0
    assert not should_group(SHORT_A, SHORT_B)
    # The same short trace with a different line number IS the same issue,
    # because it is identical after normalisation.
    assert should_group(SHORT_A, SHORT_A.replace("line 12", "line 77"))


# ── Identity helpers ────────────────────────────────────────────────────


def test_the_exception_class_is_the_one_that_escaped():
    """A chained traceback names several; the LAST is the one that got out."""
    assert exception_class(FK_VIOLATION_A) == "django.db.utils.IntegrityError"
    assert exception_class(JWT_REFUSAL_A) == "rest_framework.exceptions.AuthenticationFailed"


def test_the_service_is_part_of_the_fingerprint():
    """The same library failing the same way in two services is two issues:
    they are fixed, deployed and closed separately."""
    assert fingerprint(FK_VIOLATION_A, service="svc-a") != fingerprint(
        FK_VIOLATION_A, service="svc-b"
    )


def test_a_title_is_a_line_a_human_can_read():
    title = title_for(FK_VIOLATION_A)
    assert "IntegrityError" in title
    assert len(title) <= 255


def test_similarity_of_two_empty_traces_is_one_and_of_one_empty_is_zero():
    assert similarity("", "") == 1.0
    assert similarity(FK_VIOLATION_A, "") == 0.0
