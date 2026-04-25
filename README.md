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
  "expected_substring": "ok",
  "ssm_command": "systemctl is-active nginx",
  "rds_targets": [{"host": "db.example", "port": 3306}],
  "s3_targets": ["example-bucket"],
  "dependency_targets": [
    {"type": "tcp", "host": "redis.internal", "port": 6379},
    {"type": "http", "url": "https://api.example.com/ready"}
  ],
  "cpu_threshold": 80,
  "memory_threshold": 80,
  "disk_dimensions": [
    {"InstanceId": "i-xxxxxxxx", "path": "/", "fstype": "xfs", "device": "nvme0n1p1"}
  ]
}
```

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
