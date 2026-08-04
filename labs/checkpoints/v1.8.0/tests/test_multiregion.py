import csv
import io
import json
import threading
import time

import boto3
import botocore.exceptions
from moto import mock_aws

from platformops import multiregion


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
# _tags_from_ec2_list -- the small helper every per-resource lister shares.
# A missing "Tags" key and an empty "Tags" list must both normalize to {},
# never crash and never get treated as an error.
# ---------------------------------------------------------------------------


def test_tags_from_ec2_list_handles_missing_key_and_empty_list_and_real_tags():
    assert multiregion._tags_from_ec2_list(None) == {}
    assert multiregion._tags_from_ec2_list([]) == {}
    assert multiregion._tags_from_ec2_list([{"Key": "env", "Value": "prod"}]) == {
        "env": "prod"
    }


# ---------------------------------------------------------------------------
# list_ec2_instances -- reuses M21's paginator pattern, normalized into
# ResourceRecord instead of InstanceRecord.
# ---------------------------------------------------------------------------


@mock_aws
def test_list_ec2_instances_returns_normalized_records():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client, tags={"env": "prod"})

    records = multiregion.list_ec2_instances(client)

    assert len(records) == 1
    record = records[0]
    assert record.resource_type == "ec2-instance"
    assert record.resource_id.startswith("i-")
    assert record.region == "us-east-1"
    assert record.tags == {"env": "prod"}
    assert record.state in {"pending", "running"}


@mock_aws
def test_list_ec2_instances_walks_every_page_not_just_the_first():
    client = boto3.client("ec2", region_name="us-east-1")
    for _ in range(5):
        _run_instance(client)

    records = multiregion.list_ec2_instances(client, page_size=2)

    assert len(records) == 5


# ---------------------------------------------------------------------------
# list_ebs_volumes -- describe_volumes, paginated.
# ---------------------------------------------------------------------------


@mock_aws
def test_list_ebs_volumes_returns_normalized_records():
    client = boto3.client("ec2", region_name="us-east-1")
    client.create_volume(
        Size=8,
        AvailabilityZone="us-east-1a",
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "team", "Value": "sre"}]}
        ],
    )

    records = multiregion.list_ebs_volumes(client)

    assert len(records) == 1
    record = records[0]
    assert record.resource_type == "ebs-volume"
    assert record.resource_id.startswith("vol-")
    assert record.state == "available"
    assert record.tags == {"team": "sre"}


@mock_aws
def test_list_ebs_volumes_walks_every_page():
    client = boto3.client("ec2", region_name="us-east-1")
    for _ in range(5):
        client.create_volume(Size=8, AvailabilityZone="us-east-1a")

    records = multiregion.list_ebs_volumes(client, page_size=2)

    assert len(records) == 5


# ---------------------------------------------------------------------------
# list_security_groups -- describe_security_groups, paginated. No natural
# "state" field, so state is always None -- not an error, just not
# applicable to this resource type.
# ---------------------------------------------------------------------------


@mock_aws
def test_list_security_groups_returns_normalized_records_with_no_state():
    client = boto3.client("ec2", region_name="us-east-1")
    client.create_security_group(GroupName="web", Description="web tier")

    records = multiregion.list_security_groups(client)

    named = [r for r in records if r.resource_id.startswith("sg-")]
    assert len(named) >= 1
    assert all(r.resource_type == "security-group" for r in named)
    assert all(r.state is None for r in named)


@mock_aws
def test_list_security_groups_walks_every_page():
    client = boto3.client("ec2", region_name="us-east-1")
    for i in range(10):
        client.create_security_group(GroupName=f"sg-{i}", Description="test")

    # describe_security_groups enforces MaxResults >= 5 -- unlike
    # describe_instances/describe_volumes, which accept any page size.
    records = multiregion.list_security_groups(client, page_size=5)

    # +1 for the default security group every VPC starts with
    assert len(records) == 11


# ---------------------------------------------------------------------------
# list_elastic_ips -- describe_addresses has NO paginator (a real AWS API
# quirk, confirmed with client.can_paginate("describe_addresses") is False)
# -- it must be called directly, once, never through get_paginator().
# ---------------------------------------------------------------------------


@mock_aws
def test_list_elastic_ips_returns_normalized_records():
    client = boto3.client("ec2", region_name="us-east-1")
    client.allocate_address(Domain="vpc")

    records = multiregion.list_elastic_ips(client)

    assert len(records) == 1
    record = records[0]
    assert record.resource_type == "elastic-ip"
    assert record.state == "unassociated"


def test_describe_addresses_has_no_paginator_confirming_the_api_quirk():
    client = boto3.client("ec2", region_name="us-east-1")
    assert client.can_paginate("describe_addresses") is False


# ---------------------------------------------------------------------------
# list_s3_buckets -- global list_buckets() + per-bucket get_bucket_location()
# + best-effort get_bucket_tagging() (NoSuchTagSet on an untagged bucket
# must not crash the scan).
# ---------------------------------------------------------------------------


@mock_aws
def test_list_s3_buckets_discovers_each_buckets_real_region():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="bucket-east")
    client.create_bucket(
        Bucket="bucket-west",
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )

    records = multiregion.list_s3_buckets(client)

    by_name = {r.resource_id: r for r in records}
    assert by_name["bucket-east"].region == "us-east-1"
    assert by_name["bucket-west"].region == "us-west-2"
    assert all(r.resource_type == "s3-bucket" for r in records)


@mock_aws
def test_list_s3_buckets_handles_an_untagged_bucket_without_crashing():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="untagged-bucket")

    records = multiregion.list_s3_buckets(client)

    assert records[0].tags == {}


@mock_aws
def test_list_s3_buckets_reads_real_tags_when_present():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="tagged-bucket")
    client.put_bucket_tagging(
        Bucket="tagged-bucket",
        Tagging={"TagSet": [{"Key": "owner", "Value": "platform"}]},
    )

    records = multiregion.list_s3_buckets(client)

    assert records[0].tags == {"owner": "platform"}


# ---------------------------------------------------------------------------
# scan_region -- one region, every resource type except S3.
# ---------------------------------------------------------------------------


@mock_aws
def test_scan_region_combines_all_four_resource_types():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client)
    client.create_volume(Size=8, AvailabilityZone="us-east-1a")
    client.allocate_address(Domain="vpc")

    records = multiregion.scan_region(region="us-east-1")

    resource_types = {r.resource_type for r in records}
    assert resource_types == {
        "ec2-instance",
        "ebs-volume",
        "security-group",
        "elastic-ip",
    }


# ---------------------------------------------------------------------------
# scan_regions -- bounded concurrency across regions, partial failure
# handling, and the account-wide S3 pass.
# ---------------------------------------------------------------------------


@mock_aws
def test_scan_regions_succeeds_across_multiple_regions():
    for region in ("us-east-1", "us-west-2"):
        client = boto3.client("ec2", region_name=region)
        _run_instance(client)

    result = multiregion.scan_regions(["us-east-1", "us-west-2"])

    assert result["status"] == "ok"
    assert result["failed_regions"] == []
    assert sorted(result["regions_scanned"]) == ["us-east-1", "us-west-2"]
    ec2_records = [
        r for r in result["resources"] if r["resource_type"] == "ec2-instance"
    ]
    assert len(ec2_records) == 2
    json.dumps(result)  # must already be JSON-safe


@mock_aws
def test_scan_regions_pagination_completeness_across_the_whole_scan():
    client = boto3.client("ec2", region_name="us-east-1")
    for _ in range(12):
        _run_instance(client)

    # page_size=5 satisfies describe_security_groups' MaxResults minimum
    # (5) while still forcing describe_instances across multiple pages.
    result = multiregion.scan_regions(["us-east-1"], page_size=5)

    ec2_records = [
        r for r in result["resources"] if r["resource_type"] == "ec2-instance"
    ]
    assert len(ec2_records) == 12


def test_scan_regions_a_client_error_lands_in_failed_regions_not_a_crash(monkeypatch):
    def fake_scan_region(*, region, **kwargs):
        if region == "eu-west-1":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
                "DescribeInstances",
            )
        return []

    monkeypatch.setattr(multiregion, "scan_region", fake_scan_region)
    monkeypatch.setattr(multiregion, "list_s3_buckets", lambda client: [])

    result = multiregion.scan_regions(["us-east-1", "eu-west-1"])

    assert result["status"] == "partial"
    failed = {f["region"]: f for f in result["failed_regions"]}
    assert failed["eu-west-1"]["error"] == "UnauthorizedOperation"
    assert "us-east-1" in result["regions_scanned"]
    assert "eu-west-1" not in result["regions_scanned"]


def test_scan_regions_reports_an_expired_token_distinctly_from_other_client_errors(
    monkeypatch,
):
    def fake_scan_region(*, region, **kwargs):
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": "ExpiredToken", "Message": "token expired"}},
            "DescribeInstances",
        )

    monkeypatch.setattr(multiregion, "scan_region", fake_scan_region)
    monkeypatch.setattr(multiregion, "list_s3_buckets", lambda client: [])

    result = multiregion.scan_regions(["us-east-1"])

    assert result["failed_regions"][0]["error"] == "expired-token"
    assert result["failed_regions"][0]["error"] != "UnauthorizedOperation"


def test_scan_regions_a_region_failure_does_not_abort_other_regions(monkeypatch):
    calls = []

    def fake_scan_region(*, region, **kwargs):
        calls.append(region)
        if region == "eu-west-1":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
                "DescribeInstances",
            )
        return [
            multiregion.ResourceRecord(
                resource_type="ec2-instance",
                resource_id=f"i-{region}",
                region=region,
                tags={},
                state="running",
            )
        ]

    monkeypatch.setattr(multiregion, "scan_region", fake_scan_region)
    monkeypatch.setattr(multiregion, "list_s3_buckets", lambda client: [])

    result = multiregion.scan_regions(["us-east-1", "eu-west-1", "ap-south-1"])

    assert sorted(calls) == ["ap-south-1", "eu-west-1", "us-east-1"]
    assert sorted(result["regions_scanned"]) == ["ap-south-1", "us-east-1"]
    assert len(result["failed_regions"]) == 1


# ---------------------------------------------------------------------------
# Bounded concurrency -- proving max_workers actually caps the number of
# regions scanned at once, not just documenting it. A lock-protected
# counter tracks how many region-scans are in flight simultaneously.
# ---------------------------------------------------------------------------


def test_scan_regions_never_exceeds_max_workers_concurrent_scans(monkeypatch):
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def fake_scan_region(*, region, **kwargs):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1
        return []

    monkeypatch.setattr(multiregion, "scan_region", fake_scan_region)
    monkeypatch.setattr(multiregion, "list_s3_buckets", lambda client: [])

    regions = [f"region-{i}" for i in range(10)]
    multiregion.scan_regions(regions, max_workers=3)

    assert state["peak"] <= 3
    assert state["peak"] > 1  # proves it is concurrent at all, not sequential


def test_scan_regions_default_max_workers_is_a_small_bounded_number():
    assert 1 <= multiregion.DEFAULT_MAX_WORKERS <= 10


# ---------------------------------------------------------------------------
# Formatters -- to_markdown() and to_csv() both build from the SAME
# normalized list[ResourceRecord]; neither re-runs any discovery logic.
# ---------------------------------------------------------------------------


def _sample_records():
    return [
        multiregion.ResourceRecord(
            resource_type="ec2-instance",
            resource_id="i-abc123",
            region="us-east-1",
            tags={"env": "prod"},
            state="running",
        ),
        multiregion.ResourceRecord(
            resource_type="s3-bucket",
            resource_id="my-bucket",
            region="us-west-2",
            tags={},
            state=None,
        ),
    ]


def test_to_markdown_renders_a_table_with_every_record():
    markdown = multiregion.to_markdown(_sample_records())

    assert "i-abc123" in markdown
    assert "my-bucket" in markdown
    assert "us-east-1" in markdown
    assert "us-west-2" in markdown
    assert markdown.startswith("|")


def test_to_csv_renders_one_row_per_resource():
    csv_text = multiregion.to_csv(_sample_records())

    rows = list(csv.reader(io.StringIO(csv_text)))
    header, *data_rows = rows
    assert header == ["resource_type", "resource_id", "region", "state", "tags"]
    assert len(data_rows) == 2
    ids = {row[1] for row in data_rows}
    assert ids == {"i-abc123", "my-bucket"}


def test_formatters_use_the_same_records_no_separate_discovery():
    records = _sample_records()

    markdown = multiregion.to_markdown(records)
    csv_text = multiregion.to_csv(records)

    for record in records:
        assert record.resource_id in markdown
        assert record.resource_id in csv_text
