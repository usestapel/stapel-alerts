# stapel-alerts — configuration

Everything is read through `stapel_alerts.conf.alerts_settings` at call time.
Resolution per key: `settings.STAPEL_ALERTS` dict → a flat Django setting of
the same name → an environment variable → the default.

## The two mounts

```python
# A monolith that owns the store
INSTALLED_APPS = [..., "stapel_alerts"]
urlpatterns = [path("alerts/", include("stapel_alerts.urls"))]
STAPEL_ALERTS = {"SERVICE": "monolith"}

# A microservice that reports to it
INSTALLED_APPS = [..., "stapel_alerts"]
STAPEL_ALERTS = {
    "SERVICE": "svc-billing",
    "OWNER_URL": "https://api.example.com",
    "SERVICE_KEY": "...",                       # manage.py alerts_service svc-billing
    "FALLBACK": {"TELEGRAM_BOT_TOKEN": "...", "TELEGRAM_CHAT_ID": "..."},
}
```

## Keys

### Topology

| key | default | what it decides |
|---|---|---|
| `MODE` | `"auto"` | `owner` / `reporter`; `auto` = reporter iff `OWNER_URL` is set |
| `SERVICE` | `""` | this service's name in the tracker. Empty is a boot **error** |
| `OWNER_URL` | `""` | where the owner lives; `/alerts/api/v1/report` is appended |
| `SERVICE_KEY` | `""` | the per-service key, shown once at creation |
| `ENVIRONMENT` | `"production"` | recorded on every event |
| `RELEASE` | `""` | the running release, so `fixed_in` can be compared against it |

### Inputs

| key | default | what it decides |
|---|---|---|
| `LOG_LEVEL` | `"WARNING"` | the handler's floor. Below WARNING this becomes a log shipper |
| `LOG_EXCLUDE` | `["stapel_alerts", "django.db.backends", "django.request"]` | loggers never captured (loop breaker) |
| `RATE_LIMIT` | `10` | accepted captures per fingerprint per window |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | the window |
| `CAPTURE_5XX` | `True` | capture from the exception handler |
| `CAPTURE_CELERY` | `True` | connect Celery's `task_failure` |
| `CAPTURE_DLQ` | `True` | capture DLQ parks / task-ledger `unprocessable` |

### Store

| key | default | what it decides |
|---|---|---|
| `EVENTS_PER_ISSUE` | `50` | events kept per issue. Counters are never capped |
| `RETENTION_DAYS` | `{debug:3, info:7, warning:30, error:90, fatal:180}` | what `alerts_sweep` drops |
| `SIMILARITY_THRESHOLD` | `0.9` | the grouping threshold over normalised frames |
| `SIMILARITY_CANDIDATES` | `25` | recent issues compared before opening a new one |

### Notifications

| key | default | what it decides |
|---|---|---|
| `NOTIFY_USER_IDS` | `[]` | who is reached through stapel-notifications |
| `NOTIFY_TYPE` | `"alerts.issue"` | the notifications type name |
| `SPIKE_FACTOR` | `5` | hour-over-hour multiple that counts as a spike |
| `SPIKE_MIN_COUNT` | `20` | floor below which a factor means nothing |
| `QUIET_HOURS` | `None` | e.g. `{"start": 23, "end": 8, "tz": "Europe/Berlin"}`; `fatal` still goes out |

### Fallback

| key | default | what it decides |
|---|---|---|
| `NOTIFY` | `"telegram"` | the seam: `"telegram"`, `"none"`, a callable, or a dotted path to `notify(subject, body)` |
| `FALLBACK` | `{}` | `{"TELEGRAM_BOT_TOKEN": ..., "TELEGRAM_CHAT_ID": ...}` |
| `FALLBACK_AFTER_MINUTES` | `10` | how long the owner may be silent before the digest |
| `BUFFER_MAX` | `500` | bounded buffer; oldest dropped, count kept |
| `RETRY_BACKOFF` | `[1, 5, 30]` | seconds between attempts |
| `TIMEOUT_SECONDS` | `3.0` | per HTTP attempt |

### Sentry

| key | default | what it decides |
|---|---|---|
| `SENTRY_DSN` | `""` | falls back to the `SENTRY_DSN` environment variable. Needs `pip install 'stapel-alerts[sentry]'` |

## Boot checks

| id | level | when |
|---|---|---|
| `stapel_alerts.E001` | error | `SERVICE` is empty — every alert reads as "unknown" |
| `stapel_alerts.E002` | error | reporter mode with no `SERVICE_KEY` |
| `stapel_alerts.W001` | warning | reporter mode with no fallback channel |
| `stapel_alerts.W002` | warning | owner mode with an `OWNER_URL` that is ignored |

## `stapel-service.toml` example

```toml
[service]
name = "svc-billing"

[service.alerts]
mode = "reporter"
owner_url = "https://api.example.com"
# service_key comes from the environment, never from a committed file
service_key_env = "STAPEL_ALERTS_SERVICE_KEY"
fallback_telegram_chat_id = "-1001234567890"
```

Read it into settings the way the rest of the fleet does:

```python
STAPEL_ALERTS = {
    "SERVICE": svc["name"],
    "MODE": svc["alerts"]["mode"],
    "OWNER_URL": svc["alerts"]["owner_url"],
    "SERVICE_KEY": os.environ[svc["alerts"]["service_key_env"]],
    "FALLBACK": {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": svc["alerts"]["fallback_telegram_chat_id"],
    },
}
```
