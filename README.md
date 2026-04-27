# AWS Checker 1

AWS Checker is a Django project that monitors the EC2, RDS, and S3 inventory from your AWS account and runs the checklists from your `AWS availability check details.xlsx` workbook.

## What is included

- Seeded inventory for the 21 EC2 instances, 3 RDS databases, and 2 S3 buckets from the workbooks you shared
- Checklist definitions for all EC2, RDS, and S3 checks from the workbook
- A dashboard with buttons to run checks per resource, per service, or across everything
- Persistent run logs with per-check results
- Admin pages for editing resource metadata and `check_config`
- Workbook import commands so updated Excel exports can be reloaded later

## Quick start

1. Create or activate the virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Make sure MySQL has the database and user ready:

   ```sql
   CREATE DATABASE AWS_Logs CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   CREATE USER 'aws_checker'@'localhost' IDENTIFIED BY 'your-password';
   GRANT ALL PRIVILEGES ON AWS_Logs.* TO 'aws_checker'@'localhost';
   FLUSH PRIVILEGES;
   ```

3. Run migrations:

   ```bash
   python manage.py migrate
   ```

4. Load the bundled resource and checklist data:

   ```bash
   python manage.py sync_seed_data
   ```

5. Start the app:

   ```bash
   python manage.py runserver
   ```

6. Open [http://127.0.0.1:8000](http://127.0.0.1:8000)

## MySQL configuration

The project is now configured to use MySQL by default with these settings:

- Host: `localhost`
- Port: `3306`
- Database: `AWS_Logs`
- User: `aws_checker`

You can override them with environment variables:

```bash
export MYSQL_HOST=localhost
export MYSQL_PORT=3306
export MYSQL_DATABASE=AWS_Logs
export MYSQL_USER=aws_checker
export MYSQL_PASSWORD='your-password'
export PJT_INTEGRATION_BASE_URL='https://stage.red-flag-alerts.co.in'
export PJT_INTEGRATION_BEARER_TOKEN='your-staging-token'
export PJT_INTEGRATION_CONTRACT_VERSION='2026-04-15'
export PJT_INTEGRATION_TRIGGER_PATH='/internal/testing/v1/runs/'
```

## AWS setup

The app uses normal boto3 credential resolution. Before testing, configure AWS on this machine with the account/profile you want to use.

Common options:

- `aws configure`
- `export AWS_PROFILE=your-profile-name`
- `export AWS_DEFAULT_REGION=ap-south-1`

## Optional per-resource configuration

Some checks work immediately with AWS API access alone. Others need resource-specific config because the spreadsheets describe application-level probes that AWS cannot infer automatically.

Use Django admin to edit `ManagedResource.check_config`.

Examples:

### EC2

```json
{
  "ports": [22, 80, 443],
  "health_check_url": "https://example.com/health",
  "health_check_method": "GET",
  "expected_substring": "ok",
  "ssm_command": "systemctl is-active nginx",
  "rds_targets": [{"host": "db.example", "port": 3306}],
  "s3_targets": ["example-bucket"],
  "dependency_targets": [
    {"type": "tcp", "host": "redis.internal", "port": 6379},
    {"type": "http", "url": "https://api.example.com/ready"},
    {
      "type": "http",
      "path": "/internal/testing/v1/runs/",
      "method": "POST",
      "use_pjt_integration_auth": true,
      "json": {"source": "aws-checker"},
      "expected_status_codes": [200, 201, 202]
    }
  ],
  "cpu_threshold": 80,
  "memory_threshold": 80,
  "disk_dimensions": [
    {"InstanceId": "i-xxxxxxxx", "path": "/", "fstype": "xfs", "device": "nvme0n1p1"}
  ]
}
```

## PJT staging integration

The project now supports authenticated integration HTTP requests using these environment variables:

- `PJT_INTEGRATION_BASE_URL`
- `PJT_INTEGRATION_BEARER_TOKEN`
- `PJT_INTEGRATION_CONTRACT_VERSION`
- `PJT_INTEGRATION_TRIGGER_PATH`

To manually trigger the staging endpoint from the server:

```bash
python manage.py trigger_pjt_integration_run --payload-json '{"source":"aws-checker"}'
```

To use the same staging connection inside resource checks, add an HTTP dependency target with:

- `"path"` instead of a full URL when you want it resolved relative to `PJT_INTEGRATION_BASE_URL`
- `"use_pjt_integration_auth": true` to add both the bearer token and `X-Contract-Version`
- `"method": "POST"` for trigger endpoints

### RDS

```json
{
  "port_probe_enabled": true,
  "cpu_threshold": 80,
  "max_connections": 100,
  "db_probe": {
    "username_env": "RDS_MASTER_DB_USER",
    "password_env": "RDS_MASTER_DB_PASSWORD",
    "database_env": "RDS_MASTER_DB_NAME",
    "port": 3306
  }
}
```

### S3

```json
{
  "canary_prefix": "aws-checker/probes/rfa-live",
  "first_byte_latency_threshold": 1000,
  "total_request_latency_threshold": 2000,
  "s3_4xx_threshold": 0,
  "s3_5xx_threshold": 0
}
```

## Import updated Excel exports

If your inventory workbook changes, re-import it with:

```bash
python manage.py import_workbooks \
  --details "/absolute/path/AWS availability check details.xlsx" \
  --ec2 "/absolute/path/EC2 List.xlsx" \
  --rds "/absolute/path/RDS List.xlsx" \
  --s3 "/absolute/path/S3 List.xlsx" \
  --account-id 736616688306
```
