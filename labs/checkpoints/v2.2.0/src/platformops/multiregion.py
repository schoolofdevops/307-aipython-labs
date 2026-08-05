"""platformops.multiregion -- scan five AWS resource types across many regions at once.

`cloudinventory.py` (Module 21) proved one region, one resource type: EC2
instances, one paginator, one client. A real account inventory needs more
than that on two axes at once. First, more regions -- most accounts run
resources in several regions, and scanning them one at a time, waiting for
each to finish before starting the next, is slow in a way that gets worse
every time a region is added. Second, more resource types -- EC2 instances,
EBS volumes, security groups and Elastic IPs, each with its own nested
response shape, plus S3 buckets, which are not region-scoped at all.

This module answers both. `scan_regions()` fans out across regions with a
small, bounded thread pool -- never one thread per region, the same
discipline `httpclient.check_many()` (Module 10) already established for
concurrent HTTP checks -- and every region's scan runs inside its own
try/except, so one region's failure (a `ClientError`, an expired session)
lands in a `failed_regions` list instead of losing every other region's
results. S3 gets one separate, account-wide pass, because `list_buckets()`
already returns every bucket regardless of which region it was created in
-- looping it once per region would either miss nothing (S3 truly is
global for this call) or repeat the exact same list N times, depending on
how it were wired; neither is worth the extra API calls.

Every resource type this module discovers -- no matter how differently its
own AWS API nests the response -- gets flattened into the same
`ResourceRecord` shape. `to_markdown()` and `to_csv()` both build their
output from that one normalized list; neither one re-runs any discovery
logic of its own.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

import botocore.exceptions

from platformops.awsclient import get_aws_client

# A small, explicit, configurable cap -- never one thread per region. See
# Module 10 for why unbounded thread creation is a real production risk
# (thread creation and context-switching overhead grows with count, and
# every request-per-thread also opens its own network connection) rather
# than just a style preference.
DEFAULT_MAX_WORKERS = 5

# The AWS-side error codes that specifically mean "your session token
# expired mid-scan" -- a genuinely different problem from "this account
# doesn't have permission" (UnauthorizedOperation) or "this region doesn't
# support this resource type." Reported as its own "expired-token" reason
# so an operator does not have to parse the raw AWS error code to tell the
# two apart.
EXPIRED_TOKEN_CODES = {"ExpiredToken", "ExpiredTokenException", "RequestExpired"}


@dataclass
class ResourceRecord:
    """One resource of any type, already reduced to the same JSON-safe fields.

    EC2's `Reservations[].Instances[]`, EBS's flatter `Volumes[]`,
    security groups' `SecurityGroups[]`, EIPs' `Addresses[]`, and S3's
    bucket dicts each nest their response completely differently -- every
    lister function in this module ends by building this one shape, so
    nothing downstream (the report, the formatters) needs to know which
    AWS API a given record came from.
    """

    resource_type: str
    resource_id: str
    region: str
    tags: dict[str, str]
    state: str | None


def _tags_from_ec2_list(tag_list: list[dict[str, str]] | None) -> dict[str, str]:
    """Normalize an EC2-style `Tags` list into a plain dict.

    A real, untagged resource can come back with `Tags` missing entirely,
    or present as an empty list -- both mean "no tags," not an error, and
    both must produce the same `{}` here.
    """
    return {tag["Key"]: tag["Value"] for tag in (tag_list or [])}


def list_ec2_instances(
    client: Any, *, page_size: int | None = None
) -> list[ResourceRecord]:
    """List EC2 instances through a paginator -- the same pattern `cloudinventory.list_instances()` uses."""
    paginate_kwargs: dict[str, Any] = {}
    if page_size:
        paginate_kwargs["PaginationConfig"] = {"PageSize": page_size}

    region = client.meta.region_name
    paginator = client.get_paginator("describe_instances")

    records: list[ResourceRecord] = []
    for page in paginator.paginate(**paginate_kwargs):
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                records.append(
                    ResourceRecord(
                        resource_type="ec2-instance",
                        resource_id=instance["InstanceId"],
                        region=region,
                        tags=_tags_from_ec2_list(instance.get("Tags")),
                        state=instance["State"]["Name"],
                    )
                )
    return records


def list_ebs_volumes(
    client: Any, *, page_size: int | None = None
) -> list[ResourceRecord]:
    """List EBS volumes through a paginator -- `describe_volumes` pages exactly like `describe_instances` does."""
    paginate_kwargs: dict[str, Any] = {}
    if page_size:
        paginate_kwargs["PaginationConfig"] = {"PageSize": page_size}

    region = client.meta.region_name
    paginator = client.get_paginator("describe_volumes")

    records: list[ResourceRecord] = []
    for page in paginator.paginate(**paginate_kwargs):
        for volume in page["Volumes"]:
            records.append(
                ResourceRecord(
                    resource_type="ebs-volume",
                    resource_id=volume["VolumeId"],
                    region=region,
                    tags=_tags_from_ec2_list(volume.get("Tags")),
                    state=volume["State"],
                )
            )
    return records


def list_security_groups(
    client: Any, *, page_size: int | None = None
) -> list[ResourceRecord]:
    """List security groups through a paginator.

    A security group has no natural "state" -- it exists or it does not.
    `state` is always `None` here; that is a real, expected value for this
    resource type, not a missing field.
    """
    paginate_kwargs: dict[str, Any] = {}
    if page_size:
        paginate_kwargs["PaginationConfig"] = {"PageSize": page_size}

    region = client.meta.region_name
    paginator = client.get_paginator("describe_security_groups")

    records: list[ResourceRecord] = []
    for page in paginator.paginate(**paginate_kwargs):
        for group in page["SecurityGroups"]:
            records.append(
                ResourceRecord(
                    resource_type="security-group",
                    resource_id=group["GroupId"],
                    region=region,
                    tags=_tags_from_ec2_list(group.get("Tags")),
                    state=None,
                )
            )
    return records


def list_elastic_ips(client: Any) -> list[ResourceRecord]:
    """List Elastic IPs with one direct `describe_addresses()` call -- no paginator exists for it.

    `client.can_paginate("describe_addresses")` returns `False`: AWS never
    registered a paginator for this operation, because one account's
    Elastic IPs never span more than one API response page in practice.
    Calling `get_paginator("describe_addresses")` here would raise
    `OperationNotPageableError` -- this is a real API quirk to know about,
    not a bug to route around.
    """
    region = client.meta.region_name
    response = client.describe_addresses()

    records: list[ResourceRecord] = []
    for address in response["Addresses"]:
        resource_id = address.get("AllocationId") or address["PublicIp"]
        records.append(
            ResourceRecord(
                resource_type="elastic-ip",
                resource_id=resource_id,
                region=region,
                tags=_tags_from_ec2_list(address.get("Tags")),
                state="associated" if address.get("AssociationId") else "unassociated",
            )
        )
    return records


def _bucket_region(client: Any, bucket_name: str) -> str:
    """`get_bucket_location()` returns `None`/empty for `us-east-1` specifically -- every other region names itself."""
    response = client.get_bucket_location(Bucket=bucket_name)
    location = response.get("LocationConstraint")
    return location or "us-east-1"


def _bucket_tags(client: Any, bucket_name: str) -> dict[str, str]:
    """Best-effort tag lookup -- an untagged bucket's `get_bucket_tagging()` call fails with `NoSuchTagSet`, not an empty result.

    That failure is expected, not an error: it means "this bucket has no
    tags," the same meaning a missing `Tags` key has for every other
    resource type in this module. Anything else is a real problem and is
    left to raise.
    """
    try:
        response = client.get_bucket_tagging(Bucket=bucket_name)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NoSuchTagSet":
            return {}
        raise
    return {tag["Key"]: tag["Value"] for tag in response.get("TagSet", [])}


def list_s3_buckets(client: Any) -> list[ResourceRecord]:
    """List every bucket in the account in one call, then resolve each bucket's real region separately.

    S3 is not region-scoped the way EC2, EBS, security groups and EIPs
    are: `list_buckets()` always returns every bucket in the account,
    regardless of which region the client was built for. Looping this the
    same way as the other four resource types -- once per region in
    `scan_regions()`'s per-region loop -- would call `list_buckets()`
    once per region and return the exact same full list every time, N
    times over, while never actually learning which region each bucket
    lives in. This function is called exactly once, account-wide; the
    bucket's own region comes from a second, per-bucket call.
    """
    response = client.list_buckets()

    records: list[ResourceRecord] = []
    for bucket in response["Buckets"]:
        name = bucket["Name"]
        records.append(
            ResourceRecord(
                resource_type="s3-bucket",
                resource_id=name,
                region=_bucket_region(client, name),
                tags=_bucket_tags(client, name),
                state=None,
            )
        )
    return records


def scan_region(
    *,
    region: str,
    profile: str | None = None,
    page_size: int | None = None,
) -> list[ResourceRecord]:
    """Scan one region's EC2 instances, EBS volumes, security groups and Elastic IPs.

    S3 is deliberately excluded here -- it is not region-scoped, and
    `scan_regions()` discovers it once, account-wide, outside this
    per-region function. `get_aws_client()` (Module 22) is used instead of
    `cloudinventory.get_client()` because this function talks to more than
    one service family per call; `endpoint_url` is never passed, so this
    always resolves to real AWS, never Floci -- this module has no local
    stand-in, unlike Module 22's project.
    """
    client = get_aws_client("ec2", region=region, profile=profile)
    records: list[ResourceRecord] = []
    records.extend(list_ec2_instances(client, page_size=page_size))
    records.extend(list_ebs_volumes(client, page_size=page_size))
    records.extend(list_security_groups(client, page_size=page_size))
    records.extend(list_elastic_ips(client))
    return records


def _classify_client_error(exc: botocore.exceptions.ClientError) -> str:
    code: str = exc.response.get("Error", {}).get("Code", "Unknown")
    if code in EXPIRED_TOKEN_CODES:
        return "expired-token"
    return code


def _scan_one_region(
    region: str,
    *,
    profile: str | None,
    page_size: int | None,
    on_start: Callable[[], None] | None,
    on_end: Callable[[], None] | None,
) -> tuple[str, list[ResourceRecord] | None, dict[str, str] | None]:
    """Run inside one worker thread: scan one region, never let its failure escape as an exception.

    `on_start`/`on_end` exist purely so a test (or the Deep Dive) can
    measure how many of these are actually running at once -- production
    callers never need to pass them.
    """
    if on_start is not None:
        on_start()
    try:
        records = scan_region(region=region, profile=profile, page_size=page_size)
        return region, records, None
    except botocore.exceptions.NoCredentialsError as exc:
        return region, None, {"error": "no-credentials", "message": str(exc)}
    except botocore.exceptions.ClientError as exc:
        return (
            region,
            None,
            {
                "error": _classify_client_error(exc),
                "message": str(exc),
            },
        )
    finally:
        if on_end is not None:
            on_end()


def scan_regions(
    regions: list[str],
    *,
    profile: str | None = None,
    page_size: int | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    on_region_start: Callable[[], None] | None = None,
    on_region_end: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Scan every region concurrently, bounded by `max_workers`, plus one account-wide S3 pass.

    Each region's scan runs inside `_scan_one_region()`'s own try/except --
    a region that fails (a `ClientError`, an expired session, no
    credentials) contributes an entry to `failed_regions` with the reason,
    and every other region's scan still completes. The report always
    returns whatever succeeded, plus an honest account of what did not.

    `max_workers` bounds the thread pool itself -- `ThreadPoolExecutor`
    never starts more than `max_workers` region-scans at once, no matter
    how many regions are passed in. See the Deep Dive for a live
    measurement proving this, not just asserting it.
    """
    all_records: list[ResourceRecord] = []
    failed_regions: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _scan_one_region,
                region,
                profile=profile,
                page_size=page_size,
                on_start=on_region_start,
                on_end=on_region_end,
            ): region
            for region in regions
        }
        for future in as_completed(futures):
            region, records, failure = future.result()
            if failure is not None:
                failed_regions.append({"region": region, **failure})
            else:
                assert records is not None
                all_records.extend(records)

    s3_region = regions[0] if regions else "us-east-1"
    try:
        s3_client = get_aws_client("s3", region=s3_region, profile=profile)
        all_records.extend(list_s3_buckets(s3_client))
    except botocore.exceptions.NoCredentialsError as exc:
        failed_regions.append(
            {"region": "global/s3", "error": "no-credentials", "message": str(exc)}
        )
    except botocore.exceptions.ClientError as exc:
        failed_regions.append(
            {
                "region": "global/s3",
                "error": _classify_client_error(exc),
                "message": str(exc),
            }
        )

    failed_names = {failure["region"] for failure in failed_regions}
    regions_scanned = [region for region in regions if region not in failed_names]

    return {
        "status": "ok" if not failed_regions else "partial",
        "regions_scanned": regions_scanned,
        "failed_regions": failed_regions,
        "count": len(all_records),
        "resources": [asdict(record) for record in all_records],
    }


def to_markdown(records: list[ResourceRecord]) -> str:
    """Render every record as one human-readable Markdown table, grouped by resource type.

    Builds directly from the same normalized `list[ResourceRecord]`
    `to_csv()` also reads -- neither formatter repeats any discovery
    logic, they only differ in how they render the same records.
    """
    header = "| Resource Type | Resource ID | Region | State | Tags |"
    separator = "| --- | --- | --- | --- | --- |"
    lines = [header, separator]
    for record in sorted(
        records, key=lambda r: (r.resource_type, r.region, r.resource_id)
    ):
        tags = ", ".join(f"{k}={v}" for k, v in sorted(record.tags.items())) or "-"
        state = record.state or "-"
        lines.append(
            f"| {record.resource_type} | {record.resource_id} | {record.region} "
            f"| {state} | {tags} |"
        )
    return "\n".join(lines)


def to_csv(records: list[ResourceRecord]) -> str:
    """Render one row per resource -- machine-readable, same normalized records `to_markdown()` uses."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["resource_type", "resource_id", "region", "state", "tags"])
    for record in records:
        tags = ";".join(f"{k}={v}" for k, v in sorted(record.tags.items()))
        writer.writerow(
            [
                record.resource_type,
                record.resource_id,
                record.region,
                record.state or "",
                tags,
            ]
        )
    return buffer.getvalue()


__all__ = [
    "DEFAULT_MAX_WORKERS",
    "ResourceRecord",
    "list_ec2_instances",
    "list_ebs_volumes",
    "list_security_groups",
    "list_elastic_ips",
    "list_s3_buckets",
    "scan_region",
    "scan_regions",
    "to_markdown",
    "to_csv",
]
