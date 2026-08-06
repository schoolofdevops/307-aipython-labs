"""platformops.findingsstore -- store and query findings in DynamoDB.

A "finding" here is the same concept `incident_context.py` (Module 20)
already calls a "concern": one problem an automated check noticed, with a
severity and a message. That script only ever prints a finding once, to
stdout, for the person watching the terminal right now. This module gives a
finding somewhere durable to live, keyed so "every finding for this
service, most recent first" is a single, fast query.

DynamoDB is not a SQL table. A SQL table can grow a new index on any column
whenever you decide you need one to query by it. DynamoDB's key schema --
the partition key and, optionally, a sort key -- is fixed at table-create
time; the only queries that stay fast without a full scan are ones that
match that key schema. This module picks `service` as the partition key
(every finding for one service lives together, physically, so "give me
checkout-api's findings" is one targeted lookup) and `timestamp` as the
sort key (so results come back ordered, newest first, for free). Deciding
that up front, before the first `put_finding()` call, is the whole point of
the Deep Dive's "hot partition" discussion.

Every client comes from `platformops.awsclient.get_aws_client()` -- no
function here calls `boto3.client()` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import botocore.exceptions

PARTITION_KEY = "service"
SORT_KEY = "timestamp"


def ensure_table(client: Any, table_name: str) -> None:
    """Create `table_name` with the service/timestamp composite key if it does not exist.

    `ResourceInUseException` is DynamoDB's answer when the table already
    exists -- caught by name and treated as success, the same idempotency
    guard `reportstore.ensure_bucket()` applies to S3's
    `BucketAlreadyOwnedByYou`. `PAY_PER_REQUEST` billing means no read/write
    capacity to provision or size ahead of time -- the right default for a
    lab table with unpredictable, low traffic.
    """
    try:
        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": PARTITION_KEY, "KeyType": "HASH"},
                {"AttributeName": SORT_KEY, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": PARTITION_KEY, "AttributeType": "S"},
                {"AttributeName": SORT_KEY, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ResourceInUseException":
            raise
    client.get_waiter("table_exists").wait(TableName=table_name)


def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value["S"] for key, value in item.items()}


def put_finding(
    client: Any,
    table_name: str,
    *,
    service: str,
    severity: str,
    message: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Write one finding, and return it as the plain dict that was stored.

    `timestamp` defaults to the current time in ISO-8601 -- pass it
    explicitly (as the lab and tests do) when a specific, reproducible
    sort-key value matters.
    """
    resolved_timestamp = timestamp or datetime.now(UTC).isoformat()
    finding = {
        PARTITION_KEY: service,
        SORT_KEY: resolved_timestamp,
        "severity": severity,
        "message": message,
    }
    client.put_item(
        TableName=table_name,
        Item={key: {"S": value} for key, value in finding.items()},
    )
    return finding


def get_finding(
    client: Any, table_name: str, *, service: str, timestamp: str
) -> dict[str, str] | None:
    """Fetch one finding by its full key, or `None` if it does not exist."""
    response = client.get_item(
        TableName=table_name,
        Key={PARTITION_KEY: {"S": service}, SORT_KEY: {"S": timestamp}},
    )
    item = response.get("Item")
    if item is None:
        return None
    return _deserialize(item)


def query_findings(
    client: Any, table_name: str, *, service: str, limit: int | None = None
) -> list[dict[str, str]]:
    """Every finding for `service`, most recent first.

    `ScanIndexForward=False` reverses the sort key's natural ascending
    order -- newest timestamp first -- entirely server-side; this function
    never fetches everything and reverses it in Python.
    """
    kwargs: dict[str, Any] = {
        "TableName": table_name,
        "KeyConditionExpression": "#svc = :service",
        "ExpressionAttributeNames": {"#svc": PARTITION_KEY},
        "ExpressionAttributeValues": {":service": {"S": service}},
        "ScanIndexForward": False,
    }
    if limit is not None:
        kwargs["Limit"] = limit
    response = client.query(**kwargs)
    return [_deserialize(item) for item in response["Items"]]
