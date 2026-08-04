import uuid

import pytest

from platformops import findingsstore
from platformops.awsclient import get_aws_client

pytestmark = pytest.mark.usefixtures("require_floci")

FLOCI_ENDPOINT = "http://localhost:4566"
REGION = "us-east-1"


@pytest.fixture
def ddb_client():
    return get_aws_client("dynamodb", region=REGION, endpoint_url=FLOCI_ENDPOINT)


@pytest.fixture
def table(ddb_client):
    name = f"platformops-test-{uuid.uuid4().hex[:12]}"
    findingsstore.ensure_table(ddb_client, name)
    yield name
    ddb_client.delete_table(TableName=name)


# ---------------------------------------------------------------------------
# ensure_table -- idempotent create, composite key (service + timestamp).
# ---------------------------------------------------------------------------


def test_ensure_table_is_idempotent(ddb_client):
    name = f"platformops-test-{uuid.uuid4().hex[:12]}"

    findingsstore.ensure_table(ddb_client, name)
    findingsstore.ensure_table(ddb_client, name)  # must not raise

    description = ddb_client.describe_table(TableName=name)
    assert description["Table"]["TableStatus"] == "ACTIVE"

    ddb_client.delete_table(TableName=name)


# ---------------------------------------------------------------------------
# put_finding / get_finding -- a finding round-trips exactly, keyed on
# service (partition) + timestamp (sort).
# ---------------------------------------------------------------------------


def test_put_then_get_finding_round_trips(ddb_client, table):
    written = findingsstore.put_finding(
        ddb_client,
        table,
        service="checkout-api",
        severity="high",
        message="dashboard_url missing",
        timestamp="2026-08-05T00:00:00+00:00",
    )

    fetched = findingsstore.get_finding(
        ddb_client, table, service="checkout-api", timestamp="2026-08-05T00:00:00+00:00"
    )

    assert fetched == written
    assert fetched["severity"] == "high"
    assert fetched["message"] == "dashboard_url missing"


def test_get_finding_returns_none_when_missing(ddb_client, table):
    fetched = findingsstore.get_finding(
        ddb_client, table, service="nonexistent", timestamp="2026-08-05T00:00:00+00:00"
    )

    assert fetched is None


def test_put_finding_generates_a_timestamp_when_not_given(ddb_client, table):
    written = findingsstore.put_finding(
        ddb_client, table, service="billing-api", severity="low", message="ok"
    )

    assert "timestamp" in written
    fetched = findingsstore.get_finding(
        ddb_client, table, service="billing-api", timestamp=written["timestamp"]
    )
    assert fetched == written


# ---------------------------------------------------------------------------
# query_findings -- every finding for one service (the partition key), most
# recent first. This is why the table needs a composite key up front: a
# service can have many findings over time, and DynamoDB's key schema is
# fixed at table-create time, unlike a SQL table where you can add an index
# later.
# ---------------------------------------------------------------------------


def test_query_findings_returns_only_the_matching_service_most_recent_first(
    ddb_client, table
):
    findingsstore.put_finding(
        ddb_client,
        table,
        service="checkout-api",
        severity="low",
        message="first",
        timestamp="2026-08-01T00:00:00+00:00",
    )
    findingsstore.put_finding(
        ddb_client,
        table,
        service="checkout-api",
        severity="high",
        message="second",
        timestamp="2026-08-02T00:00:00+00:00",
    )
    findingsstore.put_finding(
        ddb_client,
        table,
        service="billing-api",
        severity="high",
        message="unrelated",
        timestamp="2026-08-01T00:00:00+00:00",
    )

    results = findingsstore.query_findings(ddb_client, table, service="checkout-api")

    assert [r["message"] for r in results] == ["second", "first"]


def test_query_findings_respects_limit(ddb_client, table):
    for i in range(3):
        findingsstore.put_finding(
            ddb_client,
            table,
            service="checkout-api",
            severity="low",
            message=f"finding-{i}",
            timestamp=f"2026-08-0{i + 1}T00:00:00+00:00",
        )

    results = findingsstore.query_findings(
        ddb_client, table, service="checkout-api", limit=2
    )

    assert len(results) == 2
