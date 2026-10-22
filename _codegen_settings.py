"""Single-module Django settings for stapel-alerts' harnesses.

One copy of the ``settings.configure(...)`` block, shared by the pytest suite
(``conftest.py``, mounted on the bare test urlconf) and the contract-emission
harness (``_codegen.py`` / ``make contract``, mounted on the canonical
``alerts/api/v1/`` prefix). Keeping it here means the two can never drift in
their INSTALLED_APPS or their mock configuration (contract-pipeline.md §3).
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_alerts.tests.urls",
    contract: bool = False,
) -> dict:
    """``settings.configure(**kwargs)`` for a single-module alerts instance."""
    if contract:
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            "stapel_alerts",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
        },
        STAPEL_ALERTS={
            "SERVICE": "svc-test",
            # The suite drives the limiter explicitly where it is the subject;
            # everywhere else it must not swallow the second event of a test.
            "RATE_LIMIT": 1000,
        },
        MIGRATION_MODULES={
            "users": None,
            "alerts": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


#: The common path prefix drf-spectacular auto-detects in a multi-module
#: aggregate. Forced on the singleton by the harness so a single-module
#: instance derives the same style of operationIds.
CODEGEN_SCHEMA_PATH_PREFIX = "/"


def enum_name_overrides() -> dict:
    """drf-spectacular ``ENUM_NAME_OVERRIDES`` for this module's contract.

    The issue status set is carried by two fields (the full model choices on
    reads, the settable subset on PATCH), so drf-spectacular falls back to a
    collision name (``Status295Enum``) that changes with unrelated edits and
    generator versions, renaming the react pair's generated type. Both sets
    are named here. Called after ``django.setup()``.
    """
    from stapel_alerts.models import IssueStatus
    from stapel_alerts.serializers import SETTABLE_STATUSES

    return {
        "IssueStatusEnum": IssueStatus,
        "IssuePatchStatusEnum": [s.value for s in SETTABLE_STATUSES],
    }
