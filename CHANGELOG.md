# Changelog

All notable changes to stapel-alerts are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.2.4] — 2026-09-20

### `from stapel_alerts import capture` handed back a module, and the fleet's alerts stopped existing

The one function this library has was defined in a submodule with the same
name as the export. `__init__.py` exports lazily (PEP 562), and PEP 562's
`__getattr__` runs only when normal attribute lookup FAILS — so the moment
anything in the process imported the submodule `stapel_alerts.capture`, Python
bound the module object onto the package attribute and the export was gone for
the life of that process. `from stapel_alerts import capture` then returned a
module and `capture(...)` raised `TypeError: 'module' object is not callable`.

Deterministically, under two import orders in a clean interpreter:

```
from stapel_alerts import capture            # -> <class 'function'>
```
```
import stapel_alerts.capture
from stapel_alerts import capture            # -> <class 'module'>   (0.2.3)
```

And the second order is the one a real deployment takes, not the rare one: the
log handler this package installs in `AppConfig.ready` does
`from .capture import capture` inside `emit()`, so every service that installed
the app bound the submodule on its FIRST WARNING record — before any library
reached for the export. A caller of an alert library is on a failure path and
guards the call, so nothing crashed. The alerts simply were not filed, and no
symptom said so. Found on a production host where a "LLM provider out of
credits" alert had reached the tracker zero times while the provider had been
refusing for two days.

The suite could not see it: `conftest.py` imported the submodule first as well,
so every test in this repo ran in the working order.

**The module `stapel_alerts.capture` is now `stapel_alerts._capture`.** The
public name is and always was the FUNCTION `stapel_alerts.capture`, which is
now the callable under every import order. There is no compatibility shim,
deliberately: a module that keeps the old name back is the defect. A sweep of
the fleet found no importer of the old module path outside this repo, and an
importer that does exist now gets a loud `ModuleNotFoundError` at import time
instead of a silent `TypeError` on a failure path. If you imported
`stapel_alerts.capture` as a module, import `stapel_alerts._capture` — or,
better, the export: `from stapel_alerts import capture`.

`tests/test_import_surface.py` is the gate that keeps it fixed: it asserts the
export is callable under three import orders in clean subprocesses, and fails
if ANY name in `__all__` is also the name of a submodule.

## [0.2.3] — 2026-09-16

Adopting 0.2.2 on the same fleet found two more, and both are the shape this
library keeps producing: a mechanism that was right for a monolith and wrong
the moment the app also runs in a process that does not own the store.

### A reporter certified GDPR erasure over tables it does not have

`AppConfig.ready` registered `AlertsGDPRProvider` unconditionally, so every
REPORTER registered it too. stapel-gdpr infers an owner's kind rather than
taking a declaration — local iff a provider with that section is registered in
this process — so a reporter made itself the LOCAL owner of a store held
somewhere else. An account closure would run `erase_subject` over the
reporter's own empty alerts tables, return a receipt, and leave the real rows,
the ones carrying `user_id`, untouched in the owner's database.

The deployment that hit this had no honest answer available: name it and
certify nothing, opt out and claim the store holds no personal data, or list it
as a remote owner nobody answers and have erasure time out in silence. The
defect was here, so the fix is here — the provider registers only in owner
mode. A process that cannot erase does not claim it can.

### The wire serializer refused what the store was about to fit

0.2.2 bounded the columns and left `max_length` on the report serializer, so a
batch carrying a 4000-character `request_path` — a URL a CLIENT chose — was
refused whole with a 400 naming `request_path` and `release`. Every genuine
alert travelling in that batch was lost to protect a column `bounds` was about
to truncate anyway. Measured against the live store, which answered
`error.400.alerts_invalid_report` to the exact payload 0.2.2 was released to
accept.

A reporter is not a user agent filling in a form; it is a process on a failure
path handing over the only record of a defect, and the store's job is to keep
that record rather than grade the submission. Length is now repaired, never
refused. What cannot be repaired — a `user_id` that is not a uuid, an
`occurrences` below 1 — is still refused, because coercing those would be
inventing data.

### `title_for` truncated a second time, silently

It ended in `title[:255]`, in front of the marked cut `bounds.fit` makes — so a
long title was stored shortened with no ellipsis and a reader could not tell
whether they were seeing the whole message. Two places that both know the
column's width is one too many. The normaliser produces the text; `bounds` is
the single boundary that makes it fit and says so.

## [0.2.2] — 2026-09-15

Four defects, all found within the first hour of the first real mount — an
eight-service fleet on a client fleet. Every one of them is the same shape:
the library worked, and what it produced was not usable.

### `POST /report` answered 500 on a long title

A title longer than 255 characters reached a `varchar(255)`. Postgres does not
truncate, it raises `StringDataRightTruncation`, the ingest's transaction rolled
back, and the endpoint answered 500 — eight times in twenty minutes. That is
the worst available failure for this library: the report that was lost was a
report ABOUT a defect, and the 500 made the alert store the loudest error on
the host, with every other service a retrying, logging client complaining
about it.

Fixed structurally rather than on the field that blew up. `stapel_alerts.bounds`
reads each limit off the model field itself — typing the numbers a second time
would make a future `max_length` change a silent data-loss bug — and `record`
fits every value from a payload before it reaches a column. Cuts are MARKED
with an ellipsis, because a silent truncation is a lie about the data. A
`choices` field (`level`, `kind`) falls back to its default instead of being
cut into nonsense: `"screaming"[:16]` is not a level.

The ordering matters and is the subtle half: `service` and `exception_class`
are fitted BEFORE the fingerprint is taken, so the hash is computed over
exactly the values that will be stored. Truncation is deterministic, so two
identical errors still truncate identically and still group — the property the
whole arrangement exists to protect. The fingerprint itself is never fitted.

And the endpoint no longer 500s on anything: a report it cannot store answers
422 `error.422.alerts_report_not_storable` and is logged locally.

### The channel was a mirror of the store, not an escalation of it

Every first-seen issue was announced, warnings included — so the channel
duplicated the thing it exists to escalate, and a channel that repeats the
store is one people mute. Owner's ruling: Telegram carries only what should
wake a person. `NOTIFY_MIN_LEVEL` (default `error`) is a floor on every
reason, not just on new issues — a warning that regressed is still a warning —
and the three thresholds (`NOTIFY_ON_NEW`, `NOTIFY_ON_REGRESSION`,
`NOTIFY_ON_SPIKE`) are configuration rather than constants.

### The noise it did announce was not ours

Four issues in the first hour, and four of them were Django's own
permission/ContentType bootstrap chatter and an internet scanner's
`Invalid HTTP_HOST header`. `stapel_alerts.ignore` ships a default set for
both, extended (never replaced) by `IGNORE_PATTERNS` and
`IGNORE_EXCEPTION_CLASSES`, and applied at BOTH ends: in `capture` so a
reporter never spends an HTTP request on it, and in `record` so the store
drops it at the door even from a reporter that has not been redeployed.

### Grouping failed on the shape it exists for

Three permission warnings differing by one word — `add_` / `change_` /
`view_` inside an identical template — became three issues. The normaliser
absorbed varying NUMBERS and not varying WORDS; that was the known limit when
0.2 shipped and this was it biting.

Now a mapping literal printed into a message has its VALUES replaced and its
KEYS kept, so `{'app_label': …}` and `{'user_id': …}` stay different payloads.
Deliberately scoped to braces and nothing else: the first version of this rule
was global, and it folded two different unique-constraint violations into one
issue — same frames, same class, two different bugs, one hidden behind the
other's "fixed". That is `test_a_different_constraint_is_a_different_issue`,
the case the README leads with, and the suite caught it. A quoted identifier
standing alone in a message is usually what TELLS two bugs apart; inside a
printed dict it is usually what varies between two occurrences of one.

### The Telegram digest went to General instead of the topic

A forum group routes a message into a topic only when the send carries
`message_thread_id`. The sender did not, so every digest landed in the group's
General tab while the deployment's Grafana contact point — which does send the
parameter — had been posting into the topic all along. The chat id was never
wrong. `FALLBACK["TELEGRAM_THREAD_ID"]` is now read and passed through, as an
integer, and offered to stapel-notifications' channel when its signature
accepts one (feature-detected, so an older channel keeps working).

## [0.2.1] — 2026-09-14

Four defects the frontend pair (`@stapel/alerts-react` 0.1.0) found while
consuming `docs/schema.json`. No wire change; the contract now tells the truth
about the wire, and a gate proves it does.

### The schema said `Issue[]`; the wire carried a page

`GET /issues` has always answered `{count, offset, limit, results}`. The schema
declared `Issue[]`, because the view's `@extend_schema(responses=
IssueSerializer(many=True))` was a hand-written claim the generator had no way
to check against the method body — and the drift gate compared the committed
schema with a fresh emission of the same claim, so it was green while both
were wrong. The response is now a named `IssuePage` component, and the view
renders its body THROUGH `IssuePageSerializer` and declares the same object,
so the contract and the wire are one thing.

The mechanism that catches the class: `tests/test_contract_wire.py` performs
every operation the committed schema declares with a JSON response, on every
interpreter, and validates the body it gets against the schema it was
promised. An operation with a response body and no recipe fails loudly
rather than being skipped. The operationIds the pair's client is keyed on
(`alerts_api_v1_issues_list` / `_retrieve`) are pinned by a test; the list is
named explicitly, because drf-spectacular derives `_list` only from a
`many=True` response and a page envelope is one object.

### `?limit=`

The page size was a server constant. It is now an optional `limit` query
parameter, `1..200`, default 50 (`views.PAGE_SIZE` / `views.MAX_PAGE_SIZE`).
Out-of-range values are clamped and the envelope echoes the limit that was
applied, so a clamp is never silent; garbage is the default.

### A mute deadline alone is a mute

`PATCH /issues/{id}` with only `muted_until` answered 200 and wrote nothing
unless the patch also carried `status`. A 200 that changes nothing is a lie.
`muted_until` has no meaning in any other status, so a patch carrying only
the deadline now sets `status=muted` (`null` mutes with no deadline); with any
status other than `muted` the deadline is ignored and cleared.

### Leaving `muted` drops the deadline

`set_status` never cleared `muted_until`, so an issue reopened or fixed after
a mute kept a stale deadline that read as "muted" to anything checking the
timestamp before the status. Every transition out of `muted` — `set_status`
to `new`/`fixed`, and `mark_fixed` — now clears it.

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
