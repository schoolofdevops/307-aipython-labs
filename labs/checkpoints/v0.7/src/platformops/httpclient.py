"""platformops.httpclient -- talk to HTTP APIs the way production code has to.

Every module so far has read a file (`config.py`) or a fixed, in-memory
inventory (`inventory/`). Real operational data increasingly lives behind an
HTTP API instead -- a source-control host, a CI system, a cloud provider, an
internal service catalog. Calling one of those correctly is not "send a GET
and print the body": a network is slower and less reliable than a disk, a
server can rate-limit or fail you mid-incident, and a client with no timeout
can hang a script forever waiting on a host that will never answer.

This module uses `httpx` -- this course's standard HTTP client -- instead of
`requests`, for three reasons that matter to an operator: it sets a sane
default timeout policy you can override explicitly (`requests` has no
timeout at all unless you remember one, every single call), it has a modern,
typed API that mirrors `requests` closely enough to be familiar, and it can
grow into async use later in the course without swapping libraries.

Two capabilities live here. `check_health()` is a general-purpose,
single-request probe: is this endpoint up, and how fast did it answer?
`get_repo_info()` and `list_workflow_runs()` are a real integration against
a public API (GitHub's) with retries, pagination and response validation --
the shape almost every production API client in this toolkit will follow
from here on.

Credentials never appear in a log line from this module. `_auth_headers()`
is the one place a token is turned into a header; every log call in this
file names the request being made, never a header's value.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger("platformops.httpclient")

GITHUB_API = "https://api.github.com"

# httpx's own default timeout (5 seconds, applied to every phase of a
# request) is already saner than a library with no default at all -- but a
# single 5-second budget for connect, read AND write can still leave a
# script hanging longer than you expect on a slow body. Setting each phase
# explicitly is the difference between "hopefully fine" and "documented
# behavior a teammate can read straight off this constant."
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# A 429 (rate limited) or a 5xx (server-side trouble) is usually transient --
# retrying a moment later is often the right call. A 4xx other than 429
# (404 not found, 401 unauthorized, 422 bad request) will not change if you
# ask again immediately, so it is never in this set.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 0.5


class HttpCheckError(Exception):
    """Base class for every failure this module raises on purpose.

    Catch this one type in a caller (a CLI command, a script) and you catch
    every deliberate HTTP failure this module can produce, the same pattern
    `platformops.config.ConfigError` established for file problems in M6.
    """


class EndpointUnreachableError(HttpCheckError):
    """DNS failure, connection refused, or every retry attempt timed out."""


class EndpointStatusError(HttpCheckError):
    """The endpoint answered, but with a status this call treats as a failure."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ResponseFormatError(HttpCheckError):
    """The endpoint answered 2xx, but the body was not JSON this call can use."""


@dataclass
class HealthResult:
    """The outcome of one `check_health()` call.

    `ok` is `True` only for a 2xx response -- a reachable server returning a
    500 is `ok=False`, because "the process answered" and "the service is
    healthy" are different questions. `error` is set only when the request
    never got a response at all (timeout, DNS failure, connection refused).
    """

    url: str
    ok: bool
    status_code: int | None
    latency_ms: float | None
    error: str | None = None


def check_health(
    url: str,
    *,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> HealthResult:
    """Make one GET request against `url` and report whether it is healthy.

    Never raises for a reachable-but-unhealthy endpoint (a 4xx or 5xx) --
    that is a normal outcome this function reports in `HealthResult`, not an
    exceptional one. It does distinguish that from "never got an answer at
    all" (timeout, DNS failure, connection refused), which also comes back
    as a normal `HealthResult` with `error` set, so a caller never has to
    wrap this call in its own try/except for the common cases.

    `transport` is normally left as `None`, which makes httpx use a real
    network connection. Tests pass an `httpx.MockTransport` here instead --
    the standard way to swap out the network for a scripted response without
    touching a single line of this function's own logic.
    """
    logger.debug("checking %s", url)
    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        logger.warning("%s: timed out", url)
        return HealthResult(
            url=url,
            ok=False,
            status_code=None,
            latency_ms=None,
            error=f"timed out: {exc}",
        )
    except httpx.ConnectError as exc:
        logger.warning("%s: connection failed", url)
        return HealthResult(
            url=url,
            ok=False,
            status_code=None,
            latency_ms=None,
            error=f"connection failed: {exc}",
        )
    except httpx.HTTPError as exc:
        logger.warning("%s: request failed", url)
        return HealthResult(
            url=url, ok=False, status_code=None, latency_ms=None, error=str(exc)
        )

    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    ok = response.is_success
    logger.info("%s answered %d in %.1fms", url, response.status_code, latency_ms)
    return HealthResult(
        url=url, ok=ok, status_code=response.status_code, latency_ms=latency_ms
    )


def _auth_headers(token: str | None) -> dict[str, str]:
    """Build request headers, adding auth only when a token is given.

    This lab's GitHub calls are all against public, unauthenticated read
    endpoints -- `token` is normally `None`. The pattern still matters:
    every real API integration you write after this course needs exactly
    this shape, and the rule is the same every time -- the token's value is
    used to build a header, and it is never passed to a log call, an
    exception message, or anything else that could end up on a screen or in
    a file. `logger.debug` below states *that* a token is present, never
    *what* it is.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        logger.debug(
            "request will carry an authorization token (value withheld from logs)"
        )
    return headers


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter: attempt 0 waits ~0.5-1s, attempt 1 ~1-1.5s, and so on.

    Jitter (the random extra fraction of a second) matters the moment more
    than one client is retrying the same overloaded server at once -- pure
    exponential backoff with no jitter means every client sleeps the exact
    same duration and then retries in the same instant, recreating the spike
    that triggered the 429/503 in the first place. The Deep Dive works
    through this in more detail.
    """
    delay = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(
        0, BASE_BACKOFF_SECONDS
    )
    logger.debug("retry backoff: sleeping %.2fs before attempt %d", delay, attempt + 2)
    time.sleep(delay)


def _request_with_retry(
    client: httpx.Client, method: str, url: str, *, max_retries: int | None = None
) -> httpx.Response:
    """Issue one request, retrying a transient failure with backoff.

    Retries a connection failure or timeout, and a 429/5xx response --
    never a 4xx other than 429, since asking again immediately will not fix
    a client error like a 404 or a 401. This is only ever called with GET
    in this module: retrying a GET is safe because GET is idempotent (asking
    twice has the same effect as asking once); the Deep Dive covers why that
    same logic does not extend to POST.

    `max_retries` defaults to the module-level `MAX_RETRIES` -- read inside
    the function body, not as a parameter default, so a test overriding
    `platformops.httpclient.MAX_RETRIES` actually takes effect. A default
    evaluated once at import time (`max_retries: int = MAX_RETRIES`) would
    freeze the value at definition time and ignore that override.
    """
    if max_retries is None:
        max_retries = MAX_RETRIES
    last_exc: httpx.HTTPError | None = None
    for attempt in range(max_retries + 1):
        try:
            response = client.request(method, url)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise EndpointUnreachableError(
                    f"{url} unreachable after {max_retries + 1} attempt(s): {exc}"
                ) from exc
            logger.warning(
                "%s: attempt %d/%d failed (%s), retrying",
                url,
                attempt + 1,
                max_retries + 1,
                exc,
            )
            _sleep_backoff(attempt)
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
            logger.warning(
                "%s: attempt %d/%d got status %d, retrying",
                url,
                attempt + 1,
                max_retries + 1,
                response.status_code,
            )
            _sleep_backoff(attempt)
            continue

        return response

    raise EndpointUnreachableError(
        f"{url} unreachable: {last_exc}"
    )  # pragma: no cover -- loop always returns or raises above


def _raise_for_status(response: httpx.Response, url: str) -> None:
    if response.is_success:
        return
    raise EndpointStatusError(
        f"{url} returned {response.status_code}", status_code=response.status_code
    )


class RepoInfo(BaseModel):
    """The subset of a GitHub repository payload this toolkit cares about.

    The same discipline `platformops.servicedef.ServiceDefinition` applies
    to a YAML file, applied to an HTTP response: never trust a raw dict
    past the boundary where it entered the program. A field GitHub renamed,
    dropped, or returned with the wrong type is caught here, as a
    `ResponseFormatError`, instead of surfacing later as a confusing
    `KeyError` or `TypeError` somewhere downstream.
    """

    full_name: str
    description: str | None = None
    default_branch: str
    open_issues_count: int
    stargazers_count: int
    html_url: str


def get_repo_info(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch a public GitHub repository's summary from api.github.com.

    Public repository reads do not require a token -- this call works
    unauthenticated, which is why this lab needs no secret to run. Pass
    `token` (typically read from an environment variable, never hardcoded)
    once you need a private repository or a higher, authenticated rate
    limit. Retries a transient failure (429/5xx, or a connection problem)
    with backoff; raises `EndpointStatusError` for a permanent failure like
    a 404, and `ResponseFormatError` if the response is not the shape this
    toolkit expects.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}"
    logger.debug("fetching repo info for %s/%s", owner, repo)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    try:
        info = RepoInfo.model_validate(payload)
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched repo info for %s/%s", owner, repo)
    return info.model_dump()


def _next_page_url(response: httpx.Response) -> str | None:
    """Parse the `next` link out of GitHub's RFC 5988 `Link` header, if present.

    GitHub's list endpoints do not return every result in one response --
    they page, and they tell you where the next page is with a header that
    looks like `<https://api.github.com/...&page=2>; rel="next", <...>; rel="last"`.
    Reading this is how a client keeps paging until GitHub itself says there
    is nothing left, instead of guessing a page count up front.
    """
    link_header = response.headers.get("link")
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if 'rel="next"' in segment:
            return segment.split(";")[0].strip().strip("<>")
    return None


def list_workflow_runs(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """List GitHub Actions workflow runs for a repository, following pagination.

    Follows the `Link` header's `next` URL until GitHub stops sending one,
    or `max_pages` is reached -- a hard ceiling so a repository with a very
    long run history cannot make this loop run indefinitely. Each page's
    request goes through the same retry-with-backoff path as
    `get_repo_info()`.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs?per_page=30"
    runs: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        page = 0
        while url and page < max_pages:
            response = _request_with_retry(client, "GET", url)
            _raise_for_status(response, url)
            runs.extend(_parse_workflow_runs_page(response, url))
            url = _next_page_url(response)
            page += 1

    logger.info(
        "fetched %d workflow run(s) for %s/%s across %d page(s)",
        len(runs),
        owner,
        repo,
        page,
    )
    return runs


def _parse_workflow_runs_page(
    response: httpx.Response, url: str
) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    if not isinstance(payload, dict) or "workflow_runs" not in payload:
        raise ResponseFormatError(f"{url} response is missing 'workflow_runs'")

    return [
        {
            "id": run.get("id"),
            "name": run.get("name"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "head_branch": run.get("head_branch"),
            "html_url": run.get("html_url"),
        }
        for run in payload["workflow_runs"]
    ]
