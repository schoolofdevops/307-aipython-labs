"""platformops.cloudinventory -- scan an AWS account's EC2 instances into one JSON report.

Every prior module in this project talks to something local: a file on
disk, a Docker daemon, a Kubernetes cluster you can `kubectl get` against.
This is the first module that talks to a cloud provider's API instead --
and the discipline that keeps that safe is the same discipline `httpclient`
already uses for a REST API: never hand-roll the request, go through the
one client library the provider maintains (`boto3`), let it own retries and
signing, and treat every failure mode (a bad parameter, a missing
credential) as its own named exception instead of a bare `except Exception`.

`scan_inventory()` is the one function a CLI command or a script needs.
Everything else (`get_client`, `list_instances`) is exported separately so
the tests -- and later modules -- can call each stage on its own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import boto3
import botocore.exceptions
from botocore.config import Config

# Botocore already retries throttling and 5xx responses on its own; this
# just makes that behavior explicit and a little more patient than the
# default 3 attempts. It does NOT retry a `ClientError` like "invalid
# parameter" or "access denied" -- those are not transient, and botocore
# knows not to retry them. See the Deep Dive for the exact split.
DEFAULT_RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"})


@dataclass
class InstanceRecord:
    """One EC2 instance, already reduced to JSON-safe fields."""

    instance_id: str
    state: str
    region: str
    tags: dict[str, str]
    launch_time: str | None


def get_client(*, profile: str | None, region: str) -> Any:
    """Build an EC2 client for one region, honoring boto3's credential chain.

    `profile` selects a named profile from `~/.aws/credentials` --  `None`
    leaves credential resolution to boto3's normal chain (explicit session
    args, then environment variables, then the shared config/credentials
    files, then an IAM role). No key or secret is ever passed in here, and
    none is ever read back out of the client this returns.
    """
    session = boto3.Session(profile_name=profile)
    return session.client("ec2", region_name=region, config=DEFAULT_RETRY_CONFIG)


def _build_filters(tag_key: str | None, tag_value: str | None) -> list[dict[str, Any]]:
    """Server-side tag filters for `describe_instances`.

    Filtering happens on the AWS side, in the `Filters` parameter, not by
    fetching every instance and discarding the ones that don't match in
    Python. On a real account with thousands of instances, client-side
    filtering still pays the full cost (API calls, pagination, bandwidth) of
    fetching everything -- server-side filtering only pays for what matches.
    """
    if tag_key and tag_value:
        return [{"Name": f"tag:{tag_key}", "Values": [tag_value]}]
    if tag_key:
        return [{"Name": "tag-key", "Values": [tag_key]}]
    return []


def list_instances(
    client: Any,
    *,
    tag_key: str | None = None,
    tag_value: str | None = None,
    instance_ids: list[str] | None = None,
    page_size: int | None = None,
) -> list[InstanceRecord]:
    """List EC2 instances through a paginator, never a single `describe_instances` call.

    A real AWS account can hold far more instances than one API response
    page returns. Calling `client.describe_instances()` directly and reading
    only `response["Reservations"]` looks correct against a small test
    fixture -- and silently drops every instance past the first page against
    a real account. `get_paginator("describe_instances")` walks every page
    for you; this function's whole job is to make sure it is actually used
    that way, not to re-prove that botocore's paginator works (that is
    covered by botocore's own test suite).

    `page_size` is optional production functionality (cap how many
    instances one API call returns, useful for tuning a scan against a very
    large account) that also happens to make pagination itself provable in
    a test: force a small page size against a handful of mocked instances,
    and any bug that only reads the first page shows up immediately as a
    short result.
    """
    paginate_kwargs: dict[str, Any] = {}
    filters = _build_filters(tag_key, tag_value)
    if filters:
        paginate_kwargs["Filters"] = filters
    if instance_ids:
        paginate_kwargs["InstanceIds"] = instance_ids
    if page_size:
        paginate_kwargs["PaginationConfig"] = {"PageSize": page_size}

    region = client.meta.region_name
    paginator = client.get_paginator("describe_instances")

    records: list[InstanceRecord] = []
    for page in paginator.paginate(**paginate_kwargs):
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                launch_time = instance.get("LaunchTime")
                records.append(
                    InstanceRecord(
                        instance_id=instance["InstanceId"],
                        state=instance["State"]["Name"],
                        region=region,
                        tags=tags,
                        launch_time=launch_time.isoformat() if launch_time else None,
                    )
                )
    return records


def scan_inventory(
    *,
    profile: str | None = None,
    region: str,
    tag_key: str | None = None,
    tag_value: str | None = None,
    instance_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Scan one region's EC2 instances into a single JSON-safe report.

    `region` has no default -- every call names the region it scans
    explicitly, the same way every prior module names the file or URL it
    acts on explicitly, instead of assuming one.

    Only two exception types are worth catching by name here:
    `NoCredentialsError` (boto3's credential chain found nothing at all --
    an operator problem, not an AWS problem) and `ClientError` (AWS
    rejected the request -- a bad instance ID, a permission the caller
    doesn't have, or any other API-level rejection, each carrying its own
    `Error.Code`). A bare `except Exception` would swallow both alongside
    real bugs in this function; catching each by name keeps a real bug
    loud while still turning the two expected failure modes into a
    structured report instead of a crash.
    """
    try:
        client = get_client(profile=profile, region=region)
        records = list_instances(
            client,
            tag_key=tag_key,
            tag_value=tag_value,
            instance_ids=instance_ids,
        )
    except botocore.exceptions.NoCredentialsError as exc:
        return {
            "status": "error",
            "error": "no-credentials",
            "message": str(exc),
        }
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        return {
            "status": "error",
            "error": code,
            "message": str(exc),
        }

    return {
        "status": "ok",
        "region": region,
        "count": len(records),
        "instances": [asdict(record) for record in records],
    }
