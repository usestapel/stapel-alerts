"""Fitting untrusted text into bounded columns, derived from the model.

Every string in a report arrives from another process. A reporter builds a
title out of an exception message, a culprit out of a stack frame, a
``request_path`` out of whatever a client asked for — none of those have a
length anybody promised, and all of them land in ``CharField``s. Postgres
does not truncate: it raises ``StringDataRightTruncation``, the ingest's
transaction rolls back, and the endpoint answers 500. Which is the worst
possible failure for this particular library, because the report that was
lost was a report ABOUT a defect, and the 500 makes the alert store itself
the loudest error on the host (a client fleet, 2026-09-15: eight of them in
twenty minutes, from a title longer than 255 characters).

So the rule is structural rather than per-field: **no value from a payload
reaches a bounded column unbounded**, and the bound is read off the field
itself. Typing the numbers here a second time would make a future
``max_length`` change a silent data-loss bug — the model would grow and the
truncation would not, or worse, shrink and the truncation would not.

Three functions, and the distinction between them is the point:

* :func:`fit` — descriptive text. Cut, with the cut MARKED, so a reader of
  the tracker can tell "this title is the whole message" from "this title is
  the front of a longer one". A silent truncation is a lie about the data.
* :func:`choice_or_default` — a ``choices`` field. ``level`` and ``kind`` are
  vocabularies, not prose: a value that is not in the vocabulary is not made
  to fit by cutting it, because ``"warninggggg"[:16]`` is not a level and
  ``"critical"`` truncated is not one either. It falls back to the field's
  own default, which is a value the rest of the module can reason about.
* :func:`max_length_of` — the one place the limit is read.

What is deliberately NOT fitted anywhere: ``fingerprint``. It is a sha256
hex digest, exactly 64 characters, and the column is 64 — fitting it could
only ever be a no-op, and writing the call would suggest to a reader that
truncating it is a thing that can legitimately happen. It is not: the
fingerprint is the identity of the bug, and a cut one would merge two bugs
that share a prefix.
"""
from __future__ import annotations

#: The marker that says "there was more". One character, so the arithmetic
#: below is honest: Django's ``max_length`` and Postgres' ``varchar(n)`` both
#: count CHARACTERS, so a one-character marker costs exactly one character of
#: budget whatever it encodes to in bytes.
ELLIPSIS = "…"


def max_length_of(model, field_name: str) -> int | None:
    """The declared ``max_length`` of *field_name*, or ``None`` if unbounded."""
    return model._meta.get_field(field_name).max_length


def fit(model, field_name: str, value) -> str:
    """*value* as a string that fits *field_name*, with any cut marked.

    ``None`` becomes ``""`` — every one of these columns is either
    ``blank=True, default=""`` or required, and neither wants a literal
    ``"None"``. A field with no ``max_length`` (a ``TextField``) is returned
    whole, so this is safe to call across a model without knowing which of
    its fields are bounded.
    """
    text = "" if value is None else str(value)
    limit = max_length_of(model, field_name)
    if limit is None or len(text) <= limit:
        return text
    if limit <= len(ELLIPSIS):
        # A column too small to hold the marker plus anything else. Cutting
        # to the limit is all that is left; a marker alone would be less
        # information than a prefix.
        return text[:limit]
    return text[: limit - len(ELLIPSIS)] + ELLIPSIS


def choice_or_default(model, field_name: str, value) -> str:
    """*value* if it is one of *field_name*'s choices, else the field default.

    Not a truncation: a vocabulary value that does not fit is not a long
    value, it is a wrong one, and the honest answer to a wrong one is the
    default the column already declares.
    """
    field = model._meta.get_field(field_name)
    allowed = {str(choice) for choice, _label in (field.choices or ())}
    text = "" if value is None else str(value)
    if text in allowed:
        return text
    return str(field.default)


__all__ = ["ELLIPSIS", "fit", "choice_or_default", "max_length_of"]
