from django.contrib import admin

from .models import CheckDefinition, CheckResult, CheckRun, ManagedResource


@admin.register(ManagedResource)
class ManagedResourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "service_type",
        "resource_identifier",
        "region",
        "last_overall_status",
        "last_run_at",
        "is_active",
    )
    list_filter = ("service_type", "is_active", "region", "last_overall_status")
    search_fields = ("name", "resource_identifier", "endpoint", "public_ip_address")
    readonly_fields = ("created_at", "updated_at", "last_run_at", "last_overall_status")


@admin.register(CheckDefinition)
class CheckDefinitionAdmin(admin.ModelAdmin):
    list_display = ("check_item", "service_type", "category", "priority", "frequency", "is_active")
    list_filter = ("service_type", "priority", "category", "is_active")
    search_fields = ("check_item", "code", "what_to_verify", "how_to_check")
    ordering = ("service_type", "sort_order")


class CheckResultInline(admin.TabularInline):
    model = CheckResult
    extra = 0
    can_delete = False
    readonly_fields = ("resource", "check_definition", "status", "summary", "observed_value", "created_at")


@admin.register(CheckRun)
class CheckRunAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "scope",
        "status",
        "resource_count",
        "check_count",
        "started_at",
        "finished_at",
    )
    list_filter = ("scope", "status", "service_type")
    search_fields = ("label", "summary", "error_message")
    readonly_fields = (
        "started_at",
        "finished_at",
        "resource_count",
        "check_count",
        "pass_count",
        "warn_count",
        "fail_count",
        "skip_count",
        "error_count",
    )
    inlines = [CheckResultInline]


@admin.register(CheckResult)
class CheckResultAdmin(admin.ModelAdmin):
    list_display = ("run", "resource", "check_definition", "status", "observed_value", "created_at")
    list_filter = ("status", "resource__service_type")
    search_fields = ("summary", "observed_value", "resource__name", "check_definition__check_item")
    readonly_fields = ("created_at",)

# Register your models here.
