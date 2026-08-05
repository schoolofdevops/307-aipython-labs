import uuid

import botocore.exceptions
import pytest

from platformops import reportstore
from platformops.awsclient import get_aws_client

pytestmark = pytest.mark.usefixtures("require_floci")

FLOCI_ENDPOINT = "http://localhost:4566"
REGION = "us-east-1"


@pytest.fixture
def s3_client():
    return get_aws_client("s3", region=REGION, endpoint_url=FLOCI_ENDPOINT)


@pytest.fixture
def bucket(s3_client):
    name = f"platformops-test-{uuid.uuid4().hex[:12]}"
    reportstore.ensure_bucket(s3_client, name, region=REGION)
    yield name
    reportstore.empty_bucket(s3_client, name)
    s3_client.delete_bucket(Bucket=name)


def test_ensure_bucket_is_idempotent(s3_client):
    name = f"platformops-test-{uuid.uuid4().hex[:12]}"

    reportstore.ensure_bucket(s3_client, name, region=REGION)
    reportstore.ensure_bucket(s3_client, name, region=REGION)  # must not raise

    response = s3_client.list_buckets()
    matching = [b for b in response["Buckets"] if b["Name"] == name]
    assert len(matching) == 1

    reportstore.empty_bucket(s3_client, name)
    s3_client.delete_bucket(Bucket=name)


def test_upload_report_returns_a_timestamped_key_under_the_prefix(s3_client, bucket):
    report = {"status": "ok", "service": "checkout-api", "concerns": []}

    key = reportstore.upload_report(s3_client, bucket, report)

    assert key.startswith("reports/")
    assert key.endswith(".json")


def test_upload_then_download_round_trips_the_same_report(s3_client, bucket):
    report = {"status": "error", "service": "billing-api", "concerns": ["dirty tree"]}

    key = reportstore.upload_report(s3_client, bucket, report)
    downloaded = reportstore.download_report(s3_client, bucket, key)

    assert downloaded == report


def test_two_uploads_in_a_row_get_different_keys(s3_client, bucket):
    key_one = reportstore.upload_report(s3_client, bucket, {"n": 1})
    key_two = reportstore.upload_report(s3_client, bucket, {"n": 2})

    assert key_one != key_two


def test_list_reports_returns_most_recent_first(s3_client, bucket):
    keys = [reportstore.upload_report(s3_client, bucket, {"n": i}) for i in range(3)]

    listed = reportstore.list_reports(s3_client, bucket)

    assert [entry["key"] for entry in listed] == list(reversed(keys))


def test_list_reports_respects_max_results(s3_client, bucket):
    for i in range(5):
        reportstore.upload_report(s3_client, bucket, {"n": i})

    listed = reportstore.list_reports(s3_client, bucket, max_results=2)

    assert len(listed) == 2


def test_list_reports_on_empty_bucket_returns_empty_list(s3_client, bucket):
    listed = reportstore.list_reports(s3_client, bucket)

    assert listed == []


def test_upload_report_to_bucket_returns_an_ok_report(bucket):
    report = {"status": "ok", "service": "checkout-api"}

    result = reportstore.upload_report_to_bucket(
        report, bucket=bucket, region=REGION, endpoint_url=FLOCI_ENDPOINT
    )

    assert result["status"] == "ok"
    assert result["bucket"] == bucket
    assert result["key"].startswith("reports/")


def test_upload_report_to_bucket_catches_client_error(monkeypatch):
    class BoomClient:
        def create_bucket(self, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "CreateBucket"
            )

    monkeypatch.setattr(reportstore, "get_aws_client", lambda *a, **k: BoomClient())

    result = reportstore.upload_report_to_bucket(
        {"status": "ok"}, bucket="some-bucket", region=REGION
    )

    assert result["status"] == "error"
    assert result["error"] == "AccessDenied"


def test_upload_report_to_bucket_catches_no_credentials_error(monkeypatch):
    class BoomClient:
        def create_bucket(self, **kwargs):
            raise botocore.exceptions.NoCredentialsError()

    monkeypatch.setattr(reportstore, "get_aws_client", lambda *a, **k: BoomClient())

    result = reportstore.upload_report_to_bucket(
        {"status": "ok"}, bucket="some-bucket", region=REGION
    )

    assert result["status"] == "error"
    assert result["error"] == "no-credentials"
