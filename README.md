# stapel-alerts

**An alert store for a fleet that has no Sentry.**

A day spent reading a production incident out of `docker logs` is the reason
this exists. The signals were all there — a foreign key violation repeating on
three accounts, twenty-two events parked in a dead-letter queue, a JWT refusal
that turned out to be correct behaviour with a misleading log line — and every
one of them had to be found by a human grepping containers, because nothing
collected them.

This library collects them. Same interface with Sentry or without it:

```python
from stapel_alerts import capture

capture(exc)
capture("STT provider exhausted", level="error", context={"provider": "x"})
```

## Two-line mount

**A monolith** — this process owns the store:

```python
INSTALLED_APPS = [..., "stapel_alerts"]
urlpatterns = [path("alerts/", include("stapel_alerts.urls"))]
```

That is all. `STAPEL_ALERTS["SERVICE"]` names the service in the tracker, and a
system check tells you at boot if you forgot it.

**A microservice** — this process reports to the owner:

```python
INSTALLED_APPS = [..., "stapel_alerts"]          # no urlconf mount
STAPEL_ALERTS = {
    "SERVICE": "svc-billing",
    "OWNER_URL": "https://api.example.com",
    "SERVICE_KEY": "<from `manage.py alerts_service svc-billing` on the owner>",
    "FALLBACK": {"TELEGRAM_BOT_TOKEN": "...", "TELEGRAM_CHAT_ID": "..."},
}
```

Nothing else changes. Every input below is wired by `AppConfig.ready()`.

## What it captures, without you instrumenting anything

| input | what it catches |
|---|---|
| `AlertsLogHandler` | every log record at WARNING+ , rate-limited per fingerprint |
| `alerts_exception_handler` | anything the fleet exception handler answers with a 5xx |
| Celery `task_failure` | a task that failed after its retries |
| `bus_event_parked` | a DLQ park or a task-ledger `unprocessable` — work dropped |
| `deliver_to_subscribers` | a comm Action handler that raised |
| `capture(...)` | whatever a library decides is worth saying |

## Two models, because they answer two questions

**`Issue`** is the tracker: one row per bug, with `status`, `count`,
`first_seen`/`last_seen`, and the version that fixed it. It is
machine-addressable on purpose — an agent lists it, fixes the code, and closes
the issue over the API.

**`ErrorEvent`** is one occurrence: the full trace, the context (redacted
through core's redaction seam), the request path, the trace id.

## Grouping is by similarity, not by exact match

Two tracebacks that differ only in a uuid, an id, a timestamp, a memory
address or a checkout path are **one issue**. The normaliser strips those, the
fingerprint is taken over the normalised top frame plus the escaping
exception's message, and a near-duplicate scoring ≥ 0.9 over normalised frames
joins the existing issue.

A **short** trace (fewer than four frames) groups only on an exact match after
normalisation. Two four-line traces hit 0.9 by coincidence, and a wrong merge
is worse than a duplicate issue: it hides a live bug behind one somebody has
already marked fixed.

The rule is tested on real production traces, including the case that must
*not* group — a different constraint on the same table, from the same frames.

## Statuses, and where they come from

```
new ──▶ fixed ──▶ regressed
 │        ▲           │
 └── muted┘◀──────────┘
```

`fixed` is set by an API call or by CI on a commit referencing
`alerts:<issue-id>`, with the version and sha that claim the fix.
**`regressed` is never set by a caller** — the store sets it when a fixed issue
receives a new event. A caller who could assert it could also decline to.

## API

```
GET    /alerts/api/v1/issues              ?status= &level= &service= &since= &open=
GET    /alerts/api/v1/issues/{id}         + the last 20 events
PATCH  /alerts/api/v1/issues/{id}         {status, note, muted_until}
POST   /alerts/api/v1/issues/{id}/fix     {version, sha}
POST   /alerts/api/v1/report              {events: [...]}   X-Service-Key
```

Staff session for the tracker, service key for `/report`, and **not the other
way round**: a reporter's key lives in every container in the fleet, so the
blast radius of one leaking must not include everything the store has ever
recorded. JSON only, stable uuid ids, ETag on the reads.

## When the owner is unreachable

Buffer, retry with backoff, and after `FALLBACK_AFTER_MINUTES` send a digest
to the `NOTIFY` seam — Telegram by default, through stapel-notifications'
channel if that is configured, otherwise a direct Bot API call. A dead alert
store must be loud, not silent. The buffer is bounded and drops oldest first:
a reporter that ran out of memory holding alerts about an outage would be a
second outage.

## The watchdog payload

A Prometheus/Grafana blind spot is itself an issue in this tracker. The
watchdog script is a later piece; the payload it posts to `/report` is:

```json
{"events": [{
  "kind": "monitoring",
  "level": "fatal",
  "message": "prometheus has not scraped svc-billing for 15m",
  "context": {"check": "scrape_gap", "target": "svc-billing", "gap_seconds": 900}
}]}
```

`check` is the watchdog's own name for what it looked at (`scrape_gap`,
`exporter_down`, `alertmanager_silenced`), `target` the thing it could not
see. Grouping treats these like any other event, so a scrape gap that recurs
every night is one issue with a rising count, not ninety.

## Sentry

Set `SENTRY_DSN` (or `STAPEL_ALERTS["SENTRY_DSN"]`) and install the extra
(`pip install 'stapel-alerts[sentry]'`): each event is forwarded and its
Sentry id stored back on the row. Storing locally is never conditional on it.

## Retention and GDPR

`manage.py alerts_sweep` drops events past their level's retention and closed
issues with nothing left. An **open** issue is never swept, however old:
deleting it would turn "unresolved" into "never happened". `erase_subject`
(wired into the GDPR provider registry) drops the `user_id` link and scrubs
context that repeats it, and keeps the failure — an alert store's rows are the
record of a system failing, not a record about a person.

## Metrics

`alerts_new_total{level,service}` (counter) and `alerts_open_total{level,service}`
(gauge, declared at zero for every known pair, because a series that has never
existed cannot be alerted on).

---

- Settings: [CONFIG.MD](CONFIG.MD) · Extension points: [MODULE.md](MODULE.md)
- Contract: [docs/schema.json](docs/schema.json) · [docs/errors.json](docs/errors.json)
- [CHANGELOG.md](CHANGELOG.md) · MIT
