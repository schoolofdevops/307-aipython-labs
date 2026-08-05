"""platformops.workqueue -- SQS queue automation with a dead-letter-queue pattern.

A queue decouples "something needs doing" from "something is doing it right
now" -- a caller sends a message and moves on, a separate process receives
it and does the work, on its own schedule. That separation only stays safe
if a message that repeatedly fails to process has somewhere to go besides
"retried forever" (a poison message wedges the queue for every message
behind it) or "silently dropped" (the failure disappears with no trace).
This module wires that somewhere-to-go: `ensure_dead_letter_queue()` creates
a DLQ, `ensure_queue()` points a main queue's `RedrivePolicy` at it with a
`maxReceiveCount`, and SQS itself moves a message there once it has been
received and left undeleted that many times.

`VisibilityTimeout` is how long a received-but-not-yet-deleted message stays
hidden from other receivers -- long enough for the receiver to finish
processing it, or the message reappears and looks like a new delivery.
`ReceiveMessageWaitTimeSeconds` enables long polling: a `receive_messages()`
call waits up to that many seconds for a message to arrive instead of
returning empty immediately, trading a slightly slower empty response for
far fewer wasted API calls against an often-empty queue.

Every client comes from `platformops.awsclient.get_aws_client()`.
"""

from __future__ import annotations

import json
from typing import Any

import botocore.exceptions

from platformops.awsclient import get_aws_client

DEFAULT_VISIBILITY_TIMEOUT = 30
DEFAULT_WAIT_TIME_SECONDS = 10


def ensure_queue(
    client: Any,
    queue_name: str,
    *,
    dead_letter_arn: str | None = None,
    max_receive_count: int = 5,
    visibility_timeout: int = DEFAULT_VISIBILITY_TIMEOUT,
    wait_time_seconds: int = DEFAULT_WAIT_TIME_SECONDS,
) -> str:
    """Create `queue_name` if it does not exist, and return its URL.

    SQS's own `create_queue` is already idempotent by name -- calling it
    again with the exact same attributes returns the existing queue's URL
    instead of erroring or creating a duplicate. This function relies on
    that directly rather than pre-checking for the queue's existence itself.

    `dead_letter_arn` and `max_receive_count` together set a `RedrivePolicy`
    attribute -- leave `dead_letter_arn` unset for a queue with no DLQ at
    all.
    """
    attributes: dict[str, str] = {
        "VisibilityTimeout": str(visibility_timeout),
        "ReceiveMessageWaitTimeSeconds": str(wait_time_seconds),
    }
    if dead_letter_arn is not None:
        attributes["RedrivePolicy"] = json.dumps(
            {
                "deadLetterTargetArn": dead_letter_arn,
                "maxReceiveCount": max_receive_count,
            }
        )
    response = client.create_queue(QueueName=queue_name, Attributes=attributes)
    queue_url: str = response["QueueUrl"]
    return queue_url


def ensure_dead_letter_queue(client: Any, queue_name: str) -> tuple[str, str]:
    """Create a plain queue meant to be used as a DLQ, and return `(queue_url, queue_arn)`.

    A DLQ is not a special AWS resource type -- it is an ordinary queue that
    a main queue's `RedrivePolicy` happens to point at. This function's only
    job beyond `ensure_queue()` is fetching the ARN, since `RedrivePolicy`
    needs the DLQ's ARN, not its URL.
    """
    queue_url = ensure_queue(client, queue_name)
    return queue_url, get_queue_arn(client, queue_url)


def get_queue_arn(client: Any, queue_url: str) -> str:
    """Fetch the ARN for a queue URL -- what `RedrivePolicy` needs to name a DLQ."""
    response = client.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )
    arn: str = response["Attributes"]["QueueArn"]
    return arn


def send_message(client: Any, queue_url: str, body: str) -> str:
    """Send one message, and return its message ID."""
    response = client.send_message(QueueUrl=queue_url, MessageBody=body)
    message_id: str = response["MessageId"]
    return message_id


def receive_messages(
    client: Any,
    queue_url: str,
    *,
    max_messages: int = 1,
    wait_time_seconds: int | None = None,
    visibility_timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Receive up to `max_messages`, each with its body, receipt handle and receive count.

    `ApproximateReceiveCount` is included on every message returned -- it is
    SQS's own count of how many times this message has been received
    without being deleted, the same number `RedrivePolicy.maxReceiveCount`
    compares against to decide when a message moves to the DLQ.
    """
    kwargs: dict[str, Any] = {
        "QueueUrl": queue_url,
        "MaxNumberOfMessages": max_messages,
        "AttributeNames": ["ApproximateReceiveCount"],
    }
    if wait_time_seconds is not None:
        kwargs["WaitTimeSeconds"] = wait_time_seconds
    if visibility_timeout is not None:
        kwargs["VisibilityTimeout"] = visibility_timeout

    response = client.receive_message(**kwargs)
    return [
        {
            "message_id": message["MessageId"],
            "receipt_handle": message["ReceiptHandle"],
            "body": message["Body"],
            "approximate_receive_count": int(
                message.get("Attributes", {}).get("ApproximateReceiveCount", "0")
            ),
        }
        for message in response.get("Messages", [])
    ]


def delete_message(client: Any, queue_url: str, receipt_handle: str) -> None:
    """Delete a message using the receipt handle from `receive_messages()`.

    Deleting is how a receiver tells SQS "I finished processing this
    successfully" -- a message that is never deleted is exactly the
    "processing failed" case this module's DLQ pattern exists to handle.
    """
    client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)


def send_to_queue(
    body: str,
    *,
    queue_name: str,
    region: str,
    profile: str | None = None,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """The one call a CLI command needs to send a message: build a client, ensure the queue, send.

    Same `{"status": "error", ...}` convention as
    `reportstore.upload_report_to_bucket()` and
    `cloudinventory.scan_inventory()`.
    """
    try:
        client = get_aws_client(
            "sqs", region=region, profile=profile, endpoint_url=endpoint_url
        )
        queue_url = ensure_queue(client, queue_name)
        message_id = send_message(client, queue_url, body)
    except botocore.exceptions.NoCredentialsError as exc:
        return {"status": "error", "error": "no-credentials", "message": str(exc)}
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        return {"status": "error", "error": code, "message": str(exc)}

    return {"status": "ok", "queue": queue_name, "message_id": message_id}


def receive_from_queue(
    *,
    queue_name: str,
    region: str,
    profile: str | None = None,
    endpoint_url: str | None = None,
    max_messages: int = 1,
    wait_time_seconds: int | None = None,
    delete: bool = False,
) -> dict[str, Any]:
    """The one call a CLI command needs to receive messages: build a client, ensure the queue, receive.

    `delete=True` deletes every message received before returning -- the
    CLI's way of saying "I processed these successfully". Leaving it
    `False` (the default) leaves each message in the queue, undeleted, so a
    learner can watch `ApproximateReceiveCount` climb across repeated
    receives, the same behavior the dead-letter-queue test exercises.
    """
    try:
        client = get_aws_client(
            "sqs", region=region, profile=profile, endpoint_url=endpoint_url
        )
        queue_url = ensure_queue(client, queue_name)
        messages = receive_messages(
            client,
            queue_url,
            max_messages=max_messages,
            wait_time_seconds=wait_time_seconds,
        )
        if delete:
            for message in messages:
                delete_message(client, queue_url, message["receipt_handle"])
    except botocore.exceptions.NoCredentialsError as exc:
        return {"status": "error", "error": "no-credentials", "message": str(exc)}
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        return {"status": "error", "error": code, "message": str(exc)}

    return {"status": "ok", "queue": queue_name, "messages": messages}
