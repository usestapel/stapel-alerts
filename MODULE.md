# stapel-alerts — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same commit as any change
> to a seam. See also README.md, CONFIG.MD and CHANGELOG.md.

## What this module provides

- **`Issue` / `ErrorEvent`** — the tracker and the occurrences. `Issue` is one
  bug (fingerprint, service, status, counters, first/last seen, `fixed_in_*`,
  `regressed_at`); `ErrorEvent` is one occurrence with the full trace and the
  redacted context. `Service` is the registry of processes allowed to report,
  each with a key stored only as a sha256.
- **A normaliser and a grouping rule** (`normalise.py`) — uuid/hex/int/
  timestamp/address substitution, path-head stripping, generated-frame
  dropping, `fingerprint()`, `similarity()` and `should_group()`. The
  threshold is 0.9 over normalised frames; a trace under four frames groups
  only on an exact match.
- **Inputs that need no host code** — a WARNING+ logging handler, a DRF
  exception handler, Celery's `task_failure`, core's `bus_event_parked` (DLQ
  parks and task-ledger `unprocessable`), and a wrapper around
  `deliver_to_subscribers`. All wired in `AppConfig.ready()`.
- **Two reporting paths** — in-process when this service owns the store, HTTP
  with `X-Service-Key` when it does not, plus a bounded buffer, backoff retry
  and a Telegram fallback.
- **An API for agents** — list/detail/patch/fix, JSON only, ETag, stable ids.
- **Notifications, metrics, retention, GDPR erasure, Sentry export.**

**Why an alert store rather than a log shipper.** A log shipper answers "what
did the system say"; a tracker answers "which bugs are open, which are fixed,
and which came back". The second question is the one a fix wave is
reconciled against, and it needs a row per bug with a status — not a search
over text.

## Extension points (fork-free)

### 1. `STAPEL_ALERTS["MODE"]` — topology (axis)

`"auto"` (default) reads the presence of `OWNER_URL`: a monolith configures
nothing, a microservice configures the two settings it obviously needs.
`"owner"` / `"reporter"` force it. This is the only axis that changes what the
module *is* rather than what it captures.

### 2. `STAPEL_ALERTS["NOTIFY"]` — the fallback channel (import-string seam)

A dotted path to `notify(subject, body) -> bool`, a callable, `"telegram"`
(the shipped sender), or `"none"`. This is where a deployment plugs in
whatever it actually reads at 3am. `"telegram"` prefers stapel-notifications'
`TELEGRAM_PROVIDER` when that is configured, and falls back to a direct Bot
API call from `STAPEL_ALERTS["FALLBACK"]` — deliberately, because in a small
fleet the notifications module lives in the alerts owner, and a fallback that
needs the thing that is down is not a fallback.

### 3. `alerts_exception_handler` — the 5xx input (settings seam)

```python
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "stapel_alerts.inputs.alerts_exception_handler",
}
```

It delegates to core's `stapel_exception_handler` — the envelope stays core's,
the response a client sees does not change — and captures what came back as a
5xx or as `None`. A host that would rather not replace the handler can call
`stapel_alerts.inputs.capture_exception(exc, context)` from its own.

### 4. Serializer seams — `SerializerSeamMixin`

Every view exposes `request_serializer_class` / `response_serializer_class`.
Subclass the view, set the attribute, remount the URL.

### 5. The input switches — `CAPTURE_5XX`, `CAPTURE_CELERY`, `CAPTURE_DLQ`,
`LOG_LEVEL`, `LOG_EXCLUDE`

Each input is individually switchable. `LOG_EXCLUDE` is the loop-breaker:
records from `stapel_alerts.*` are never captured, because a failure to report
an alert logs an error which becomes an alert which fails to report.

### 6. Grouping knobs — `SIMILARITY_THRESHOLD`, `SIMILARITY_CANDIDATES`

Lower the threshold to merge more aggressively. Think twice: a wrong merge
hides a live bug behind one marked fixed, and that failure is silent.

## Mechanisms that exist — do not rebuild them

- **`superseded_loggers()`.** Core logs a DLQ park *and* (from 0.68.1)
  announces it; a comm handler failure is both logged and returned. When the
  structured input is connected it registers the logger it supersedes, so the
  log line does not open a second issue with a different fingerprint. If you
  add an input that duplicates a log line, register it the same way.
- **The rate limiter carries what it dropped.** A suppressed occurrence is
  folded into the next accepted event's `occurrences`. Do not "fix" the
  limiter by counting rows — the count is the fact, the events are the sample.
- **`Service.authenticate` looks up by hash**, so the plaintext key never
  reaches a slow-query log. There is no code path that reads a key back.
- **`services.record` is the only writer.** It does grouping, the regression
  flip, the event cap, the Sentry forward and the post-commit hooks in one
  transaction. Creating an `Issue` or an `ErrorEvent` directly skips all five.
- **`capture()` cannot raise, and is re-entrancy guarded.** Every caller is on
  a failure path.

## What this module deliberately does NOT do

- **It does not report over the bus.** The bus is the thing whose failures it
  records; a reporting path that dies with it is a smoke detector wired to the
  burning fuse box. Microservices report by HTTP, monoliths in process.
- **It does not create a shadow user row.** `user_id` is a plain uuid with no
  FK, precisely so an alert about an account this database has never heard of
  can still be recorded.
- **It does not let a caller set `regressed`.**
- **It does not capture a 400.** A validation error is the API working.
- **It does not notify on every occurrence.** New issue, regression, and count
  spike — nothing else.

## Comm and core seams used

| seam | why |
|---|---|
| `stapel_core.conf.AppSettings` | the `STAPEL_ALERTS` namespace |
| `stapel_core.signals.bus_event_parked` | DLQ / task-ledger input (core ≥ 0.68.1) |
| `stapel_core.comm.actions.deliver_to_subscribers` | comm handler failures |
| `stapel_core.django.api.errors` | the fleet error envelope + error registry |
| `stapel_core.django.api.permissions.IsStaffUser` | the tracker wall |
| `stapel_core.observability.metrics` | `alerts_new_total` / `alerts_open_total` |
| `stapel_core.observability` `REDACT_FIELDS` | context redaction |
| `stapel_core.observability.trace_ids` | correlation on every event |
| `stapel_core.gdpr.GDPRProvider` | `erase_subject` |
| `stapel_notifications` (optional) | email/chat + the telegram channel |
| `sentry_sdk` (optional extra) | the export |

## Not yet here (0.2 candidates)

- `@stapel/alerts-react` — the feed pair.
- The Prometheus blind-spot watchdog script itself (the payload it posts is
  documented in README.md and accepted today).
- A comm Action so a host can subscribe to `alerts.issue.opened`.
- An i18n errors catalog (the six owned keys render as English fallbacks).
- Per-issue assignment and a "who is looking at this" field.
