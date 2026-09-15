"""The 0.2.2 wave: bounded columns, the ignore set, and template grouping.

Every test here is a defect that reached a live fleet on 2026-09-15, within
the first hour of the tracker being mounted on a client fleet:

* a title longer than 255 characters made ``POST /report`` answer 500 with
  ``psycopg.errors.StringDataRightTruncation`` — an alert store that loses
  the alert AND becomes the loudest error on the host;
* three permission-bootstrap warnings that differ by one word became three
  issues, which is the exact failure grouping exists to prevent;
* the channel announced every first-seen issue including warnings, making it
  a duplicate of the store rather than an escalation of it;
* and the noise it announced was Django's own migration chatter and an
  internet scanner, neither of which is a defect of the deployment.
"""
import pytest

from stapel_alerts import bounds
from stapel_alerts.ignore import is_ignored
from stapel_alerts.models import ErrorEvent, Issue
from stapel_alerts.services import record

pytestmark = pytest.mark.django_db


# ── 1. Nothing untrusted reaches a bounded column unbounded ──────────────


def test_limits_are_read_off_the_model_not_typed_twice():
    """The numbers live in one place. A `max_length` change moves both."""
    assert bounds.max_length_of(Issue, "title") == Issue._meta.get_field("title").max_length
    assert bounds.max_length_of(ErrorEvent, "request_path") == 512
    # A TextField has no bound, and asking is not an error.
    assert bounds.max_length_of(ErrorEvent, "message") is None
    assert bounds.fit(ErrorEvent, "message", "x" * 10_000) == "x" * 10_000


def test_the_payload_that_returned_500_now_stores():
    """A 4000-char title, a 300-char culprit, a long path, an unknown level."""
    event = record(
        message="E" * 4000,
        service="svc-cdn",
        level="screaming",            # not a level
        kind="not-a-kind",            # not a kind
        request_path="/" + "p" * 4000,
        release="r" * 400,
        environment="e" * 400,
        trace_id="t" * 400,
    )

    assert event is not None
    issue = Issue.objects.get()
    for model, row, field in (
        (Issue, issue, "title"),
        (Issue, issue, "culprit"),
        (Issue, issue, "service"),
        (Issue, issue, "environment"),
        (Issue, issue, "exception_class"),
        (ErrorEvent, event, "request_path"),
        (ErrorEvent, event, "release"),
        (ErrorEvent, event, "trace_id"),
    ):
        limit = bounds.max_length_of(model, field)
        assert len(getattr(row, field)) <= limit, field


def test_a_cut_is_marked_so_a_reader_can_tell():
    """A silent truncation is a lie about the data."""
    fitted = bounds.fit(Issue, "title", "T" * 4000)
    assert len(fitted) == 255
    assert fitted.endswith(bounds.ELLIPSIS)
    assert not bounds.fit(Issue, "title", "short").endswith(bounds.ELLIPSIS)


def test_a_bad_choice_falls_back_to_the_default_rather_than_truncating():
    """`"screaming"[:16]` is not a level, and neither is `"critical"` cut."""
    assert bounds.choice_or_default(Issue, "level", "screaming") == "error"
    assert bounds.choice_or_default(Issue, "level", "fatal") == "fatal"
    assert bounds.choice_or_default(Issue, "kind", "nonsense") == "exception"


def test_truncation_cannot_change_how_two_identical_errors_group():
    """The property the whole ordering in `record` exists to protect."""
    for _ in range(3):
        record(message="Z" * 4000, service="svc-cdn", level="error")

    assert Issue.objects.count() == 1
    assert Issue.objects.get().count == 3


def test_the_fingerprint_is_never_truncated():
    record(message="Y" * 4000, service="svc-cdn", level="error")
    fingerprint = Issue.objects.get().fingerprint
    assert len(fingerprint) == 64
    assert not fingerprint.endswith(bounds.ELLIPSIS)


# ── 2. The ignore set, at the door ───────────────────────────────────────

PERMISSION_WARNINGS = [
    "Could not add permission {'app_label': 'cdn', 'model': 'asset', "
    "'codename': 'add_asset'}: ContentType matching query does not exist.",
    "Could not add permission {'app_label': 'cdn', 'model': 'asset', "
    "'codename': 'change_asset'}: ContentType matching query does not exist.",
    "Could not add permission {'app_label': 'cdn', 'model': 'asset', "
    "'codename': 'view_asset'}: ContentType matching query does not exist.",
]


def test_a_disallowed_host_report_is_silenced_at_the_door():
    """A scanner typing a raw address is not a defect of ours."""
    event = record(
        message="Invalid HTTP_HOST header: 'alerts-proof-a.invalid'.",
        service="svc-cdn",
        level="warning",
    )
    assert event is None
    assert Issue.objects.count() == 0


def test_the_exception_class_alone_is_enough_to_ignore():
    assert is_ignored("anything at all", exc_class="DisallowedHost")
    assert is_ignored("anything", exc_class="django.core.exceptions.DisallowedHost")
    assert not is_ignored("a real failure", exc_class="IntegrityError")


def test_migration_permission_chatter_never_becomes_an_issue():
    for message in PERMISSION_WARNINGS:
        assert record(message=message, service="svc-cdn", level="warning") is None
    assert Issue.objects.count() == 0


def test_a_host_extends_the_defaults_rather_than_replacing_them(settings):
    settings.STAPEL_ALERTS = {"IGNORE_PATTERNS": [r"a noisy vendor library"]}
    assert is_ignored("a noisy vendor library exploded")
    # and the shipped default is still in force
    assert is_ignored("Invalid HTTP_HOST header: 'x'")


def test_a_real_failure_is_not_ignored():
    event = record(
        message="IntegrityError: duplicate key value violates unique constraint",
        service="svc-cdn",
        level="error",
    )
    assert event is not None
    assert Issue.objects.count() == 1


# ── 3. Grouping on a template with one varying word ──────────────────────


def test_the_three_permission_messages_fold_into_one_issue(settings):
    """The shape this wave exists for. Ignore set off, so grouping is what
    is being measured rather than the filter in front of it."""
    settings.STAPEL_ALERTS = {"IGNORE_PATTERNS": []}
    from stapel_alerts import ignore as ignore_module

    original = ignore_module.DEFAULT_IGNORE_PATTERNS
    ignore_module.DEFAULT_IGNORE_PATTERNS = ()
    try:
        for message in PERMISSION_WARNINGS:
            record(message=message, service="svc-cdn", level="warning")
    finally:
        ignore_module.DEFAULT_IGNORE_PATTERNS = original

    assert Issue.objects.count() == 1
    assert Issue.objects.get().count == 3


def test_two_genuinely_different_errors_stay_apart():
    record(
        message="IntegrityError: duplicate key value violates unique "
                "constraint 'uniq_email'",
        service="svc-cdn",
        level="error",
    )
    record(
        message="OperationalError: could not translate host name 'db' to address",
        service="svc-cdn",
        level="error",
    )
    assert Issue.objects.count() == 2


def test_a_mapping_key_still_tells_two_payloads_apart():
    """Values are unstable; the SHAPE of the payload is the bug."""
    from stapel_alerts import normalise as norm

    same = norm.normalise_line("failed {'codename': 'add_asset'}")
    also = norm.normalise_line("failed {'codename': 'view_asset'}")
    other = norm.normalise_line("failed {'user_id': 'add_asset'}")
    assert same == also
    assert same != other
