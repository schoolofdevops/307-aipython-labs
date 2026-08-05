import threading
import time
from unittest import mock

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
# _tags_from_ec2_list -- missing key and empty list both normalize to {}.
# ---------------------------------------------------------------------------


def test_tags_from_ec2_list_handles_missing_key_and_empty_list_and_real_tags():
    assert multiregion._tags_from_ec2_list(None) == {}
    assert multiregion._tags_from_ec2_list([]) == {}
    assert multiregion._tags_from_ec2_list([{"Key": "env", "Value": "prod"}]) == {
        "env": "prod"
    }


# ---------------------------------------------------------------------------
# list_ec2_instances / list_ebs_volumes -- normalized records, paginated.
# ---------------------------------------------------------------------------


@mock_aws
def test_list_ec2_instances_returns_normalized_records():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client, tags={"env": "prod"})

    records = multiregion.list_ec2_instances(client)

    assert len(records) == 1
    record = records[0]
    assert record.resource_type == "ec2-instance"
    assert record.tags == {"env": "prod"}


@mock_aws
def test_list_ec2_instances_walks_every_page_not_just_the_first():
    client = boto3.client("ec2", region_name="us-east-1")
    for _ in range(5):
        _run_instance(client)

    records = multiregion.list_ec2_instances(client, page_size=2)

    assert len(records) == 5


@mock_aws
def test_list_ebs_volumes_returns_normalized_records():
    client = boto3.client("ec2", region_name="us-east-1")
    client.create_volume(Size=8, AvailabilityZone="us-east-1a")

    records = multiregion.list_ebs_volumes(client)

    assert len(records) == 1
    assert records[0].resource_type == "ebs-volume"
    assert records[0].state == "available"


@mock_aws
def test_list_ebs_volumes_walks_every_page():
    client = boto3.client("ec2", region_name="us-east-1")
    for _ in range(5):
        client.create_volume(Size=8, AvailabilityZone="us-east-1a")

    records = multiregion.list_ebs_volumes(client, page_size=2)

    assert len(records) == 5


# ---------------------------------------------------------------------------
# NOTE (learner-QA finding): the Phase-1-only tests
# test_scan_region_combines_ec2_and_ebs (asserted only {ec2-instance,
# ebs-volume}) and test_scan_regions_runs_strictly_sequentially (asserted
# strictly sequential start/end pairs) were removed here. Step 6's own
# text never says to remove them, but they are guaranteed to fail against
# the Step 5 finished module (scan_region() now also returns
# security-group/elastic-ip records, and scan_regions() is now
# concurrent, not sequential) -- confirmed by actually running the suite
# with both left in place, see the lab.md Step 6 finding below. Their
# replacements are test_scan_region_combines_all_four_resource_types and
# the new concurrency-bound tests further down.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# scan_regions -- partial failure handling.
# ---------------------------------------------------------------------------


@mock_aws
def test_scan_regions_succeeds_across_multiple_regions():
    for region in ("us-east-1", "us-west-2"):
        client = boto3.client("ec2", region_name=region)
        _run_instance(client)

    result = multiregion.scan_regions(["us-east-1", "us-west-2"])

    assert result["status"] == "ok"
    assert sorted(result["regions_scanned"]) == ["us-east-1", "us-west-2"]
    ec2_records = [
        r for r in result["resources"] if r["resource_type"] == "ec2-instance"
    ]
    assert len(ec2_records) == 2


def test_scan_regions_a_client_error_lands_in_failed_regions_not_a_crash(monkeypatch):
    def fake_scan_region(*, region, **kwargs):
        if region == "eu-west-1":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
                "DescribeInstances",
            )
        return []

    monkeypatch.setattr(multiregion, "scan_region", fake_scan_region)

    result = multiregion.scan_regions(["us-east-1", "eu-west-1"])

    assert result["status"] == "partial"
    failed = {f["region"]: f for f in result["failed_regions"]}
    assert failed["eu-west-1"]["error"] == "UnauthorizedOperation"
    assert "us-east-1" in result["regions_scanned"]


# ---------------------------------------------------------------------------
# Step 6 additions -- security groups, elastic IPs, S3 buckets, bounded
# concurrency, pagination completeness across the whole scan, and both
# formatters. labs/m23/checks.json was supposed to list the exact function
# names to match, but that file does not exist in the labs repo clone --
# these names are taken from the "Expected output" collected-items block
# in lab.md Step 6 instead.
# ---------------------------------------------------------------------------


@mock_aws
def test_list_security_groups_returns_normalized_records_with_no_state():
    client = boto3.client("ec2", region_name="us-east-1")
    client.create_security_group(GroupName="test-sg", Description="test")

    records = multiregion.list_security_groups(client)

    assert len(records) >= 1
    record = [r for r in records if r.resource_id.startswith("sg-")][0]
    assert record.resource_type == "security-group"
    assert record.state is None


@mock_aws
def test_list_security_groups_walks_every_page():
    client = boto3.client("ec2", region_name="us-east-1")
    for i in range(5):
        client.create_security_group(GroupName=f"test-sg-{i}", Description="test")

    # DescribeSecurityGroups enforces MaxResults >= 5 -- unlike
    # describe_instances/describe_volumes, page_size=2 raises
    # ParamValidationError. Found this the hard way with no checks.json
    # to specify valid values for this test.
    records = multiregion.list_security_groups(client, page_size=5)

    # +1 for the default security group every VPC starts with.
    assert len(records) == 6


@mock_aws
def test_list_elastic_ips_returns_normalized_records():
    client = boto3.client("ec2", region_name="us-east-1")
    client.allocate_address(Domain="vpc")

    records = multiregion.list_elastic_ips(client)

    assert len(records) == 1
    assert records[0].resource_type == "elastic-ip"
    assert records[0].state == "unassociated"


def test_describe_addresses_has_no_paginator_confirming_the_api_quirk():
    client = boto3.client("ec2", region_name="us-east-1")
    assert client.can_paginate("describe_addresses") is False


@mock_aws
def test_list_s3_buckets_discovers_each_buckets_real_region():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="bucket-in-east")
    client.create_bucket(
        Bucket="bucket-in-west",
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )

    records = multiregion.list_s3_buckets(client)

    by_name = {r.resource_id: r for r in records}
    assert by_name["bucket-in-east"].region == "us-east-1"
    assert by_name["bucket-in-west"].region == "us-west-2"


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
        Tagging={"TagSet": [{"Key": "team", "Value": "platform"}]},
    )

    records = multiregion.list_s3_buckets(client)

    assert records[0].tags == {"team": "platform"}


@mock_aws
def test_scan_region_combines_all_four_resource_types():
    client = boto3.client("ec2", region_name="us-east-1")
    _run_instance(client)
    client.create_volume(Size=8, AvailabilityZone="us-east-1a")
    client.create_security_group(GroupName="test-sg", Description="test")
    client.allocate_address(Domain="vpc")

    records = multiregion.scan_region(region="us-east-1")

    resource_types = {r.resource_type for r in records}
    assert resource_types == {
        "ec2-instance",
        "ebs-volume",
        "security-group",
        "elastic-ip",
    }


@mock_aws
def test_scan_regions_pagination_completeness_across_the_whole_scan():
    client = boto3.client("ec2", region_name="us-east-1")
    for _ in range(12):
        _run_instance(client)

    with mock.patch.object(multiregion, "list_s3_buckets", return_value=[]):
        result = multiregion.scan_regions(["us-east-1"], page_size=5)

    ec2_records = [
        r for r in result["resources"] if r["resource_type"] == "ec2-instance"
    ]
    assert len(ec2_records) == 12


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
    monkeypatch.setattr(multiregion, "get_aws_client", lambda *a, **k: object())

    result = multiregion.scan_regions(["us-east-1"])

    failed = {f["region"]: f for f in result["failed_regions"]}
    assert failed["us-east-1"]["error"] == "expired-token"


def test_scan_regions_a_region_failure_does_not_abort_other_regions(monkeypatch):
    def fake_scan_region(*, region, **kwargs):
        if region == "eu-west-1":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
                "DescribeInstances",
            )
        return []

    monkeypatch.setattr(multiregion, "scan_region", fake_scan_region)
    monkeypatch.setattr(multiregion, "list_s3_buckets", lambda client: [])
    monkeypatch.setattr(multiregion, "get_aws_client", lambda *a, **k: object())

    result = multiregion.scan_regions(["us-east-1", "eu-west-1"])

    assert result["status"] == "partial"
    assert "us-east-1" in result["regions_scanned"]
    assert "eu-west-1" not in result["regions_scanned"]


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
    monkeypatch.setattr(multiregion, "get_aws_client", lambda *a, **k: object())

    multiregion.scan_regions([f"region-{i}" for i in range(10)], max_workers=3)

    assert state["peak"] <= 3
    assert state["peak"] > 1


def test_scan_regions_default_max_workers_is_a_small_bounded_number():
    assert 1 < multiregion.DEFAULT_MAX_WORKERS <= 10


def test_to_markdown_renders_a_table_with_every_record():
    records = [
        multiregion.ResourceRecord(
            resource_type="ec2-instance",
            resource_id="i-abc123",
            region="us-east-1",
            tags={"env": "prod"},
            state="running",
        ),
        multiregion.ResourceRecord(
            resource_type="security-group",
            resource_id="sg-abc123",
            region="us-east-1",
            tags={},
            state=None,
        ),
    ]

    output = multiregion.to_markdown(records)

    assert "i-abc123" in output
    assert "sg-abc123" in output
    assert "env=prod" in output
    assert output.count("\n") == 3  # header + separator + 2 rows


def test_to_csv_renders_one_row_per_resource():
    records = [
        multiregion.ResourceRecord(
            resource_type="ec2-instance",
            resource_id="i-abc123",
            region="us-east-1",
            tags={"env": "prod"},
            state="running",
        ),
    ]

    output = multiregion.to_csv(records)
    lines = output.strip().splitlines()

    assert lines[0] == "resource_type,resource_id,region,state,tags"
    assert lines[1] == "ec2-instance,i-abc123,us-east-1,running,env=prod"


def test_formatters_use_the_same_records_no_separate_discovery():
    records = [
        multiregion.ResourceRecord(
            resource_type="ec2-instance",
            resource_id=f"i-{i}",
            region="us-east-1",
            tags={},
            state="running",
        )
        for i in range(3)
    ]

    markdown_rows = len(multiregion.to_markdown(records).splitlines()) - 2
    csv_rows = len(multiregion.to_csv(records).strip().splitlines()) - 1

    assert markdown_rows == csv_rows == len(records)
