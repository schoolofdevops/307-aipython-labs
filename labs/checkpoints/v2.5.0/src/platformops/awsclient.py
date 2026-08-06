"""platformops.awsclient -- one function that builds a boto3 client for any AWS service.

`cloudinventory.get_client()` (Module 21) builds an EC2 client and nothing
else -- it has no way to point boto3 at anything but real AWS. This module
generalizes that same session/profile/region wiring to any service name, and
adds one more axis: an `endpoint_url`. Left at its default (`None`), a
client this function returns talks to real AWS, exactly like
`cloudinventory.get_client()` already does. Set it to Floci's local endpoint
(`http://localhost:4566`) and every call the returned client makes goes
there instead -- no other code has to know the difference.

boto3 itself already reads an `AWS_ENDPOINT_URL` environment variable and
applies it automatically, for every client any code in the process creates.
This module deliberately does not lean on that. It reads the same variable,
but only as a fallback inside this one function, and only for clients built
through it -- an explicit `endpoint_url=` argument always wins. That keeps
the decision local and testable (a unit test can assert exactly which
argument reached `session.client()`) instead of ambient: a `platformops`
process that also happens to import some other library which creates its
own boto3 client would silently redirect that client too, if the env var
were left to boto3's own global handling. Routing every AWS client in this
project through `get_aws_client()` means "is this call still hitting real
AWS?" is answered by reading one function, not by tracing environment state
through the whole process.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.config import Config

DEFAULT_RETRY_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"})


def get_aws_client(
    service_name: str,
    *,
    region: str,
    profile: str | None = None,
    endpoint_url: str | None = None,
) -> Any:
    """Build a boto3 client for `service_name`, the one place this project resolves an AWS endpoint.

    `endpoint_url` left as `None` (the default) falls back to the
    `AWS_ENDPOINT_URL` environment variable, and `None` from that lookup
    means "no endpoint override at all" -- the client talks to real AWS. An
    explicit `endpoint_url` argument always wins over the environment
    variable, the same precedence `profile` already has over boto3's wider
    credential chain.
    """
    resolved_endpoint = (
        endpoint_url if endpoint_url is not None else os.environ.get("AWS_ENDPOINT_URL")
    )
    session = boto3.Session(profile_name=profile)
    return session.client(
        service_name,
        region_name=region,
        endpoint_url=resolved_endpoint,
        config=DEFAULT_RETRY_CONFIG,
    )
