from django.contrib import admin

from .models import ErrorEvent, Issue, Service


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("title", "service", "level", "status", "count", "last_seen")
    list_filter = ("status", "level", "service", "environment")
    search_fields = ("title", "fingerprint", "exception_class", "culprit")
    readonly_fields = ("fingerprint", "normalised_trace", "first_seen", "count")


@admin.register(ErrorEvent)
class ErrorEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "service", "level", "kind", "message")
    list_filter = ("level", "kind", "service", "environment")
    search_fields = ("message", "trace", "trace_id")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    # key_hash is deliberately not editable and the plaintext key is nowhere:
    # it exists once, in the output of `manage.py alerts_service`.
    list_display = ("name", "is_active", "created_at", "last_report_at")
    readonly_fields = ("key_hash", "created_at", "last_report_at")
