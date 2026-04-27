from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from io import StringIO

from botocore.exceptions import NoCredentialsError

from checker.models import CheckDefinition, CheckRun, HealthStatus, ManagedResource
from checker.services.check_runner import CheckOutcome, CheckRunner


class SeedDataTests(TestCase):
    def setUp(self):
        call_command("sync_seed_data", stdout=StringIO())

    def test_seed_command_loads_expected_resource_counts(self):
        self.assertEqual(ManagedResource.objects.filter(service_type="ec2").count(), 21)
        self.assertEqual(ManagedResource.objects.filter(service_type="rds").count(), 3)
        self.assertEqual(ManagedResource.objects.filter(service_type="s3").count(), 2)
        self.assertEqual(CheckDefinition.objects.filter(service_type="ec2").count(), 17)

    def test_dashboard_renders(self):
        response = Client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Monitor EC2, RDS, and S3 from one place")


class RunnerTests(TestCase):
    def setUp(self):
        call_command("sync_seed_data", stdout=StringIO())

    @patch.object(CheckRunner, "_execute_check")
    def test_run_resource_persists_results(self, mocked_execute_check):
        mocked_execute_check.return_value = CheckOutcome(
            status=HealthStatus.PASS,
            summary="mock success",
            observed_value="ok",
        )
        resource = ManagedResource.objects.filter(service_type="s3").first()

        run = CheckRunner().run_resource(resource)

        self.assertEqual(run.status, HealthStatus.PASS)
        self.assertEqual(run.resource_count, 1)
        self.assertEqual(run.check_count, CheckDefinition.objects.filter(service_type="s3").count())
        self.assertEqual(CheckRun.objects.count(), 1)
        resource.refresh_from_db()
        self.assertEqual(resource.last_overall_status, HealthStatus.PASS)

    @patch.object(CheckRunner, "_execute_check")
    def test_missing_credentials_are_reported_cleanly(self, mocked_execute_check):
        mocked_execute_check.side_effect = NoCredentialsError()
        resource = ManagedResource.objects.filter(service_type="s3").first()

        run = CheckRunner().run_resource(resource)
        first_result = run.results.first()

        self.assertEqual(run.status, HealthStatus.ERROR)
        self.assertEqual(first_result.status, HealthStatus.ERROR)
        self.assertIn("AWS credentials are not configured", first_result.summary)

    @override_settings(
        PJT_INTEGRATION_BASE_URL="https://stage.red-flag-alerts.co.in",
        PJT_INTEGRATION_BEARER_TOKEN="test-token",
        PJT_INTEGRATION_CONTRACT_VERSION="2026-04-15",
    )
    @patch("checker.services.check_runner.requests.request")
    def test_http_dependency_targets_support_authenticated_post_integration(self, mocked_request):
        mocked_request.return_value = Mock(ok=True, status_code=202, text="accepted")
        resource = ManagedResource.objects.filter(service_type="ec2").first()
        resource.check_config = {
            "dependency_targets": [
                {
                    "type": "http",
                    "path": "/internal/testing/v1/runs/",
                    "method": "POST",
                    "use_pjt_integration_auth": True,
                    "json": {"source": "aws-checker"},
                    "expected_status_codes": [200, 201, 202],
                }
            ]
        }

        outcome = CheckRunner()._ec2_ec2_to_other_dependencies(resource, None, {})

        self.assertEqual(outcome.status, HealthStatus.PASS)
        _, kwargs = mocked_request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(kwargs["headers"]["X-Contract-Version"], "2026-04-15")
        self.assertEqual(kwargs["json"], {"source": "aws-checker"})
