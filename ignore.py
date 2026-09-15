"""What never becomes an issue, however loudly it is logged.

An alert store's whole value is that somebody reads it. The fastest way to
destroy that is to fill it with things that are not defects of the deployment
being watched — and the broadest input, a WARNING+ handler on the root
logger, catches exactly those by construction:

* **framework bootstrap chatter.** Django's permission/ContentType sync logs
  ``Could not add permission {...}: ContentType matching query does not
  exist`` at WARNING while a migration is mid-flight. It is the framework
  talking to itself about a state it is about to leave.
* **the internet.** ``Invalid HTTP_HOST header`` is a scanner typing a raw
  address, not a defect of ours. Django logs it at ERROR with a traceback of
  its own request handling, which makes it look like a bug and rank like one.

Owner's ruling (2026-09-15), after the first hour of the tracker running on
a client fleet produced four issues of which four were noise: ship the
ignore set as a DEFAULT, extendable by configuration, and apply it at both
ends — in :func:`stapel_alerts.capture` so a reporter never sends it, and in
:func:`stapel_alerts.services.record` so the store drops it at the door even
when a reporter running an older release still does.

Both ends, because those are two different failures. A reporter that filters
saves the request; a store that filters is the one guarantee that does not
depend on every container in the fleet having been redeployed.

Patterns are matched with :func:`re.search` against the message, so a bare
substring works as a pattern and an anchored one is available when it is
wanted. They are compiled once per distinct pattern list and re-read when the
setting changes.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Message patterns that are never a defect of the deployment. Extended, not
#: replaced, by ``STAPEL_ALERTS["IGNORE_PATTERNS"]`` — a host adding one of
#: its own must not have to restate these to keep them.
DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    # Django's permission/ContentType bootstrap, mid-migration.
    r"Could not add permission",
    r"ContentType matching query does not exist",
    r"Auth permissions? .* not created",
    # A scanner, or a monitoring probe, reaching the host by raw address.
    # This also covers this library's OWN proof traffic by construction: the
    # only safe way to prove an ingest end to end from outside is to make a
    # request that is refused, and the refusal that costs nothing is this one.
    r"Invalid HTTP_HOST header",
)

#: Exception classes that are an edge FACT rather than an application error.
#: ``DisallowedHost`` is a ``SuspiciousOperation``: the request was correctly
#: refused, and Django's own traceback of refusing it is not a stack worth
#: keeping.
DEFAULT_IGNORE_EXCEPTION_CLASSES: tuple[str, ...] = (
    "DisallowedHost",
    "SuspiciousOperation",
)

_CACHE: dict[tuple[str, ...], tuple] = {}


def _compiled(patterns: tuple[str, ...]) -> tuple:
    cached = _CACHE.get(patterns)
    if cached is None:
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error:
                logger.warning(
                    'alerts: STAPEL_ALERTS["IGNORE_PATTERNS"] entry %r is not a '
                    "valid regular expression and is being skipped.",
                    pattern,
                )
        cached = tuple(compiled)
        _CACHE[patterns] = cached
    return cached


def _configured(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    from .conf import alerts_settings

    extra = tuple(str(item) for item in (getattr(alerts_settings, name, None) or ()))
    return defaults + extra


def is_ignored(message: str = "", *, exc_class: str = "", logger_name: str = "") -> bool:
    """Is this occurrence something the store must not open an issue for?

    Checked against the exception class first — it is an exact comparison and
    the cheapest of the three — then against the message patterns. The logger
    name is folded into the text the patterns see, so a host can silence a
    whole noisy logger with one entry without a second setting to learn.
    """
    if exc_class:
        # The bare class name, so `django.core.exceptions.DisallowedHost` and
        # `DisallowedHost` are the same answer.
        bare = exc_class.rsplit(".", 1)[-1]
        if bare in _configured(
            "IGNORE_EXCEPTION_CLASSES", DEFAULT_IGNORE_EXCEPTION_CLASSES
        ):
            return True

    haystack = message or ""
    if logger_name:
        haystack = f"{logger_name} {haystack}"
    if not haystack.strip():
        return False
    for pattern in _compiled(_configured("IGNORE_PATTERNS", DEFAULT_IGNORE_PATTERNS)):
        if pattern.search(haystack):
            return True
    return False


__all__ = [
    "is_ignored",
    "DEFAULT_IGNORE_PATTERNS",
    "DEFAULT_IGNORE_EXCEPTION_CLASSES",
]
