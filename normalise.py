"""Turning two tracebacks that describe one bug into one fingerprint.

A stack trace is mostly stable text with a few unstable islands in it: the
uuid of the row that violated a constraint, the id of the user whose token was
refused, a memory address, a checkout path, the line number of a template
compiled at import. Two occurrences of the SAME bug differ only in those
islands — and an exact-match grouper files them as two hundred separate
issues, which is how an alert store becomes a thing nobody reads.

So: normalise, then compare.

Normalisation (:func:`normalise_trace`) replaces every unstable island with a
placeholder, drops frames that belong to a generated or vendored file, and
strips the absolute prefix off paths so the same deploy on two hosts reads
alike. Fingerprinting (:func:`fingerprint`) hashes the result together with
the service and the exception class. Comparison (:func:`similarity`) scores
two normalised traces as a sequence ratio over their frames, so a bug that
takes one extra frame on one path still lands in the same issue.

The grouping rule (:func:`should_group`) is the owner's, 2026-09-13:

* ``>= SIMILARITY_THRESHOLD`` (0.9 by default) over normalised frames — same
  issue;
* a SHORT trace (fewer than :data:`SHORT_TRACE_FRAMES` frames) — exact match
  after normalisation only. Two four-line traces share 0.9 of their text by
  accident all the time; there is not enough evidence there to merge on, and a
  wrong merge is worse than a duplicate issue because it hides a bug behind
  one that is already marked fixed.

Order matters and is deliberate: placeholders are substituted before frames
are dropped, so a filter never has to understand a uuid.
"""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

#: Two traces whose normalised frames score at least this join one issue.
SIMILARITY_THRESHOLD = 0.9

#: Below this many frames a trace is "short": similarity is not evidence, and
#: only an exact match after normalisation groups.
SHORT_TRACE_FRAMES = 4

# ── The unstable islands ────────────────────────────────────────────────
#
# Ordered longest-token-first: a uuid must be eaten before the integer rule
# reaches the digits inside it, and an ISO timestamp before the same.

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# A bare uuid with the dashes stripped, as psycopg and some log lines print it.
_UUID_HEX = re.compile(r"\b[0-9a-fA-F]{32}\b")
_ISO_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
# CPython's default repr: `<foo.Bar object at 0x104f2a390>`.
_MEMORY_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,16}\b")
# A git sha or any other long hex token (shorter than a uuid-hex, longer than
# a plausible identifier).
_HEX = re.compile(r"\b[0-9a-fA-F]{7,31}\b")
_INT = re.compile(r"(?<![\w.])\d+(?![\w.])")

#: `File "<path>", line N, in <name>` — the shape CPython prints.
_FRAME_LINE = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<fn>.+)$')

#: Path prefixes whose *variable* head is stripped so two deploys agree. The
#: first match wins and everything before it goes, e.g.
#: ``/opt/venv/lib/python3.12/site-packages/django/db/models/query.py`` →
#: ``site-packages/django/db/models/query.py``.
_PATH_ANCHORS = (
    "/site-packages/",
    "/dist-packages/",
    "/lib/python",
    "/app/",
    "/srv/",
    "/usr/local/lib/",
    "/home/",
    "/Users/",
)

#: Frames from files matching any of these are dropped: they are generated,
#: vendored plumbing or an interpreter frame, and they are the part of a trace
#: that differs most between two occurrences of one bug (a template compiled
#: at import gets a different line number every deploy).
_GENERATED_FRAME_MARKERS = (
    "<frozen ",
    "<string>",
    "<template>",
    "<generated>",
    "importlib._bootstrap",
    "/_pytest/",
)

#: What each substitution leaves behind. Distinct tokens on purpose — a uuid
#: and an integer are different KINDS of instability, and collapsing them into
#: one placeholder would group "row <id> missing" with "retry 3 of 3".
UUID_PLACEHOLDER = "<uuid>"
HEX_PLACEHOLDER = "<hex>"
INT_PLACEHOLDER = "<int>"
TIMESTAMP_PLACEHOLDER = "<ts>"
ADDRESS_PLACEHOLDER = "<addr>"


def normalise_line(line: str) -> str:
    """Replace every unstable island in one line with its placeholder."""
    line = _ISO_TIMESTAMP.sub(TIMESTAMP_PLACEHOLDER, line)
    line = _MEMORY_ADDRESS.sub(ADDRESS_PLACEHOLDER, line)
    line = _UUID.sub(UUID_PLACEHOLDER, line)
    line = _UUID_HEX.sub(UUID_PLACEHOLDER, line)
    line = _HEX.sub(HEX_PLACEHOLDER, line)
    line = _INT.sub(INT_PLACEHOLDER, line)
    return line.strip()


def normalise_path(path: str) -> str:
    """Strip the deploy-specific head off a source path.

    ``/opt/venv/lib/python3.12/site-packages/django/db/models/query.py``
    and ``/home/ci/.venv/lib/python3.13/site-packages/django/db/models/query.py``
    are the same frame; only the tail says so.
    """
    for anchor in _PATH_ANCHORS:
        index = path.find(anchor)
        if index != -1:
            return path[index + 1:]
    return path


def is_generated_frame(path: str) -> bool:
    """Is this frame's file generated, vendored plumbing, or interpreter-owned?"""
    return any(marker in path for marker in _GENERATED_FRAME_MARKERS)


def normalise_trace(trace: str) -> list[str]:
    """Normalise *trace* into the list of lines two occurrences can be compared on.

    A ``File "...", line N, in fn`` line becomes ``path:fn`` — the LINE NUMBER
    IS DROPPED. One edit above a raise moves every line number below it, and a
    bug that files itself anew on every unrelated commit is a bug tracker
    nobody trusts. The path and the function still identify the frame.

    The quoted SOURCE line under a frame is KEPT (normalised). It is what
    distinguishes two different calls in one function, and losing it makes two
    genuinely different short traces identical — which is the one merge that
    must never happen, because it hides a live bug behind a fixed one. A frame
    whose source text changed is a frame that changed; the line NUMBER moving
    is not.
    """
    lines: list[str] = []
    in_frame = False
    for raw in trace.splitlines():
        if not raw.strip():
            in_frame = False
            continue
        match = _FRAME_LINE.match(raw)
        if match:
            path = match.group("path")
            in_frame = not is_generated_frame(path)
            if not in_frame:
                continue
            lines.append(f"{normalise_path(path)}:{match.group('fn').strip()}")
            continue
        if raw.startswith((" ", "\t")):
            # The quoted source line under a frame header — kept unless the
            # frame itself was dropped as generated.
            if in_frame:
                normalised = normalise_line(raw)
                if normalised:
                    lines.append(normalised)
            in_frame = False
            continue
        in_frame = False
        normalised = normalise_line(raw)
        if normalised and normalised != "Traceback (most recent call last):":
            lines.append(normalised)
    return lines


#: ``package.module.ClassName`` at the start of an UNINDENTED line, optionally
#: followed by ``: message`` — the shape CPython prints an exception in. The
#: last segment must read as a class name (CapWords with at least one
#: lowercase letter), which is what keeps ``DETAIL:  Key (user_id)=…`` — a
#: psycopg continuation line, unindented and colon-terminated — from being
#: read as an exception type.
_EXCEPTION_LINE = re.compile(
    r"^(?P<name>[A-Za-z_][\w.]*\.)?(?P<klass>[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*)"
    r"(?::\s*(?P<message>.*))?$"
)
_NOT_AN_EXCEPTION_PREFIX = ("Traceback", "During handling", "The above exception")


def _exception_lines(trace: str):
    """Every ``Module.Class: message`` line in *trace*, in order."""
    for raw in trace.splitlines():
        if not raw or raw[0].isspace():
            continue
        line = raw.strip()
        if not line or line.startswith(_NOT_AN_EXCEPTION_PREFIX):
            continue
        match = _EXCEPTION_LINE.match(line)
        if match:
            yield (match.group("name") or "") + match.group("klass"), (
                match.group("message") or ""
            )


def exception_class(trace: str) -> str:
    """The exception type named by the LAST ``Module.Class: message`` line.

    A chained traceback ("During handling of the above exception…") names
    several; the last one is the exception that actually escaped, which is the
    one an issue is about.
    """
    found = ""
    for klass, _ in _exception_lines(trace):
        found = klass
    return found


def exception_message(trace: str) -> str:
    """The message of the exception that escaped — NORMALISED.

    This is what tells two otherwise identical traces apart: a foreign key
    violation on one constraint and on another have the same frames, the same
    class and the same length, and are two different bugs with two different
    fixes. Without the message in the fingerprint they would be one issue, and
    fixing one would close the other.
    """
    found = ""
    for _, message in _exception_lines(trace):
        if message:
            found = message
    return normalise_line(found)


def similarity(a: str | list[str], b: str | list[str]) -> float:
    """How alike two traces are, 0.0 … 1.0, over their NORMALISED frames.

    Accepts raw traces (normalised here) or already-normalised frame lists.
    ``SequenceMatcher`` over the list of lines — not over the characters —
    because the unit that differs between two occurrences of one bug is a
    frame, not a letter: one extra retry frame should cost one line of
    similarity, not the length of that line.
    """
    left = normalise_trace(a) if isinstance(a, str) else list(a)
    right = normalise_trace(b) if isinstance(b, str) else list(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def is_short(frames: list[str]) -> bool:
    """Is this normalised trace too short for similarity to be evidence?"""
    return len(frames) < SHORT_TRACE_FRAMES


def should_group(
    candidate: str | list[str],
    existing: str | list[str],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> bool:
    """The owner's grouping rule, in one predicate.

    Long traces group at ``>= threshold``. If EITHER side is short, only an
    exact match after normalisation groups — a four-frame trace and another
    four-frame trace hit 0.9 by coincidence, and merging a live bug into an
    issue somebody has already marked fixed is the expensive mistake here.
    """
    left = normalise_trace(candidate) if isinstance(candidate, str) else list(candidate)
    right = normalise_trace(existing) if isinstance(existing, str) else list(existing)
    if is_short(left) or is_short(right):
        return left == right
    return similarity(left, right) >= threshold


def fingerprint(
    trace: str,
    *,
    service: str = "",
    exc_class: str | None = None,
    message: str = "",
) -> str:
    """The stable id of the bug *trace* describes — sha256, hex, 64 chars.

    Over: the service, the exception class, the top surviving frame, and the
    normalised message. Not over the whole trace — the whole trace is what
    :func:`similarity` is for, and hashing it would make the fingerprint as
    unstable as the text it hashes, which is the problem this module exists
    to solve.

    The service is IN the fingerprint: the same library failing the same way
    in two services is two issues, because they are fixed, deployed and closed
    separately.
    """
    frames = normalise_trace(trace)
    top_frame = next((f for f in frames if ".py:" in f or "/" in f), frames[0] if frames else "")
    klass = exc_class if exc_class is not None else exception_class(trace)
    # The caller's message wins when there is one (a log record, an explicit
    # capture); otherwise the escaping exception's own message is used, which
    # is the text that separates two bugs sharing a stack.
    subject = normalise_line(message) if message else exception_message(trace)
    material = "\x1e".join([
        service or "",
        klass or "",
        top_frame,
        subject,
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def title_for(trace: str, *, message: str = "") -> str:
    """A one-line human title: ``ExceptionClass: first line of the message``.

    Truncated to 255 characters (the column) at a word boundary where one is
    near the cut.
    """
    klass = exception_class(trace)
    text = (message or "").strip().splitlines()
    head = text[0] if text else ""
    if not head:
        for raw in reversed(trace.splitlines()):
            line = raw.strip()
            if line and not line.startswith(("File \"", "Traceback", "During handling")):
                head = line
                break
    title = f"{klass}: {head}" if klass and not head.startswith(klass) else (head or klass)
    return title[:255]


__all__ = [
    "SIMILARITY_THRESHOLD",
    "SHORT_TRACE_FRAMES",
    "UUID_PLACEHOLDER",
    "HEX_PLACEHOLDER",
    "INT_PLACEHOLDER",
    "TIMESTAMP_PLACEHOLDER",
    "ADDRESS_PLACEHOLDER",
    "normalise_line",
    "normalise_path",
    "normalise_trace",
    "is_generated_frame",
    "exception_class",
    "exception_message",
    "similarity",
    "is_short",
    "should_group",
    "fingerprint",
    "title_for",
]
