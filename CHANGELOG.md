# Changelog

All notable changes to stapel-alerts are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.2.0] — 2026-09-14

The blind spot in the monitoring, the fact on the bus, and the six refusals
that stopped speaking English.

### The watchdog that watches the watchman

Every other input here reports a failure the fleet HAD. This one reports a
failure the fleet cannot see — and the reason it belongs in an alert store is
that a monitoring stack with a blind spot and a healthy fleet produce the same
picture: no alerts. "Nothing is firing" is the observable state of both, and
the only way to tell them apart is for something outside the stack to ask.

`manage.py alerts_watch_monitoring`, and `stapel_alerts.beat` for the beat
entry (`alerts-watch-monitoring`, every five minutes, shipped as a splat a
host merges into `CELERY_BEAT_SCHEDULE`). Six checks: `up == 0` on each
configured scrape target; a target with **no** `up` series at all — dropped
from the scrape config or renamed, which is strictly worse, because every
expression over its metrics now returns no data and an alert on no data does
not fire; `absent()` on named metrics; a dead-man's-switch alert that stopped
firing; Alertmanager's active silences; and an Alertmanager or Prometheus that
does not answer. A 200 whose body says `status != success` counts as blind,
not as an empty result — reading it as "no series" would make every check
report health at exactly the moment nothing could be seen.

Each finding is a `kind="monitoring"` issue **and** a message on the `NOTIFY`
seam, on every run that has one. Both channels, per the owner's rule: the
notification thresholds (new / regressed / spike) would be silent on a blind
spot's ninth consecutive run, which is not less urgent than its first. If the
cadence is too loud the cadence is a setting; silence is not.

A finding's message is fixed per `(check, target)` and the run's numbers live
in the context, so a scrape gap that recurs every night is one issue with a
count of ninety. A check that stops failing closes its issue with
`fixed_in_version = "recovered <ts>"` — a watchdog that can only open issues
produces a tracker full of blind spots that were fixed weeks ago. A check that
is no longer **configured** is not closed: nothing recovered, nobody looked,
and the lie would be repeated forever. An Alertmanager silence is keyed by its
matchers rather than its id, which is fresh every time somebody re-silences
the same alert.

`stapel_alerts.W003` fires when the watchdog is configured, this process runs
beat, and nothing schedules it — the one misconfiguration in this module whose
symptom is indistinguishable from health.

### Two facts on the comm bus

`alerts.issue.opened` and `alerts.issue.regressed`, with schemas in
`schemas/emits/` that core's autoloader registers and validates on every emit.
A host pages, opens a ticket or posts to a channel off these instead of
polling `GET /issues`. `regressed` carries the release that claimed the fix
next to the release that is running — the pair that separates "the fix is
wrong" from "the fix is not deployed".

Two facts, never one per occurrence: an event per occurrence would put a
failing loop's whole traffic on the bus and make every subscriber re-derive
the grouping this module already did.

They are emitted **after** the issue has committed, in a transaction of their
own, and not in the ingest's atomic block where the rest of the fleet emits.
`emit` marks its transaction rollback-only when it fails, so emitting inside
the ingest would mean a broken outbox DELETES the alert row. The bus is the
thing whose failures this store records; it has to record them on the day the
bus is what is broken.

### The six keys speak ru and es

`translations/errors.{ru,es}.json` with the shared `.state.json` provenance
sidecar, `docs/errors.{en,ru,es}.md`, and the coverage/staleness/params/
byte-stability gate in `tests/test_error_i18n.py`. Every string is a machine
translation (`origin: llm`, the gate's unreviewed counter) — the builtin
corpus carries none of these keys.

This also silences `[warning:unshipped]`, which `make contract` printed on
every emission of 0.1: a translated deployment rendered this module's
refusals in English next to everything else's Russian, and the person reading
a 403 at 3am had to work out that the mismatch was the library and not the
bug.

### The DLQ input has no fallback left

Core floor `>=0.68.1` — the release that announces a park rather than only
counting it. `connect_dlq()` is unconditional; the `try/except ImportError`
and its log-line fallback are gone. On a 0.67 core that path was reachable; on
every core this now supports it was present, untried and believed to work,
which is the shape of a gate that proves nothing.

`superseded_loggers()` **stays**, and is not vestigial: `record_parked` still
writes its ERROR line beside the signal (the line is for the human reading
container output, the signal for a listener), and `deliver_to_subscribers`
logs a handler failure it also returns. Without the supersession one park is
two issues with two fingerprints.

### Contract artifacts

`docs/capabilities.json` (six axes: topology, the fallback channel, the three
input switches, the watchdog), `docs/llms.txt`, and a generated `README.md`
assembled from `docs/readme.md` plus the artifacts — the sibling-library
pipeline, emitted by `make contract` and drift-gated by `make contract-check`
and the suite. `docs/capabilities.meta.json` is the hand-curated half.

### Requires

`stapel-core>=0.68.1,<1.0`.

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

`stapel-core>=0.67.0,<1.0` at the time. The structured DLQ input needed
`stapel_core.signals.bus_event_parked` (core 0.68.1); below that a park was
still captured, through the ERROR line `record_parked` writes. 0.2 raises the
floor and drops that fallback.
