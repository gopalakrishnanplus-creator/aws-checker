from django.db import models
from django.urls import reverse
from django.utils import timezone


class HealthStatus(models.TextChoices):
    PASS = "pass", "Pass"
    WARN = "warn", "Warn"
    FAIL = "fail", "Fail"
    SKIP = "skip", "Skipped"
    ERROR = "error", "Error"


class ManagedResource(models.Model):
    class ServiceType(models.TextChoices):
        EC2 = "ec2", "EC2"
        RDS = "rds", "RDS"
        S3 = "s3", "S3"

    account_id = models.CharField(max_length=32, default="736616688306")
    service_type = models.CharField(max_length=10, choices=ServiceType.choices)
    name = models.CharField(max_length=255)
    resource_identifier = models.CharField(max_length=255)
    region = models.CharField(max_length=32)
    availability_zone = models.CharField(max_length=32, blank=True)
    endpoint = models.CharField(max_length=255, blank=True)
    public_ip_address = models.GenericIPAddressField(null=True, blank=True)
    elastic_ip = models.GenericIPAddressField(null=True, blank=True)
    port = models.PositiveIntegerField(null=True, blank=True)
    engine = models.CharField(max_length=64, blank=True)
    resource_state = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    check_config = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_overall_status = models.CharField(
        max_length=10,
        choices=HealthStatus.choices,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service_type", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_type", "resource_identifier"],
                name="unique_service_resource_identifier",
            )
        ]

    def __str__(self):
        return f"{self.get_service_type_display()} | {self.name}"

    def get_absolute_url(self):
        return reverse("checker:resource-detail", args=[self.pk])

    @property
    def status_label(self):
        return self.last_overall_status or "not-run"

    @property
    def endpoint_or_ip(self):
        return self.endpoint or self.public_ip_address or "-"


class CheckDefinition(models.Model):
    class Priority(models.TextChoices):
        P1 = "P1", "P1"
        P2 = "P2", "P2"
        P3 = "P3", "P3"

    service_type = models.CharField(max_length=10, choices=ManagedResource.ServiceType.choices)
    sort_order = models.PositiveSmallIntegerField(default=1)
    category = models.CharField(max_length=64)
    code = models.SlugField(max_length=100)
    check_item = models.CharField(max_length=255)
    what_to_verify = models.TextField()
    how_to_check = models.TextField()
    success_criteria = models.TextField()
    priority = models.CharField(max_length=2, choices=Priority.choices)
    frequency = models.CharField(max_length=32)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service_type", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_type", "code"],
                name="unique_check_code_per_service",
            )
        ]

    def __str__(self):
        return f"{self.get_service_type_display()} | {self.check_item}"


class CheckRun(models.Model):
    class Scope(models.TextChoices):
        RESOURCE = "resource", "Single resource"
        SERVICE = "service", "Single service"
        ALL = "all", "All services"

    scope = models.CharField(max_length=12, choices=Scope.choices)
    label = models.CharField(max_length=255)
    service_type = models.CharField(
        max_length=10,
        choices=ManagedResource.ServiceType.choices,
        blank=True,
    )
    resource = models.ForeignKey(
        ManagedResource,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="runs",
    )
    status = models.CharField(max_length=10, choices=HealthStatus.choices, default=HealthStatus.WARN)
    resource_count = models.PositiveIntegerField(default=0)
    check_count = models.PositiveIntegerField(default=0)
    pass_count = models.PositiveIntegerField(default=0)
    warn_count = models.PositiveIntegerField(default=0)
    fail_count = models.PositiveIntegerField(default=0)
    skip_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    summary = models.TextField(blank=True)
    error_message = models.TextField(blank=True)
    initiated_from = models.CharField(max_length=50, default="ui", blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"{self.label} ({self.started_at:%Y-%m-%d %H:%M:%S})"

    @property
    def duration_seconds(self):
        if not self.finished_at:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 2)


class CheckResult(models.Model):
    run = models.ForeignKey(CheckRun, on_delete=models.CASCADE, related_name="results")
    resource = models.ForeignKey(
        ManagedResource,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="check_results",
    )
    check_definition = models.ForeignKey(
        CheckDefinition,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="results",
    )
    status = models.CharField(max_length=10, choices=HealthStatus.choices)
    summary = models.TextField()
    observed_value = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["resource__name", "check_definition__sort_order", "id"]

    def __str__(self):
        if self.check_definition:
            return f"{self.check_definition.check_item}: {self.status}"
        return f"Check result: {self.status}"
