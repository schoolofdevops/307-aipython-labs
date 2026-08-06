"""platformops.reportstore -- upload, list and download JSON reports in S3.

Every prior module that produces a report (`review-service.py`,
`service_readiness.py`, `incident_context.py`) prints it to stdout and
stops there. Once a report is worth keeping past the terminal session that
produced it -- comparing today's readiness check against last week's, or
handing an incident report to someone who was not on the call -- it needs
somewhere durable to live. S3 is that place: `upload_report()` stores a
report under a timestamped key so repeated uploads never collide,
`list_reports()` finds recent ones, and `download_report()` reads one back.

Every client this module uses comes from `platformops.awsclient.get_aws_client()`
-- no function here ever calls `boto3.client()` directly, and no function
here ever hardcodes `endpoint_url="http://localhost:4566"`. Point
`get_aws_client()` at Floci for a lab run, or leave `endpoint_url` unset for
a real S3 bucket -- this module does not know or care which one it is
talking to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import botocore.exceptions

from platformops.awsclient import get_aws_client

DEFAULT_PREFIX = "reports"


def ensure_bucket(client: Any, bucket: str, *, region: str) -> None:
    """Create `bucket` if it does not already exist -- safe to call every run.

    S3's `us-east-1` is the one region that rejects a `CreateBucketConfiguration`
    with a `LocationConstraint` matching its own name -- every other region
    requires one. `BucketAlreadyOwnedByYou` is the expected outcome the
    second time this runs against the same bucket; it is caught by name and
    treated as success, the same way `findingsstore.ensure_table()` treats
    `ResourceInUseException`.
    """
    kwargs: dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        client.create_bucket(**kwargs)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise


def empty_bucket(client: Any, bucket: str) -> None:
    """Delete every object in `bucket` -- S3 refuses to delete a non-empty bucket."""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=obj["Key"])


def upload_report(
    client: Any, bucket: str, report: dict[str, Any], *, prefix: str = DEFAULT_PREFIX
) -> str:
    """Upload `report` as JSON under a timestamped key, and return that key.

    The timestamp carries microseconds precision (`%Y-%m-%dT%H-%M-%S-%fZ`,
    colons replaced with dashes -- S3 keys allow colons, but several shells
    and most local filesystems do not, and a report key that is also a safe
    local filename is one less thing to think about) so two uploads in the
    same second still land under two different keys instead of one
    overwriting the other silently.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    key = f"{prefix}/{timestamp}.json"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(report).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def download_report(client: Any, bucket: str, key: str) -> dict[str, Any]:
    """Download the object at `key` and parse it back into the report dict."""
    response = client.get_object(Bucket=bucket, Key=key)
    body: bytes = response["Body"].read()
    result: dict[str, Any] = json.loads(body)
    return result


def list_reports(
    client: Any,
    bucket: str,
    *,
    prefix: str = DEFAULT_PREFIX,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """List reports under `prefix`, most recently uploaded first.

    Walks every page through `list_objects_v2`'s paginator -- the same
    discipline `cloudinventory.list_instances()` uses for EC2 -- so a bucket
    with more reports than one page holds still sorts correctly before
    `max_results` trims it, instead of only ever seeing the first page.
    """
    paginator = client.get_paginator("list_objects_v2")
    entries: list[dict[str, Any]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            entries.append(
                {
                    "key": obj["Key"],
                    "last_modified": obj["LastModified"].isoformat(),
                    "size": obj["Size"],
                }
            )
    entries.sort(key=lambda entry: entry["key"], reverse=True)
    return entries[:max_results]


def upload_report_to_bucket(
    report: dict[str, Any],
    *,
    bucket: str,
    region: str,
    profile: str | None = None,
    endpoint_url: str | None = None,
    prefix: str = DEFAULT_PREFIX,
) -> dict[str, Any]:
    """The one call a CLI command or a script needs: build a client, ensure the bucket, upload.

    Catches the same two exception types `cloudinventory.scan_inventory()`
    catches, by name, and returns the same `{"status": "error", ...}` shape
    -- one error-reporting convention across every AWS-backed command in
    this project.
    """
    try:
        client = get_aws_client(
            "s3", region=region, profile=profile, endpoint_url=endpoint_url
        )
        ensure_bucket(client, bucket, region=region)
        key = upload_report(client, bucket, report, prefix=prefix)
    except botocore.exceptions.NoCredentialsError as exc:
        return {"status": "error", "error": "no-credentials", "message": str(exc)}
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        return {"status": "error", "error": code, "message": str(exc)}

    return {"status": "ok", "bucket": bucket, "key": key}
