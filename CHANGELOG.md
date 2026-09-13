# Changelog

All notable changes to stapel-alerts are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.1.0] — 2026-09-13

First release. An alert store for a fleet that has no Sentry — and the same
`capture()` call whether one is connected or not.

### Why

A production incident read out of container logs, one day, by hand. Every
signal was already there: a foreign key violation repeating on three accounts,
twenty-two events parked in a dead-letter queue, and a JWT refusal that turned
out to be correct behaviour behind a log line naming a cause it had never
checked. Nothing collected any of it, so finding it cost a day and the
counting was limited to what `docker logs --since` could still reach.

### The tracker

**Two models.** `Issue` — one row per bug: fingerprint, service, environment,
level, status, `count`, `count_since_fix`, first/last seen, `fixed_in_version`
/ `fixed_in_sha`, `regressed_at`. `ErrorEvent` — one occurrence: the full
trace, the redacted context, the request path, the trace id, the user id as a
plain uuid with no foreign key. `Service` — who may report, with the key
stored only as a sha256.

**Statuses come from evidence.** `new` → `fixed` (an API call or CI, carrying
the version and sha that claim the fix) → `regressed`, which **only the store
sets**, when a fixed issue receives a new event. A caller able to assert a
regression would be a caller able to decline to.

### Grouping

Normalise, then compare. uuids, bare hex uuids, long hex tokens, integers, ISO
timestamps and memory addresses become placeholders; a deploy-specific path
head is stripped; generated and interpreter frames are dropped; a frame's LINE
NUMBER is dropped and its source line kept. Fingerprint over service +
exception class + top frame + the escaping exception's normalised message.
A near-duplicate scoring ≥ 0.9 over normalised frames joins the existing issue.

A trace under four frames groups only on an exact match after normalisation.
Two four-line traces reach 0.9 by coincidence, and a wrong merge hides a live
bug behind one somebody has already closed.

Tested on real production traces, including the case that must NOT group: a
different constraint on the same table, raised from the same frames, with the
same exception class and the same length.

### Inputs, none of which a host wires

A WARNING+ logging handler (rate-limited per fingerprint, with what it dropped
carried into the next accepted event's `occurrences`); a DRF exception handler
for 5xx that delegates the envelope to core; Celery's `task_failure`; core's
`bus_event_parked` for DLQ parks and task-ledger `unprocessable` records;
a wrapper around `deliver_to_subscribers` that reports the exceptions it
RETURNS — the ones no `except` clause anywhere will ever see; and explicit
`capture()`.

An input that duplicates a log line registers the logger it supersedes, so one
park is one issue rather than two with different fingerprints.

### Reporting by topology

Monoliths call the store in process. Microservices `POST /alerts/api/v1/report`
with `X-Service-Key`, retried with backoff, buffered (bounded, oldest dropped)
while the owner is down, and digested to the `NOTIFY` seam — Telegram by
default, through stapel-notifications' channel when that is configured and a
direct Bot API call otherwise — once the owner has been unreachable for
`FALLBACK_AFTER_MINUTES`. Not the bus, in either direction: the bus is the
thing whose failures this records.

### API

`GET /issues` (status/level/service/since/open filters, newest activity first,
ETag), `GET /issues/{id}` with the last events, `PATCH /issues/{id}`,
`POST /issues/{id}/fix`, `POST /report`. Staff session for the tracker,
service key for reports, and not the other way round — a reporter's key lives
in every container in the fleet.

`POST /report` accepts `kind="monitoring"`, the Prometheus blind-spot
watchdog's entry: a monitoring stack that cannot see something is itself an
issue in the tracker.

### The rest

Threshold notifications (new issue, regression, count spike — never a repeat
occurrence) with quiet hours, off by default. `alerts_new_total` and
`alerts_open_total`, the gauge declared at zero for every known pair so an
alert expression has a series to fire on. `manage.py alerts_sweep` (an OPEN
issue is never swept, however old). `erase_subject` through the GDPR seam,
which drops the link and keeps the failure. Sentry export behind an optional
extra, storing the event id back and never conditioning the local row on it.

### Requires

`stapel-core>=0.67.0,<1.0`. The structured DLQ input needs
`stapel_core.signals.bus_event_parked` (core 0.68.1); below that a park is
still captured, through the ERROR line `record_parked` writes.
