import json

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from platformops import cloudinventory


def _run_instance(client, *, tags=None):
    kwargs = {
        "ImageId": "ami-12345678",
        "MinCount": 1,
        "MaxCount": 1,
        "InstanceType": "t2.micro",
    }
    if tags:
        kwargs["TagSpecifications"] = [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
            }
        ]
    return client.run_instances(**kwargs)["Instances"][0]["InstanceId"]


# ---------------------------------------------------------------------------
# get_client -- session, profile and region wiring, and the retry config
# every call goes out with. No real AWS call here: boto3.Session is faked so
# the test proves the arguments this function passes, not moto's behavior.
# ---------------------------------------------------------------------------


def test_get_client_passes_profile_region_and_retry_config(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self, profile_name=None):
            captured["profile_name"] = profile_name

        def client(self, service_name, *, region_name=None, config=None):
            captured["service_name"] = service_name
            captured["region_name"] = region_name
            captured["config"] = config
            return "fake-ec2-client"

    monkeypatch.setattr(cloudinventory.boto3, "Session", FakeSession)

    client = cloudinventory.get_client(profile="course-lab", region="eu-west-1")

    assert client == "fake-ec2-client"
    assert captured["profile_name"] == "course-lab"
    assert captured["service_name"] == "ec2"
    assert captured["region_name"] == "eu-west-1"
    assert captured["config"].retries == {"max_attempts": 5, "mode": "standard"}


# ---------------------------------------------------------------------------
# list_instances -- the paginator-backed core. Region, tags and launch time
# all come back JSON-safe (launch_time is an isoformat string, not a
# datetime).
# ---------------------------------------------------------------------------


@mock_aws
def test_list_instances_returns_id_state_region_tags_launch_time():
    client = boto3.client("ec2", region_name="us-west-2")
    _run_instance(client, tags={"team": "platform"})

    records = cloudinventory.list_instances(client)

    assert len(records) == 1
    record = records[0]
    assert record.instance_id.startswith("i-")
    assert record.state in {"pending", "running"}
    assert record.region == "us-west-2"
    assert record.tags == {"team": "platform"}
    assert record.launch_time is not None
    json.dumps(record.launch_time)  # must already be a JSON-safe string


@mock_aws
def test_list_instances_with_no_instances_returns_empty_list():
    client = boto3.client("ec2", region_name="us-east-1")

    records = cloudinventory.list_instances(client)

    assert records == []


# ---------------------------------------------------------------------------
# Pagination -- forcing a small page size (moto honors PaginationConfig)
# proves the code walks every page, not just the first one. With page_size=2
# and 5 real instances, a bug that stopped after the first page would return
# 2, not 5.
# ---------------------------------------------------------------------------


@mock_aws
def test_list_instances_walks_every_page_not_just_the_first():
    client = boto3.client("ec2", region_name="us-east-1")
    for _ in range(5):
        _run_instance(client)

    records = cloudinventory.list_instances(client, page_size=2)

    assert len(records) == 5


# ---------------------------------------------------------------------------
# Tag filtering -- server-side, via the Filters parameter. Only the matching
# subset comes back; the non-matching instance is never even fetched.
# ---------------------------------------------------------------------------


@mock_aws
def test_tag_filtering_returns_only_the_matching_subset():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client, tags={"env": "prod"})
    _run_instance(client, tags={"env": "staging"})

    records = cloudinventory.list_instances(client, tag_key="env", tag_value="prod")

    assert len(records) == 1
    assert records[0].tags["env"] == "prod"


@mock_aws
def test_tag_key_filter_without_value_matches_any_value():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client, tags={"env": "prod"})
    _run_instance(client, tags={"team": "platform"})

    records = cloudinventory.list_instances(client, tag_key="env")

    assert len(records) == 1
    assert "env" in records[0].tags


# ---------------------------------------------------------------------------
# botocore.exceptions.ClientError -- a real one, raised by moto for a
# instance ID that does not exist. list_instances lets it propagate;
# scan_inventory is the layer that catches it.
# ---------------------------------------------------------------------------


@mock_aws
def test_list_instances_raises_client_error_for_unknown_instance_id():
    client = boto3.client("ec2", region_name="us-east-1")

    with pytest.raises(botocore.exceptions.ClientError) as exc_info:
        cloudinventory.list_instances(client, instance_ids=["i-doesnotexist"])

    assert exc_info.value.response["Error"]["Code"] == "InvalidInstanceID.NotFound"


@mock_aws
def test_scan_inventory_catches_client_error_and_reports_it():
    result = cloudinventory.scan_inventory(
        region="us-east-1", instance_ids=["i-doesnotexist"]
    )

    assert result["status"] == "error"
    assert result["error"] == "InvalidInstanceID.NotFound"
    assert "i-doesnotexist" in result["message"]


# ---------------------------------------------------------------------------
# botocore.exceptions.NoCredentialsError -- this must run OUTSIDE @mock_aws.
# moto fakes credentials for you the moment it is active, so the only way to
# see a real NoCredentialsError is with every real credential source cleared
# and moto not patched in at all.
# ---------------------------------------------------------------------------


def test_scan_inventory_catches_missing_credentials(monkeypatch):
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/credentials")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    result = cloudinventory.scan_inventory(region="us-east-1")

    assert result["status"] == "error"
    assert result["error"] == "no-credentials"


# ---------------------------------------------------------------------------
# scan_inventory -- the JSON report a CLI command or a script can print
# directly. Every field must already be JSON-safe.
# ---------------------------------------------------------------------------


@mock_aws
def test_scan_inventory_returns_a_json_safe_ok_report():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client, tags={"env": "prod"})

    result = cloudinventory.scan_inventory(region="us-east-1")

    assert result["status"] == "ok"
    assert result["region"] == "us-east-1"
    assert result["count"] == 1
    assert result["instances"][0]["tags"] == {"env": "prod"}
    json.dumps(result)


@mock_aws
def test_scan_inventory_applies_tag_filter():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client, tags={"env": "prod"})
    _run_instance(client, tags={"env": "staging"})

    result = cloudinventory.scan_inventory(
        region="us-east-1", tag_key="env", tag_value="staging"
    )

    assert result["count"] == 1
    assert result["instances"][0]["tags"]["env"] == "staging"
