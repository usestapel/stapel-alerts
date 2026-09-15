"""Settings namespace for stapel-alerts.

Everything is read through ``alerts_settings`` at call time — never a
module-level ``os.getenv`` (the value would freeze at import, and this module
is configured differently in the owner process and in every reporter).
Resolution per key: ``settings.STAPEL_ALERTS`` dict → flat Django setting of
the same name → environment variable → the default below.

The one axis that changes what this module IS:

``MODE``
    ``"owner"`` — this process holds the store. Captures are written to the
    local database and the API is served from here.
    ``"reporter"`` — this process has no store. Captures go over HTTP to
    ``OWNER_URL`` with ``X-Service-Key``, buffered and retried, and fall back
    to Telegram when the owner has been unreachable for ``FALLBACK_AFTER_MINUTES``.
    ``"auto"`` (the default) — owner if ``stapel_alerts`` is in
    ``INSTALLED_APPS`` *and* no ``OWNER_URL`` is configured; reporter otherwise.
    A monolith therefore needs no setting at all, and a microservice needs
    exactly two (``OWNER_URL`` + ``SERVICE_KEY``).
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities emitter read the axis keys without
#: re-parsing the AppSettings() call.
DEFAULTS = {
    # ── Topology ──────────────────────────────────────────────────────
    # "owner" | "reporter" | "auto". See the module docstring.
    "MODE": "auto",
    # This service's name, as it appears in the tracker. Defaults to the
    # empty string, which the module reads as "unknown service" and reports
    # as a system check — an alert whose service nobody can name is an alert
    # nobody can route.
    "SERVICE": "",
    # Reporter mode: where the owner lives, e.g. "https://api.example.com".
    # The module appends "/alerts/api/v1/report".
    "OWNER_URL": "",
    # Reporter mode: the per-service key, shown once when the Service row is
    # created (`manage.py alerts_service <name>`). Only its hash is stored.
    "SERVICE_KEY": "",
    # Deployment environment recorded on every event ("production", "stand").
    "ENVIRONMENT": "production",
    # The release this process is running, recorded on every event so a
    # `fixed_in` version can be compared against it. Usually the image tag.
    "RELEASE": "",

    # ── Inputs ────────────────────────────────────────────────────────
    # Minimum level the logging handler captures. Below WARNING an alert
    # store becomes a log shipper.
    "LOG_LEVEL": "WARNING",
    # Loggers the handler NEVER captures from, to break the loop where a
    # failure to report an alert logs an error which becomes an alert.
    "LOG_EXCLUDE": ["stapel_alerts", "django.db.backends", "django.request"],
    # Per-fingerprint rate limit: at most N captures per window, per process.
    # The issue's `count` still rises for what is dropped (the store is told
    # once per window how many occurrences it stands for), so a rate limit
    # loses detail, never the fact.
    "RATE_LIMIT": 10,
    "RATE_LIMIT_WINDOW_SECONDS": 60,
    # Capture 5xx from the fleet exception handler.
    "CAPTURE_5XX": True,
    # Capture Celery task failures (connected only if celery is importable).
    "CAPTURE_CELERY": True,
    # Capture bus DLQ parks and task-ledger unprocessable records. These are
    # the "work dropped on the floor" class and are alerts by construction.
    "CAPTURE_DLQ": True,
    # Extra message patterns (regular expressions, matched with `search`) that
    # never become an issue. ADDED to the shipped defaults in
    # stapel_alerts.ignore.DEFAULT_IGNORE_PATTERNS, never replacing them: a
    # host silencing one logger of its own must not have to restate the
    # framework chatter to keep it silenced. Applied at BOTH ends — in
    # capture() so a reporter never sends it, and in record() so the store
    # drops it at the door even from a reporter that has not been redeployed.
    "IGNORE_PATTERNS": [],
    # Exception class names that are an edge fact rather than a defect, added
    # to DEFAULT_IGNORE_EXCEPTION_CLASSES the same way. Compared on the bare
    # class name, so a dotted path and a bare name are the same answer.
    "IGNORE_EXCEPTION_CLASSES": [],

    # ── Store ─────────────────────────────────────────────────────────
    # Events kept per issue. Older ones are swept; the issue's counters are
    # not, so history survives the events that made it.
    "EVENTS_PER_ISSUE": 50,
    # Retention in days, per level. The sweep command reads this.
    "RETENTION_DAYS": {
        "debug": 3,
        "info": 7,
        "warning": 30,
        "error": 90,
        "fatal": 180,
    },
    # Grouping knobs — see normalise.py. Lowering the threshold merges more
    # aggressively; a wrong merge hides a live bug behind a fixed one.
    "SIMILARITY_THRESHOLD": 0.9,
    # How many recent open issues of the same (service, exception class) a new
    # event is compared against before it gives up and opens its own. Bounded
    # because this runs on the failure path.
    "SIMILARITY_CANDIDATES": 25,

    # ── Notifications ─────────────────────────────────────────────────
    # Who hears about a new issue / a regression / a spike. A list of user
    # ids (uuid strings) reached through stapel-notifications.
    "NOTIFY_USER_IDS": [],
    # The notifications type name used for alert mail/chat. Merged into that
    # module's TYPES registry by the host; the module only names it.
    "NOTIFY_TYPE": "alerts.issue",
    # ── What the channel carries, as opposed to what the store keeps ──
    # The notification channel is an ESCALATION path, not a mirror of the
    # store (owner's ruling 2026-09-15). The floor applies to every reason
    # below, so at the default a warning never reaches Telegram however new or
    # regressed it is: warnings live in the store and are read there. Set to
    # "warning" to page on them, "fatal" to page only on an outage.
    "NOTIFY_MIN_LEVEL": "error",
    # The three thresholds, each switchable. Paging on regressions while
    # triaging new issues in the store is a legitimate posture; so is the
    # reverse. Nothing else notifies — a repeat occurrence of a known issue
    # reaches nobody, by design.
    "NOTIFY_ON_NEW": True,
    "NOTIFY_ON_REGRESSION": True,
    "NOTIFY_ON_SPIKE": True,
    # A count spike: an issue whose occurrences in the last hour exceed this
    # multiple of the previous hour re-notifies.
    "SPIKE_FACTOR": 5,
    # Minimum occurrences in the window before SPIKE_FACTOR is even applied —
    # 1 → 5 is a factor of five and means nothing.
    "SPIKE_MIN_COUNT": 20,
    # Quiet hours, local to QUIET_HOURS_TZ: no notification is sent inside
    # [start, end) except for `fatal`. `None` disables quiet hours.
    # Example: {"start": 23, "end": 8, "tz": "Europe/Berlin"}
    "QUIET_HOURS": None,

    # ── Fallback ──────────────────────────────────────────────────────
    # The seam a reporter uses when the owner has been unreachable. Either a
    # dotted path to `notify(subject, body) -> bool`, or the shipped
    # "telegram" name, which reads FALLBACK below.
    "NOTIFY": "telegram",
    # {"TELEGRAM_BOT_TOKEN": ..., "TELEGRAM_CHAT_ID": ..., "TELEGRAM_THREAD_ID": ...}
    # Used by the shipped telegram fallback when stapel-notifications is not
    # installed or its TELEGRAM_PROVIDER is unconfigured.
    #
    # TELEGRAM_THREAD_ID is optional and is the id of a TOPIC in a forum
    # group. It is not cosmetic: Telegram routes a message into a topic only
    # when the send carries `message_thread_id`, and without it the API
    # answers 200 and posts to General. A destination that is configured and
    # silently ignored is a send that succeeded and went to the wrong place.
    "FALLBACK": {},
    # How long the owner must be unreachable before the buffer is digested to
    # the fallback channel.
    "FALLBACK_AFTER_MINUTES": 10,
    # Reporter buffer: how many events are held in memory while the owner is
    # down. Oldest are dropped past this, with a count kept, so a reporter can
    # never OOM because the alert store is the thing that is broken.
    "BUFFER_MAX": 500,
    # Retry backoff for the reporter, in seconds. Exhausted → fallback.
    "RETRY_BACKOFF": [1, 5, 30],
    # Seconds to wait on the owner's HTTP endpoint. Short: reporting an alert
    # must never become the slowest thing in a request.
    "TIMEOUT_SECONDS": 3.0,

    # ── Monitoring watchdog ───────────────────────────────────────────
    # The blind-spot watchdog (see monitoring.py). Empty PROMETHEUS_URL =
    # off, which is the right default: a library must not guess where a
    # host's Prometheus lives, and a watchdog pointed at nothing reads
    # exactly like a fleet with nothing wrong.
    #
    #   PROMETHEUS_URL   base URL, e.g. "http://prometheus:9090"
    #   TARGETS          scrape targets checked for `up == 0` and for having
    #                    no `up` series at all. A bare name is matched on
    #                    `job`; anything containing "=" is used as the label
    #                    matcher verbatim (`instance="10.0.0.4:9100"`).
    #   METRICS          metric names checked with `absent()` — the only
    #                    expression that fires when a metric stops existing.
    #   HEARTBEAT_ALERT  alertname of an always-firing dead-man's switch.
    #   ALERTMANAGER_URL base URL; active silences become issues.
    #   SERVICE          the tracker name findings are filed under; defaults
    #                    to SERVICE above.
    #   TIMEOUT_SECONDS  per HTTP request.
    #   SCHEDULED        "yes, something runs this" — silences the W003 check
    #                    for a deployment that drives it from cron or a k8s
    #                    CronJob rather than from Celery beat.
    "MONITORING": {
        "PROMETHEUS_URL": "",
        "TARGETS": [],
        "METRICS": [],
        "HEARTBEAT_ALERT": "",
        "ALERTMANAGER_URL": "",
        "SERVICE": "",
        "TIMEOUT_SECONDS": 5.0,
        "SCHEDULED": False,
    },

    # ── Sentry ────────────────────────────────────────────────────────
    # When set (or when SENTRY_DSN is in the environment), each event is also
    # forwarded to Sentry and the returned id stored on the event. Storing
    # locally is NOT conditional on this: the same interface either way.
    "SENTRY_DSN": "",
}

alerts_settings = AppSettings(
    "STAPEL_ALERTS",
    defaults=DEFAULTS,
    import_strings=(),
)

__all__ = ["alerts_settings", "DEFAULTS"]
