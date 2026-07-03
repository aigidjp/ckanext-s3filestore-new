#!/bin/bash
# Run tests inside the ckan-test container.
# Usage:
#   docker compose -f docker-compose.test.yml run --rm ckan-test bash bin/run-tests.sh [pytest-args...]
#
# Examples:
#   # Run all tests
#   docker compose -f docker-compose.test.yml run --rm ckan-test bash bin/run-tests.sh
#
#   # Run a specific test file
#   docker compose -f docker-compose.test.yml run --rm ckan-test bash bin/run-tests.sh ckanext/s3filestore/tests/test_upload.py
#
#   # Run with verbose output
#   docker compose -f docker-compose.test.yml run --rm ckan-test bash bin/run-tests.sh -v
set -e

echo "==> Installing ckanext-s3filestore..."
pip install -e /srv/app/src/ckanext-s3filestore -q
pip install pytest-cov -q

echo "==> Creating S3 buckets in Moto..."
python3 - <<'EOF'
import boto3
import os

s3 = boto3.client(
    "s3",
    endpoint_url="http://moto:5000",
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    region_name=os.environ["AWS_DEFAULT_REGION"],
)

for bucket in ["test-bucket", "test-odsp-bucket"]:
    s3.create_bucket(Bucket=bucket)
    print(f"  created: {bucket}")
EOF

echo "==> Running tests..."
pytest --ckan-ini=test.ini --disable-warnings "${@:-ckanext/s3filestore/tests}"