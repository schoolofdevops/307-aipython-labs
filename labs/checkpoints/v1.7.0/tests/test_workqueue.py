import uuid

import botocore.exceptions
import pytest

from platformops import workqueue
from platformops.awsclient import get_aws_client

pytestmark = pytest.mark.usefixtures("require_floci")

FLOCI_ENDPOINT = "http://localhost:4566"
REGION = "us-east-1"


@pytest.fixture
def sqs_client():
    return get_aws_client("sqs", region=REGION, endpoint_url=FLOCI_ENDPOINT)


@pytest.fixture
def queue_name():
    return f"platformops-test-{uuid.uuid4().hex[:12]}"


def _teardown_queue(client, queue_url):
    client.delete_queue(QueueUrl=queue_url)


# ---------------------------------------------------------------------------
# ensure_queue -- idempotent create, and it round-trips VisibilityTimeout /
# ReceiveMessageWaitTimeSeconds as real queue attributes, not just
# arguments this module remembers.
# ---------------------------------------------------------------------------


def test_ensure_queue_is_idempotent_and_sets_attributes(sqs_client, queue_name):
    url_one = workqueue.ensure_queue(
        sqs_client, queue_name, visibility_timeout=15, wait_time_seconds=5
    )
    url_two = workqueue.ensure_queue(
        sqs_client, queue_name, visibility_timeout=15, wait_time_seconds=5
    )

    assert url_one == url_two

    attrs = sqs_client.get_queue_attributes(
        QueueUrl=url_one,
        AttributeNames=["VisibilityTimeout", "ReceiveMessageWaitTimeSeconds"],
    )["Attributes"]
    assert attrs["VisibilityTimeout"] == "15"
    assert attrs["ReceiveMessageWaitTimeSeconds"] == "5"

    _teardown_queue(sqs_client, url_one)


# ---------------------------------------------------------------------------
# send_message / receive_messages / delete_message -- the basic round trip.
# ---------------------------------------------------------------------------


def test_send_then_receive_then_delete_round_trip(sqs_client, queue_name):
    queue_url = workqueue.ensure_queue(sqs_client, queue_name)

    message_id = workqueue.send_message(sqs_client, queue_url, "do the thing")
    assert message_id

    messages = workqueue.receive_messages(sqs_client, queue_url, max_messages=1)
    assert len(messages) == 1
    assert messages[0]["body"] == "do the thing"

    workqueue.delete_message(sqs_client, queue_url, messages[0]["receipt_handle"])
    remaining = workqueue.receive_messages(
        sqs_client, queue_url, max_messages=1, wait_time_seconds=1
    )
    assert remaining == []

    _teardown_queue(sqs_client, queue_url)


# ---------------------------------------------------------------------------
# Dead-letter-queue mechanics -- a message that is received (and never
# deleted) past maxReceiveCount moves to the DLQ instead of being retried
# forever or silently dropped. This is the real, working code the lesson
# and deep dive point back to.
# ---------------------------------------------------------------------------


def test_dead_letter_queue_receives_a_message_after_max_receive_count(
    sqs_client, queue_name
):
    dlq_name = f"{queue_name}-dlq"
    dlq_url, dlq_arn = workqueue.ensure_dead_letter_queue(sqs_client, dlq_name)
    main_url = workqueue.ensure_queue(
        sqs_client,
        queue_name,
        dead_letter_arn=dlq_arn,
        max_receive_count=2,
        visibility_timeout=1,
        wait_time_seconds=1,
    )

    workqueue.send_message(sqs_client, main_url, "poison message")

    # Receive it, without deleting, across max_receive_count failed
    # attempts -- each receive with no delete before VisibilityTimeout
    # expires again is one failed processing attempt. ApproximateReceiveCount
    # climbs 1, then 2 (== maxReceiveCount). The next delivery attempt after
    # that is the one Floci redirects to the DLQ instead of the main queue,
    # so this loop runs one attempt past max_receive_count to trigger it.
    import time

    receive_counts = []
    for _ in range(3):
        received = workqueue.receive_messages(
            sqs_client, main_url, max_messages=1, wait_time_seconds=2
        )
        if received:
            receive_counts.append(received[0]["approximate_receive_count"])
        time.sleep(1.5)  # let VisibilityTimeout=1 expire before the next receive

    assert receive_counts == [1, 2]

    dlq_messages = workqueue.receive_messages(
        sqs_client, dlq_url, max_messages=1, wait_time_seconds=2
    )
    assert len(dlq_messages) == 1
    assert dlq_messages[0]["body"] == "poison message"

    _teardown_queue(sqs_client, main_url)
    _teardown_queue(sqs_client, dlq_url)


def test_get_queue_arn_returns_a_real_arn(sqs_client, queue_name):
    queue_url = workqueue.ensure_queue(sqs_client, queue_name)

    arn = workqueue.get_queue_arn(sqs_client, queue_url)

    assert arn.startswith("arn:aws:sqs:")
    assert queue_name in arn

    _teardown_queue(sqs_client, queue_url)


# ---------------------------------------------------------------------------
# send_to_queue / receive_from_queue -- the CLI-facing wrappers. Each builds
# its own client through get_aws_client(), ensures the queue, and turns AWS
# errors into the same {"status": "error", ...} shape reportstore's wrapper
# and cloudinventory.scan_inventory() already use.
# ---------------------------------------------------------------------------


def test_send_to_queue_returns_an_ok_report(sqs_client, queue_name):
    result = workqueue.send_to_queue(
        "do the thing",
        queue_name=queue_name,
        region=REGION,
        endpoint_url=FLOCI_ENDPOINT,
    )

    assert result["status"] == "ok"
    assert "message_id" in result

    queue_url = sqs_client.get_queue_url(QueueName=queue_name)["QueueUrl"]
    _teardown_queue(sqs_client, queue_url)


def test_receive_from_queue_returns_sent_messages(sqs_client, queue_name):
    workqueue.send_to_queue(
        "do the thing",
        queue_name=queue_name,
        region=REGION,
        endpoint_url=FLOCI_ENDPOINT,
    )

    result = workqueue.receive_from_queue(
        queue_name=queue_name,
        region=REGION,
        endpoint_url=FLOCI_ENDPOINT,
        max_messages=1,
        wait_time_seconds=2,
    )

    assert result["status"] == "ok"
    assert len(result["messages"]) == 1
    assert result["messages"][0]["body"] == "do the thing"

    queue_url = sqs_client.get_queue_url(QueueName=queue_name)["QueueUrl"]
    _teardown_queue(sqs_client, queue_url)


def test_receive_from_queue_with_delete_removes_the_message(sqs_client, queue_name):
    workqueue.send_to_queue(
        "do the thing",
        queue_name=queue_name,
        region=REGION,
        endpoint_url=FLOCI_ENDPOINT,
    )

    workqueue.receive_from_queue(
        queue_name=queue_name,
        region=REGION,
        endpoint_url=FLOCI_ENDPOINT,
        max_messages=1,
        wait_time_seconds=2,
        delete=True,
    )
    result = workqueue.receive_from_queue(
        queue_name=queue_name,
        region=REGION,
        endpoint_url=FLOCI_ENDPOINT,
        max_messages=1,
        wait_time_seconds=1,
    )

    assert result["messages"] == []

    queue_url = sqs_client.get_queue_url(QueueName=queue_name)["QueueUrl"]
    _teardown_queue(sqs_client, queue_url)


def test_send_to_queue_catches_client_error(monkeypatch):
    class BoomClient:
        def create_queue(self, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "CreateQueue"
            )

    monkeypatch.setattr(workqueue, "get_aws_client", lambda *a, **k: BoomClient())

    result = workqueue.send_to_queue("hi", queue_name="q", region=REGION)

    assert result["status"] == "error"
    assert result["error"] == "AccessDenied"


def test_receive_from_queue_catches_no_credentials_error(monkeypatch):
    class BoomClient:
        def create_queue(self, **kwargs):
            raise botocore.exceptions.NoCredentialsError()

    monkeypatch.setattr(workqueue, "get_aws_client", lambda *a, **k: BoomClient())

    result = workqueue.receive_from_queue(queue_name="q", region=REGION)

    assert result["status"] == "error"
    assert result["error"] == "no-credentials"
