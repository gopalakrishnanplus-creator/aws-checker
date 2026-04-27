from __future__ import annotations

import socket
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urljoin

import boto3
import pymysql
import psycopg2
import requests
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from checker.models import CheckDefinition, CheckResult, CheckRun, HealthStatus, ManagedResource
from checker.services.pjt_integration import PJTIntegrationClient


@dataclass
class CheckOutcome:
    status: str
    summary: str
    observed_value: str = ""
    details: dict = field(default_factory=dict)


class CheckRunner:
    def __init__(self):
        self._sessions = {}

    def run_resource(self, resource: ManagedResource):
        return self._run(
            resources=[resource],
            scope=CheckRun.Scope.RESOURCE,
            service_type=resource.service_type,
            resource=resource,
            label=f"{resource.get_service_type_display()} run for {resource.name}",
        )

    def run_service(self, service_type: str):
        service_type = service_type.lower()
        resources = list(
            ManagedResource.objects.filter(service_type=service_type, is_active=True).order_by("name")
        )
        service_label = dict(ManagedResource.ServiceType.choices).get(service_type, service_type.upper())
        return self._run(
            resources=resources,
            scope=CheckRun.Scope.SERVICE,
            service_type=service_type,
            label=f"{service_label} bulk run",
        )

    def run_all(self):
        resources = list(ManagedResource.objects.filter(is_active=True).order_by("service_type", "name"))
        return self._run(resources=resources, scope=CheckRun.Scope.ALL, label="Full AWS bulk run")

    def _run(self, *, resources, scope, label, service_type="", resource=None):
        run = CheckRun.objects.create(
            scope=scope,
            label=label,
            service_type=service_type,
            resource=resource,
            started_at=timezone.now(),
            status=HealthStatus.WARN,
        )
        totals = Counter()

        try:
            with transaction.atomic():
                for managed_resource in resources:
                    definitions = list(
                        CheckDefinition.objects.filter(
                            service_type=managed_resource.service_type,
                            is_active=True,
                        ).order_by("sort_order", "id")
                    )
                    resource_totals = Counter()
                    context = {}

                    for definition in definitions:
                        started_at = timezone.now()
                        try:
                            outcome = self._execute_check(managed_resource, definition, context)
                        except NoCredentialsError:
                            outcome = CheckOutcome(
                                status=HealthStatus.ERROR,
                                summary=(
                                    "AWS credentials are not configured on this machine. "
                                    "Install/configure AWS CLI credentials or export AWS keys, then rerun."
                                ),
                                details={"exception": "NoCredentialsError"},
                            )
                        except PartialCredentialsError:
                            outcome = CheckOutcome(
                                status=HealthStatus.ERROR,
                                summary=(
                                    "AWS credentials are incomplete on this machine. "
                                    "Check your AWS profile or environment variables and rerun."
                                ),
                                details={"exception": "PartialCredentialsError"},
                            )
                        except Exception as exc:  # pragma: no cover - safety net
                            outcome = CheckOutcome(
                                status=HealthStatus.ERROR,
                                summary=f"Unexpected runner error: {exc}",
                                details={"exception": type(exc).__name__},
                            )

                        CheckResult.objects.create(
                            run=run,
                            resource=managed_resource,
                            check_definition=definition,
                            status=outcome.status,
                            summary=outcome.summary,
                            observed_value=outcome.observed_value,
                            details=outcome.details,
                            started_at=started_at,
                            finished_at=timezone.now(),
                        )
                        totals[outcome.status] += 1
                        resource_totals[outcome.status] += 1

                    managed_resource.last_run_at = timezone.now()
                    managed_resource.last_overall_status = self._overall_status(resource_totals)
                    managed_resource.save(update_fields=["last_run_at", "last_overall_status", "updated_at"])

        except Exception as exc:
            run.finished_at = timezone.now()
            run.status = HealthStatus.ERROR
            run.error_message = str(exc)
            run.summary = "The run exited early because a bulk-level error occurred."
            run.save(
                update_fields=["finished_at", "status", "error_message", "summary"]
            )
            return run

        run.finished_at = timezone.now()
        run.resource_count = len(resources)
        run.check_count = sum(totals.values())
        run.pass_count = totals[HealthStatus.PASS]
        run.warn_count = totals[HealthStatus.WARN]
        run.fail_count = totals[HealthStatus.FAIL]
        run.skip_count = totals[HealthStatus.SKIP]
        run.error_count = totals[HealthStatus.ERROR]
        run.status = self._overall_status(totals)
        run.summary = self._build_summary(resources, totals)
        run.save(
            update_fields=[
                "finished_at",
                "resource_count",
                "check_count",
                "pass_count",
                "warn_count",
                "fail_count",
                "skip_count",
                "error_count",
                "status",
                "summary",
            ]
        )
        return run

    def _build_summary(self, resources, totals):
        return (
            f"Processed {len(resources)} resources. "
            f"{totals[HealthStatus.PASS]} passed, "
            f"{totals[HealthStatus.WARN]} warned, "
            f"{totals[HealthStatus.FAIL]} failed, "
            f"{totals[HealthStatus.SKIP]} skipped, "
            f"{totals[HealthStatus.ERROR]} errored."
        )

    def _overall_status(self, totals: Counter):
        if totals[HealthStatus.ERROR]:
            return HealthStatus.ERROR
        if totals[HealthStatus.FAIL]:
            return HealthStatus.FAIL
        if totals[HealthStatus.WARN] or totals[HealthStatus.SKIP]:
            return HealthStatus.WARN
        if totals[HealthStatus.PASS]:
            return HealthStatus.PASS
        return HealthStatus.SKIP

    def _execute_check(self, resource: ManagedResource, definition: CheckDefinition, context: dict):
        handler_name = f"_{resource.service_type}_{definition.code}"
        if not hasattr(self, handler_name):
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="This check is defined in the workbook but is not implemented yet.",
            )
        return getattr(self, handler_name)(resource, definition, context)

    def _session(self, region: str | None = None):
        region = region or settings.AWS_CHECKER_DEFAULT_REGION
        if region not in self._sessions:
            self._sessions[region] = boto3.Session(region_name=region)
        return self._sessions[region]

    def _client(self, service_name: str, region: str | None = None):
        return self._session(region).client(service_name)

    def _latest_metric(self, *, namespace, metric_name, region, dimensions, statistic="Average", period=300, lookback_minutes=15):
        cloudwatch = self._client("cloudwatch", region)
        end_time = timezone.now()
        start_time = end_time - timedelta(minutes=lookback_minutes)
        response = cloudwatch.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=[{"Name": name, "Value": str(value)} for name, value in dimensions.items()],
            StartTime=start_time,
            EndTime=end_time,
            Period=period,
            Statistics=[statistic],
        )
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            return None
        latest = max(datapoints, key=lambda item: item["Timestamp"])
        return latest.get(statistic)

    def _threshold_status(self, value, threshold, *, pass_when_below=True, label="value"):
        if value is None:
            return CheckOutcome(
                status=HealthStatus.WARN,
                summary=f"No recent metric datapoint was available for {label}.",
            )
        passed = value <= threshold if pass_when_below else value >= threshold
        status = HealthStatus.PASS if passed else HealthStatus.FAIL
        comparator = "<=" if pass_when_below else ">="
        return CheckOutcome(
            status=status,
            summary=f"{label} observed {value:.2f}; expected {comparator} {threshold}.",
            observed_value=f"{value:.2f}",
            details={"threshold": threshold, "label": label},
        )

    def _probe_tcp(self, host, port, timeout=None):
        timeout = timeout or settings.AWS_CHECKER_HEALTHCHECK_TIMEOUT
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True

    def _resolve_hostname(self, hostname):
        return socket.gethostbyname(hostname)

    def _describe_instance(self, resource):
        ec2 = self._client("ec2", resource.region)
        response = ec2.describe_instances(InstanceIds=[resource.resource_identifier])
        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            return None
        return reservations[0]["Instances"][0]

    def _describe_instance_status(self, resource):
        ec2 = self._client("ec2", resource.region)
        response = ec2.describe_instance_status(
            InstanceIds=[resource.resource_identifier],
            IncludeAllInstances=True,
        )
        statuses = response.get("InstanceStatuses", [])
        return statuses[0] if statuses else None

    def _describe_db_instance(self, resource):
        rds = self._client("rds", resource.region)
        response = rds.describe_db_instances(DBInstanceIdentifier=resource.resource_identifier)
        instances = response.get("DBInstances", [])
        return instances[0] if instances else None

    def _run_ssm_command(self, resource, command):
        ssm = self._client("ssm", resource.region)
        response = ssm.send_command(
            InstanceIds=[resource.resource_identifier],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
        )
        command_id = response["Command"]["CommandId"]
        for _ in range(15):
            time.sleep(1)
            invocation = ssm.list_command_invocations(
                CommandId=command_id,
                Details=True,
            ).get("CommandInvocations", [])
            if not invocation:
                continue
            current = invocation[0]
            status = current.get("Status")
            if status in {"Success", "Failed", "Cancelled", "TimedOut"}:
                return current
        return None

    def _load_db_probe_settings(self, resource):
        probe = (resource.check_config or {}).get("db_probe", {})
        username_env = probe.get("username_env")
        password_env = probe.get("password_env")
        database_env = probe.get("database_env")
        username = settings.__dict__.get(username_env, None)
        password = settings.__dict__.get(password_env, None)
        database = settings.__dict__.get(database_env, None)
        if not username_env or not password_env or not database_env:
            return None
        import os

        username = os.getenv(username_env)
        password = os.getenv(password_env)
        database = os.getenv(database_env)
        if not username or not password or not database:
            return None
        return {
            "username": username,
            "password": password,
            "database": database,
            "port": probe.get("port") or resource.port,
        }

    def _db_probe_connection(self, resource, query_only=False):
        probe = self._load_db_probe_settings(resource)
        if not probe:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="DB probe skipped. Add db_probe env var names in check_config and export those env vars before running.",
            )

        port = int(probe["port"])
        engine = (resource.engine or "").lower()
        if "mysql" in engine:
            connection = pymysql.connect(
                host=resource.endpoint,
                user=probe["username"],
                password=probe["password"],
                database=probe["database"],
                port=port,
                connect_timeout=settings.AWS_CHECKER_HEALTHCHECK_TIMEOUT,
            )
            with connection.cursor() as cursor:
                if query_only:
                    cursor.execute("SELECT 1")
                    value = cursor.fetchone()
                    connection.close()
                    return CheckOutcome(
                        status=HealthStatus.PASS if value and value[0] == 1 else HealthStatus.FAIL,
                        summary="Simple MySQL query completed.",
                        observed_value=str(value[0] if value else ""),
                    )
                connection.close()
                return CheckOutcome(status=HealthStatus.PASS, summary="MySQL login succeeded.")

        connection = psycopg2.connect(
            host=resource.endpoint,
            user=probe["username"],
            password=probe["password"],
            dbname=probe["database"],
            port=port,
            connect_timeout=settings.AWS_CHECKER_HEALTHCHECK_TIMEOUT,
        )
        with connection.cursor() as cursor:
            if query_only:
                cursor.execute("SELECT 1")
                value = cursor.fetchone()
                connection.close()
                return CheckOutcome(
                    status=HealthStatus.PASS if value and value[0] == 1 else HealthStatus.FAIL,
                    summary="Simple PostgreSQL query completed.",
                    observed_value=str(value[0] if value else ""),
                )
            connection.close()
            return CheckOutcome(status=HealthStatus.PASS, summary="PostgreSQL login succeeded.")

    def _logs_health_check(self, resource, default_groups=None):
        config = resource.check_config or {}
        log_groups = config.get("log_groups") or default_groups or []
        if not log_groups:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Log inspection skipped. Add CloudWatch log groups in check_config.log_groups.",
            )

        pattern = config.get("critical_pattern", '"ERROR" "CRITICAL" "FATAL"')
        logs = self._client("logs", resource.region)
        start_time = int((timezone.now() - timedelta(minutes=15)).timestamp() * 1000)
        total_events = 0
        for group in log_groups:
            response = logs.filter_log_events(
                logGroupName=group,
                startTime=start_time,
                filterPattern=pattern,
                limit=20,
            )
            total_events += len(response.get("events", []))
        status = HealthStatus.PASS if total_events == 0 else HealthStatus.FAIL
        summary = "No recent critical log events found." if total_events == 0 else f"Found {total_events} matching log events."
        return CheckOutcome(status=status, summary=summary, observed_value=str(total_events))

    def _http_probe(self, payload, *, default_method="GET"):
        payload = dict(payload or {})
        method = payload.get("method", default_method).upper()
        timeout = payload.get("timeout", settings.AWS_CHECKER_HEALTHCHECK_TIMEOUT)
        headers = dict(payload.get("headers") or {})
        use_pjt_integration_auth = payload.get("use_pjt_integration_auth", False)
        url = payload.get("url")
        path = payload.get("path")

        if use_pjt_integration_auth:
            client = PJTIntegrationClient()
            url = client.build_url(path=path, url=url)
            headers = client.build_headers(headers=headers)
        elif not url and path:
            base = settings.PJT_INTEGRATION_BASE_URL.rstrip("/")
            if not base:
                raise ValueError(
                    "PJT_INTEGRATION_BASE_URL is required when an HTTP probe uses a relative path."
                )
            url = urljoin(base + "/", path.lstrip("/"))

        if not url:
            raise ValueError("HTTP probe is missing a target url or path.")

        request_kwargs = {
            "headers": headers,
            "timeout": timeout,
            "allow_redirects": payload.get("allow_redirects", True),
        }
        if "json" in payload:
            request_kwargs["json"] = payload["json"]
        if "data" in payload:
            request_kwargs["data"] = payload["data"]
        if "params" in payload:
            request_kwargs["params"] = payload["params"]

        response = requests.request(method, url, **request_kwargs)
        expected_status_codes = payload.get("expected_status_codes")
        if expected_status_codes is None and payload.get("expected_status") is not None:
            expected_status_codes = [payload["expected_status"]]
        expected_substring = payload.get("expected_substring")

        ok = response.ok if expected_status_codes is None else response.status_code in expected_status_codes
        if expected_substring:
            ok = ok and expected_substring in response.text
        return response, ok, url

    def _prepare_s3_probe(self, resource, context):
        if "s3_probe" in context:
            return context["s3_probe"]
        s3 = self._client("s3", resource.region)
        config = resource.check_config or {}
        object_key = config.get("canary_object_key")
        created_here = False
        content = "aws-checker-probe"
        if not object_key:
            prefix = config.get("canary_prefix", "aws-checker/probes")
            object_key = f"{prefix}/{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex}.txt"
            s3.put_object(Bucket=resource.resource_identifier, Key=object_key, Body=content.encode("utf-8"))
            created_here = True
        context["s3_probe"] = {
            "key": object_key,
            "created_here": created_here,
            "content": content,
        }
        return context["s3_probe"]

    def _s3_metric(self, resource, metric_name, *, statistic="Average", dimensions=None, lookback_minutes=15):
        dimensions = dimensions or {"BucketName": resource.resource_identifier, "FilterId": "EntireBucket"}
        candidates = [resource.region, "us-east-1"]
        seen = set()
        for region in candidates:
            if not region or region in seen:
                continue
            seen.add(region)
            try:
                value = self._latest_metric(
                    namespace="AWS/S3",
                    metric_name=metric_name,
                    region=region,
                    dimensions=dimensions,
                    statistic=statistic,
                    lookback_minutes=lookback_minutes,
                )
                if value is not None:
                    return value
            except ClientError:
                continue
        return None

    def _ec2_instance_state(self, resource, _definition, _context):
        instance = self._describe_instance(resource)
        if not instance:
            return CheckOutcome(status=HealthStatus.FAIL, summary="EC2 instance was not found.")
        state = instance["State"]["Name"]
        return CheckOutcome(
            status=HealthStatus.PASS if state == "running" else HealthStatus.FAIL,
            summary=f"Instance state is {state}.",
            observed_value=state,
        )

    def _ec2_system_status_check(self, resource, _definition, _context):
        status = self._describe_instance_status(resource)
        system_state = status.get("SystemStatus", {}).get("Status") if status else None
        if not system_state:
            return CheckOutcome(status=HealthStatus.WARN, summary="System status data was unavailable.")
        return CheckOutcome(
            status=HealthStatus.PASS if system_state == "ok" else HealthStatus.FAIL,
            summary=f"System status is {system_state}.",
            observed_value=system_state,
        )

    def _ec2_instance_status_check(self, resource, _definition, _context):
        status = self._describe_instance_status(resource)
        instance_state = status.get("InstanceStatus", {}).get("Status") if status else None
        if not instance_state:
            return CheckOutcome(status=HealthStatus.WARN, summary="Instance status data was unavailable.")
        return CheckOutcome(
            status=HealthStatus.PASS if instance_state == "ok" else HealthStatus.FAIL,
            summary=f"Instance status is {instance_state}.",
            observed_value=instance_state,
        )

    def _ec2_attached_ebs_status(self, resource, _definition, _context):
        status = self._describe_instance_status(resource)
        ebs_state = status.get("AttachedEbsStatus", {}).get("Status") if status else None
        if not ebs_state:
            return CheckOutcome(status=HealthStatus.WARN, summary="Attached EBS status data was unavailable.")
        return CheckOutcome(
            status=HealthStatus.PASS if ebs_state == "ok" else HealthStatus.FAIL,
            summary=f"Attached EBS status is {ebs_state}.",
            observed_value=ebs_state,
        )

    def _ec2_scheduled_events(self, resource, _definition, _context):
        status = self._describe_instance_status(resource)
        events = status.get("Events", []) if status else []
        return CheckOutcome(
            status=HealthStatus.PASS if not events else HealthStatus.FAIL,
            summary="No scheduled EC2 events are pending." if not events else "Scheduled EC2 events are pending.",
            observed_value=str(len(events)),
            details={"events": events},
        )

    def _ec2_required_ports_reachable(self, resource, _definition, _context):
        config = resource.check_config or {}
        ports = config.get("ports", [])
        host = config.get("probe_host") or resource.public_ip_address or resource.endpoint
        if not ports or not host:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Port probe skipped. Add check_config.ports and a reachable host if you want to test TCP ports.",
            )
        failures = []
        for port in ports:
            try:
                self._probe_tcp(host, port)
            except OSError as exc:
                failures.append({"port": port, "error": str(exc)})
        return CheckOutcome(
            status=HealthStatus.PASS if not failures else HealthStatus.FAIL,
            summary="All configured ports were reachable." if not failures else "One or more configured ports were unreachable.",
            details={"host": host, "failures": failures},
            observed_value=",".join(str(port) for port in ports),
        )

    def _ec2_health_url_app_response(self, resource, _definition, _context):
        config = resource.check_config or {}
        url = config.get("health_check_url")
        path = config.get("health_check_path")
        if not url and not path:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="HTTP probe skipped. Add check_config.health_check_url or health_check_path to test the application endpoint.",
            )
        response, ok, resolved_url = self._http_probe(
            {
                "url": url,
                "path": path,
                "method": config.get("health_check_method", "GET"),
                "headers": config.get("health_check_headers", {}),
                "json": config.get("health_check_json"),
                "data": config.get("health_check_data"),
                "expected_status_codes": config.get("health_check_expected_status_codes"),
                "expected_status": config.get("health_check_expected_status"),
                "expected_substring": config.get("expected_substring"),
                "use_pjt_integration_auth": config.get("use_pjt_integration_auth", False),
            },
            default_method="GET",
        )
        return CheckOutcome(
            status=HealthStatus.PASS if ok else HealthStatus.FAIL,
            summary=f"HTTP probe returned {response.status_code}.",
            observed_value=str(response.status_code),
            details={"url": resolved_url},
        )

    def _ec2_process_service_running(self, resource, _definition, _context):
        config = resource.check_config or {}
        command = config.get("ssm_command")
        if not command:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Process check skipped. Add check_config.ssm_command for an SSM shell probe.",
            )
        invocation = self._run_ssm_command(resource, command)
        if not invocation:
            return CheckOutcome(status=HealthStatus.ERROR, summary="SSM command did not return a final status.")
        status = invocation.get("Status")
        details = invocation.get("CommandPlugins", [])
        return CheckOutcome(
            status=HealthStatus.PASS if status == "Success" else HealthStatus.FAIL,
            summary=f"SSM command finished with {status}.",
            observed_value=status,
            details={"plugins": details},
        )

    def _ec2_ec2_to_rds_connectivity(self, resource, _definition, _context):
        targets = (resource.check_config or {}).get("rds_targets", [])
        if not targets:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Dependency check skipped. Add check_config.rds_targets with host and port pairs.",
            )
        failures = []
        for target in targets:
            try:
                self._probe_tcp(target["host"], target["port"])
            except OSError as exc:
                failures.append({"target": target, "error": str(exc)})
        return CheckOutcome(
            status=HealthStatus.PASS if not failures else HealthStatus.FAIL,
            summary="All configured RDS dependency probes succeeded." if not failures else "One or more RDS dependency probes failed.",
            details={"failures": failures},
        )

    def _ec2_ec2_to_s3_access(self, resource, _definition, _context):
        targets = (resource.check_config or {}).get("s3_targets", [])
        if not targets:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Dependency check skipped. Add check_config.s3_targets with bucket names to test S3 access from the configured runtime credentials.",
            )
        s3 = self._client("s3", resource.region)
        failures = []
        for bucket_name in targets:
            try:
                s3.head_bucket(Bucket=bucket_name)
            except ClientError as exc:
                failures.append({"bucket": bucket_name, "error": str(exc)})
        return CheckOutcome(
            status=HealthStatus.PASS if not failures else HealthStatus.FAIL,
            summary="All configured S3 buckets were reachable with the active AWS credentials." if not failures else "One or more S3 access probes failed.",
            details={"failures": failures},
        )

    def _ec2_ec2_to_other_dependencies(self, resource, _definition, _context):
        targets = (resource.check_config or {}).get("dependency_targets", [])
        if not targets:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Dependency check skipped. Add check_config.dependency_targets for host, port, or URL probes.",
            )
        failures = []
        for target in targets:
            if target.get("type") in {"http", "integration"}:
                try:
                    response, ok, resolved_url = self._http_probe(target, default_method="GET")
                except Exception as exc:
                    failures.append({"target": target, "error": str(exc)})
                    continue
                if not ok:
                    failures.append({"target": target, "status_code": response.status_code, "url": resolved_url})
            else:
                try:
                    self._probe_tcp(target["host"], target["port"])
                except OSError as exc:
                    failures.append({"target": target, "error": str(exc)})
        return CheckOutcome(
            status=HealthStatus.PASS if not failures else HealthStatus.FAIL,
            summary="All configured dependency probes succeeded." if not failures else "One or more dependency probes failed.",
            details={"failures": failures},
        )

    def _ec2_cpu_utilization(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/EC2",
            metric_name="CPUUtilization",
            region=resource.region,
            dimensions={"InstanceId": resource.resource_identifier},
        )
        threshold = (resource.check_config or {}).get("cpu_threshold", 80)
        return self._threshold_status(value, threshold, label="CPUUtilization")

    def _ec2_memory_usage(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="CWAgent",
            metric_name="mem_used_percent",
            region=resource.region,
            dimensions={"InstanceId": resource.resource_identifier},
        )
        threshold = (resource.check_config or {}).get("memory_threshold", 80)
        return self._threshold_status(value, threshold, label="mem_used_percent")

    def _ec2_disk_space_usage(self, resource, _definition, _context):
        disk_dimensions = (resource.check_config or {}).get("disk_dimensions", [])
        if not disk_dimensions:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Disk usage check skipped. Add check_config.disk_dimensions with CWAgent metric dimensions.",
            )
        threshold = (resource.check_config or {}).get("disk_threshold", 85)
        highest = None
        for dimensions in disk_dimensions:
            metric = self._latest_metric(
                namespace="CWAgent",
                metric_name="disk_used_percent",
                region=resource.region,
                dimensions=dimensions,
            )
            if metric is not None:
                highest = max(highest or metric, metric)
        return self._threshold_status(highest, threshold, label="disk_used_percent")

    def _ec2_disk_ebs_io(self, resource, _definition, _context):
        instance = self._describe_instance(resource)
        mappings = instance.get("BlockDeviceMappings", []) if instance else []
        volume_ids = [mapping.get("Ebs", {}).get("VolumeId") for mapping in mappings if mapping.get("Ebs")]
        volume_ids = [volume_id for volume_id in volume_ids if volume_id]
        if not volume_ids:
            return CheckOutcome(status=HealthStatus.WARN, summary="No attached EBS volumes were discovered.")
        threshold = (resource.check_config or {}).get("volume_queue_threshold", 5)
        highest = None
        for volume_id in volume_ids:
            metric = self._latest_metric(
                namespace="AWS/EBS",
                metric_name="VolumeQueueLength",
                region=resource.region,
                dimensions={"VolumeId": volume_id},
                statistic="Maximum",
            )
            if metric is not None:
                highest = max(highest or metric, metric)
        return self._threshold_status(highest, threshold, label="VolumeQueueLength")

    def _ec2_network_in_out_baseline(self, resource, _definition, _context):
        network_in = self._latest_metric(
            namespace="AWS/EC2",
            metric_name="NetworkIn",
            region=resource.region,
            dimensions={"InstanceId": resource.resource_identifier},
        )
        network_out = self._latest_metric(
            namespace="AWS/EC2",
            metric_name="NetworkOut",
            region=resource.region,
            dimensions={"InstanceId": resource.resource_identifier},
        )
        max_in = (resource.check_config or {}).get("network_in_max")
        max_out = (resource.check_config or {}).get("network_out_max")
        if max_in is None and max_out is None:
            return CheckOutcome(
                status=HealthStatus.WARN,
                summary="Observed traffic values were collected, but no baseline thresholds are configured.",
                details={"network_in": network_in, "network_out": network_out},
                observed_value=f"in={network_in}, out={network_out}",
            )
        if max_in is not None and network_in is not None and network_in > max_in:
            return CheckOutcome(status=HealthStatus.FAIL, summary=f"NetworkIn is above the configured threshold ({network_in:.2f}).")
        if max_out is not None and network_out is not None and network_out > max_out:
            return CheckOutcome(status=HealthStatus.FAIL, summary=f"NetworkOut is above the configured threshold ({network_out:.2f}).")
        return CheckOutcome(
            status=HealthStatus.PASS,
            summary="Network traffic is within the configured baseline.",
            observed_value=f"in={network_in}, out={network_out}",
        )

    def _ec2_recent_critical_errors(self, resource, _definition, _context):
        return self._logs_health_check(resource)

    def _rds_db_instance_status(self, resource, _definition, _context):
        instance = self._describe_db_instance(resource)
        if not instance:
            return CheckOutcome(status=HealthStatus.FAIL, summary="RDS instance was not found.")
        status = instance.get("DBInstanceStatus")
        return CheckOutcome(
            status=HealthStatus.PASS if status == "available" else HealthStatus.FAIL,
            summary=f"RDS instance status is {status}.",
            observed_value=status,
        )

    def _rds_pending_maintenance_status(self, resource, _definition, _context):
        instance = self._describe_db_instance(resource)
        if not instance:
            return CheckOutcome(status=HealthStatus.FAIL, summary="RDS instance was not found.")
        rds = self._client("rds", resource.region)
        response = rds.describe_pending_maintenance_actions(ResourceIdentifier=instance["DBInstanceArn"])
        actions = response.get("PendingMaintenanceActions", [])
        return CheckOutcome(
            status=HealthStatus.PASS if not actions else HealthStatus.WARN,
            summary="No pending maintenance actions were reported." if not actions else "Pending maintenance actions were reported.",
            observed_value=str(len(actions)),
            details={"actions": actions},
        )

    def _rds_endpoint_dns_resolution(self, resource, _definition, _context):
        address = self._resolve_hostname(resource.endpoint)
        return CheckOutcome(
            status=HealthStatus.PASS,
            summary=f"Endpoint resolved to {address}.",
            observed_value=address,
        )

    def _rds_port_connectivity_from_app_servers(self, resource, _definition, _context):
        config = resource.check_config or {}
        if not config.get("port_probe_enabled"):
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Port probe skipped. Enable check_config.port_probe_enabled to test connectivity from this Django host.",
            )
        self._probe_tcp(resource.endpoint, resource.port or 3306)
        return CheckOutcome(status=HealthStatus.PASS, summary="TCP connection to the RDS endpoint succeeded.")

    def _rds_db_login(self, resource, _definition, _context):
        return self._db_probe_connection(resource, query_only=False)

    def _rds_simple_query(self, resource, _definition, _context):
        return self._db_probe_connection(resource, query_only=True)

    def _rds_cpu_utilization(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="CPUUtilization",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
        )
        return self._threshold_status(value, (resource.check_config or {}).get("cpu_threshold", 80), label="CPUUtilization")

    def _rds_freeable_memory(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="FreeableMemory",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
        )
        threshold = (resource.check_config or {}).get("min_freeable_memory_bytes", 268435456)
        return self._threshold_status(value, threshold, pass_when_below=False, label="FreeableMemory")

    def _rds_free_storage_space(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="FreeStorageSpace",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
        )
        threshold = (resource.check_config or {}).get("min_free_storage_bytes", 2147483648)
        return self._threshold_status(value, threshold, pass_when_below=False, label="FreeStorageSpace")

    def _rds_database_connections(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="DatabaseConnections",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
        )
        threshold = (resource.check_config or {}).get("max_connections")
        if threshold is None:
            return CheckOutcome(
                status=HealthStatus.WARN,
                summary="Current database connection count was collected, but no max_connections threshold is configured.",
                observed_value=f"{value}" if value is not None else "",
            )
        return self._threshold_status(value, threshold, label="DatabaseConnections")

    def _rds_disk_queue_depth(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="DiskQueueDepth",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
            statistic="Maximum",
        )
        return self._threshold_status(value, (resource.check_config or {}).get("disk_queue_threshold", 10), label="DiskQueueDepth")

    def _rds_read_latency(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="ReadLatency",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
            statistic="Average",
        )
        return self._threshold_status(value, (resource.check_config or {}).get("read_latency_threshold", 0.2), label="ReadLatency")

    def _rds_write_latency(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="WriteLatency",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
            statistic="Average",
        )
        return self._threshold_status(value, (resource.check_config or {}).get("write_latency_threshold", 0.2), label="WriteLatency")

    def _rds_replica_lag(self, resource, _definition, _context):
        value = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="ReplicaLag",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
            statistic="Maximum",
        )
        threshold = (resource.check_config or {}).get("replica_lag_threshold", 60)
        if value is None:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="ReplicaLag metric was unavailable. This is normal for primaries or instances without replica metrics.",
            )
        return self._threshold_status(value, threshold, label="ReplicaLag")

    def _rds_network_throughput(self, resource, _definition, _context):
        receive = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="NetworkReceiveThroughput",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
        )
        transmit = self._latest_metric(
            namespace="AWS/RDS",
            metric_name="NetworkTransmitThroughput",
            region=resource.region,
            dimensions={"DBInstanceIdentifier": resource.resource_identifier},
        )
        return CheckOutcome(
            status=HealthStatus.WARN,
            summary="Throughput values were collected. Add thresholds in check_config if you want this to become a pass/fail gate.",
            observed_value=f"rx={receive}, tx={transmit}",
            details={"receive": receive, "transmit": transmit},
        )

    def _rds_enhanced_monitoring_os_metrics(self, resource, _definition, _context):
        default_group = f"RDSOSMetrics"
        return self._logs_health_check(resource, default_groups=[default_group])

    def _rds_db_logs_recent_errors(self, resource, _definition, _context):
        default_group = f"/aws/rds/instance/{resource.resource_identifier}/error"
        return self._logs_health_check(resource, default_groups=[default_group])

    def _s3_bucket_access(self, resource, _definition, _context):
        s3 = self._client("s3", resource.region)
        s3.head_bucket(Bucket=resource.resource_identifier)
        return CheckOutcome(status=HealthStatus.PASS, summary="HeadBucket succeeded.")

    def _s3_access_from_ec2_app_role(self, resource, _definition, _context):
        s3 = self._client("s3", resource.region)
        s3.head_bucket(Bucket=resource.resource_identifier)
        return CheckOutcome(
            status=HealthStatus.PASS,
            summary="Bucket access succeeded using the currently configured boto3 credentials for this app.",
        )

    def _s3_canary_metadata_check(self, resource, _definition, context):
        s3 = self._client("s3", resource.region)
        probe = self._prepare_s3_probe(resource, context)
        s3.head_object(Bucket=resource.resource_identifier, Key=probe["key"])
        return CheckOutcome(status=HealthStatus.PASS, summary=f"HeadObject succeeded for {probe['key']}.", observed_value=probe["key"])

    def _s3_canary_content_read(self, resource, _definition, context):
        s3 = self._client("s3", resource.region)
        probe = self._prepare_s3_probe(resource, context)
        response = s3.get_object(Bucket=resource.resource_identifier, Key=probe["key"])
        content = response["Body"].read().decode("utf-8")
        expected = (resource.check_config or {}).get("expected_canary_content", probe["content"])
        return CheckOutcome(
            status=HealthStatus.PASS if expected in content else HealthStatus.FAIL,
            summary="Canary object content was readable." if expected in content else "Canary object content did not match the expected text.",
            observed_value=probe["key"],
        )

    def _s3_canary_write(self, resource, _definition, context):
        s3 = self._client("s3", resource.region)
        probe = self._prepare_s3_probe(resource, context)
        content = f"aws-checker-write-{timezone.now().isoformat()}"
        s3.put_object(Bucket=resource.resource_identifier, Key=probe["key"], Body=content.encode("utf-8"))
        probe["content"] = content
        return CheckOutcome(status=HealthStatus.PASS, summary=f"PutObject succeeded for {probe['key']}.", observed_value=probe["key"])

    def _s3_canary_delete_overwrite(self, resource, _definition, context):
        s3 = self._client("s3", resource.region)
        probe = self._prepare_s3_probe(resource, context)
        if probe["created_here"]:
            s3.delete_object(Bucket=resource.resource_identifier, Key=probe["key"])
            return CheckOutcome(status=HealthStatus.PASS, summary=f"Temporary probe object {probe['key']} was deleted.")
        return CheckOutcome(
            status=HealthStatus.SKIP,
            summary="Cleanup skipped because the canary object is user-managed. Set a generated canary_prefix if you want AWS Checker to create and delete a temporary object.",
            observed_value=probe["key"],
        )

    def _s3_prefix_listing(self, resource, _definition, context):
        s3 = self._client("s3", resource.region)
        probe = self._prepare_s3_probe(resource, context)
        prefix = (resource.check_config or {}).get("list_prefix") or probe["key"].rsplit("/", 1)[0]
        s3.list_objects_v2(Bucket=resource.resource_identifier, Prefix=prefix, MaxKeys=10)
        return CheckOutcome(status=HealthStatus.PASS, summary=f"ListObjectsV2 succeeded for prefix {prefix}.", observed_value=prefix)

    def _s3_client_4xx_errors(self, resource, _definition, _context):
        value = self._s3_metric(resource, "4xxErrors", statistic="Sum")
        return self._threshold_status(value, (resource.check_config or {}).get("s3_4xx_threshold", 0), label="4xxErrors")

    def _s3_server_5xx_errors(self, resource, _definition, _context):
        value = self._s3_metric(resource, "5xxErrors", statistic="Sum")
        return self._threshold_status(value, (resource.check_config or {}).get("s3_5xx_threshold", 0), label="5xxErrors")

    def _s3_first_byte_latency(self, resource, _definition, _context):
        value = self._s3_metric(resource, "FirstByteLatency")
        return self._threshold_status(value, (resource.check_config or {}).get("first_byte_latency_threshold", 1000), label="FirstByteLatency")

    def _s3_total_request_latency(self, resource, _definition, _context):
        value = self._s3_metric(resource, "TotalRequestLatency")
        return self._threshold_status(value, (resource.check_config or {}).get("total_request_latency_threshold", 2000), label="TotalRequestLatency")

    def _s3_request_volumes(self, resource, _definition, _context):
        metrics = {}
        for metric_name in ["AllRequests", "GetRequests", "PutRequests", "DeleteRequests", "HeadRequests", "ListRequests"]:
            metrics[metric_name] = self._s3_metric(resource, metric_name, statistic="Sum")
        return CheckOutcome(
            status=HealthStatus.WARN,
            summary="S3 request volume metrics were collected. Add thresholds in check_config if you want alerting behavior.",
            details=metrics,
            observed_value=str(metrics.get("AllRequests")),
        )

    def _s3_bucket_size(self, resource, _definition, _context):
        value = self._s3_metric(
            resource,
            "BucketSizeBytes",
            statistic="Average",
            dimensions={"BucketName": resource.resource_identifier, "StorageType": "StandardStorage"},
            lookback_minutes=1440,
        )
        return CheckOutcome(
            status=HealthStatus.WARN if value is not None else HealthStatus.SKIP,
            summary="Bucket size metric collected." if value is not None else "Bucket size metric was unavailable.",
            observed_value=f"{value}" if value is not None else "",
        )

    def _s3_number_of_objects(self, resource, _definition, _context):
        value = self._s3_metric(
            resource,
            "NumberOfObjects",
            statistic="Average",
            dimensions={"BucketName": resource.resource_identifier, "StorageType": "AllStorageTypes"},
            lookback_minutes=1440,
        )
        return CheckOutcome(
            status=HealthStatus.WARN if value is not None else HealthStatus.SKIP,
            summary="Object count metric collected." if value is not None else "NumberOfObjects metric was unavailable.",
            observed_value=f"{value}" if value is not None else "",
        )

    def _s3_replication_backlog_failures(self, resource, _definition, _context):
        failures = self._s3_metric(resource, "OperationsFailedReplication", statistic="Sum")
        lag = self._s3_metric(resource, "ReplicationLatency", statistic="Maximum")
        pending = self._s3_metric(resource, "OperationsPendingReplication", statistic="Maximum")
        if failures is None and lag is None and pending is None:
            return CheckOutcome(
                status=HealthStatus.SKIP,
                summary="Replication metrics were unavailable. This is normal when S3 replication is not enabled.",
            )
        if failures and failures > 0:
            return CheckOutcome(
                status=HealthStatus.FAIL,
                summary="Replication failures were reported.",
                details={"failures": failures, "lag": lag, "pending": pending},
            )
        threshold = (resource.check_config or {}).get("replication_lag_threshold", 300)
        if lag is not None and lag > threshold:
            return CheckOutcome(
                status=HealthStatus.FAIL,
                summary=f"Replication latency is above the configured threshold ({lag}).",
                details={"lag": lag, "pending": pending},
            )
        return CheckOutcome(
            status=HealthStatus.PASS,
            summary="Replication metrics are healthy.",
            details={"failures": failures, "lag": lag, "pending": pending},
        )
