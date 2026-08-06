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

`check_many()`, `check_many_sequential()` and `check_many_async()` answer a
different question: not "is this one endpoint healthy," but "is this whole
fleet of endpoints healthy, and how fast can I find out without hammering
every one of them at once." A bounded thread pool and an `asyncio` version
both check many URLs concurrently, with a hard cap on how many requests are
in flight at any moment -- unbounded concurrency is a self-inflicted denial
of service against the very services you are trying to monitor.

Credentials never appear in a log line from this module. `_auth_headers()`
is the one place a token is turned into a header; every log call in this
file names the request being made, never a header's value.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

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

# How many health checks run at the same time in check_many() and
# check_many_async(). Every service you might check has its own capacity --
# checking 200 endpoints with no cap means 200 simultaneous connections
# landing on whichever load balancers and DNS resolvers sit in front of
# them, which looks a lot like the outage you were trying to detect. 8 is a
# deliberately modest default a caller can override for a bigger fleet.
DEFAULT_MAX_CONCURRENCY = 8


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


@dataclass
class HealthSummary:
    """The aggregated outcome of checking many URLs at once.

    `results` keeps every individual `HealthResult` in the same order the
    URLs were given -- a caller that needs the detail never has to
    re-correlate it. `ok` and `failed` are the two numbers you actually want
    on a terminal or in a CI log without scanning the whole list: how many
    of the fleet answered healthy, and how many did not.
    """

    total: int
    ok: int
    failed: int
    results: list[HealthResult]


def summarize_health_results(results: list[HealthResult]) -> HealthSummary:
    """Turn a list of `HealthResult` into the counts a report actually needs."""
    ok_count = sum(1 for result in results if result.ok)
    return HealthSummary(
        total=len(results),
        ok=ok_count,
        failed=len(results) - ok_count,
        results=results,
    )


def check_many_sequential(
    urls: list[str], *, timeout: httpx.Timeout | float = DEFAULT_TIMEOUT
) -> list[HealthResult]:
    """Check every URL one after another. The baseline every faster version is measured against.

    Total time is roughly the sum of every individual check's latency -- 50
    endpoints at ~200ms each is 10 seconds, even though each request spends
    almost all of that time waiting on the network, not doing CPU work. That
    wasted wait is exactly what `check_many()` and `check_many_async()` claw
    back by overlapping the waits instead of taking them one at a time.
    """
    return [check_health(url, timeout=timeout) for url in urls]


def check_many(
    urls: list[str],
    *,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    max_workers: int = DEFAULT_MAX_CONCURRENCY,
) -> list[HealthResult]:
    """Check many URLs concurrently using a bounded thread pool.

    Each `check_health()` call spends almost all of its time blocked on
    network I/O, not holding Python's GIL -- exactly the situation a thread
    pool is good at speeding up, even in CPython. `max_workers` caps how
    many requests are ever in flight at once, the same discipline
    `_request_with_retry()`'s backoff applies to a single retried request,
    applied here across a whole batch instead.

    Returns results in the same order as `urls`, not completion order --
    `executor.map()` guarantees this, so a caller can zip the input list
    against the output without re-sorting by URL.
    """
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(lambda url: check_health(url, timeout=timeout), urls))


async def check_health_async(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> HealthResult:
    """The `async`/`await` twin of `check_health()` -- same contract, same never-raises promise.

    Takes an already-open `client` instead of opening its own, because
    `check_many_async()` shares one `httpx.AsyncClient` (and its underlying
    connection pool) across every URL in a batch -- opening a fresh client
    per request would defeat connection reuse, the main efficiency async
    buys you here.
    """
    logger.debug("checking %s (async)", url)
    start = time.perf_counter()
    try:
        response = await client.get(url, timeout=timeout)
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
    logger.info(
        "%s answered %d in %.1fms (async)", url, response.status_code, latency_ms
    )
    return HealthResult(
        url=url, ok=ok, status_code=response.status_code, latency_ms=latency_ms
    )


async def check_many_async(
    urls: list[str],
    *,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    batch_timeout: float | None = None,
) -> list[HealthResult]:
    """Check many URLs concurrently with `asyncio`, bounded by a semaphore.

    `max_concurrency` works like `check_many()`'s `max_workers`, but there
    is no thread pool underneath it -- an `asyncio.Semaphore` simply refuses
    to let more than `max_concurrency` coroutines past it at once, all
    inside one thread. Every check still gets its own `timeout`, so one slow
    endpoint fails on its own schedule and never blocks the others.

    `batch_timeout`, if given, is a second, independent deadline for the
    *whole batch* -- "give up on this round of checks after N seconds no
    matter how many URLs are left." Any check still running when that
    deadline hits is cancelled, and comes back as a `HealthResult` with
    `error="cancelled: batch timeout exceeded"` instead of being silently
    dropped -- this is the "aggregate partial results, never lose one"
    contract this module keeps everywhere else.
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _bounded_check(url: str, client: httpx.AsyncClient) -> HealthResult:
        async with semaphore:
            return await check_health_async(url, client=client, timeout=timeout)

    async with httpx.AsyncClient() as client:
        tasks = {asyncio.create_task(_bounded_check(url, client)): url for url in urls}
        done, pending = await asyncio.wait(
            tasks, timeout=batch_timeout, return_when=asyncio.ALL_COMPLETED
        )

        for task in pending:
            task.cancel()
        if pending:
            # Cancellation is a request, not an instant -- await the
            # cancelled tasks so each one actually unwinds (closing its
            # slice of the shared connection pool cleanly) before this
            # function returns.
            await asyncio.gather(*pending, return_exceptions=True)

        results_by_url: dict[str, HealthResult] = {}
        for task in done:
            results_by_url[tasks[task]] = task.result()
        for task in pending:
            url = tasks[task]
            logger.warning("%s: cancelled -- batch timeout exceeded", url)
            results_by_url[url] = HealthResult(
                url=url,
                ok=False,
                status_code=None,
                latency_ms=None,
                error="cancelled: batch timeout exceeded",
            )

        return [results_by_url[url] for url in urls]


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
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int | None = None,
) -> httpx.Response:
    """Issue one request, retrying a transient failure with backoff.

    Retries a connection failure or timeout, and a 429/5xx response --
    never a 4xx other than 429, since asking again immediately will not fix
    a client error like a 404 or a 401. This is only ever called with GET
    in this module: retrying a GET is safe because GET is idempotent (asking
    twice has the same effect as asking once); the Deep Dive covers why that
    same logic does not extend to POST.

    `params`, when given, is passed straight to `httpx` as query
    parameters -- `httpx` handles the URL-encoding, which matters the
    moment a caller's value (a PromQL query, a log-search string) contains
    characters a hand-built query string would need to escape itself.

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
            response = client.request(method, url, params=params)
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
    link_header: str | None = response.headers.get("link")
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
    url: str | None = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs?per_page=30"
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


# ---------------------------------------------------------------------------
# Release-readiness endpoints (Module 26) -- five more read-only GitHub REST
# calls, following the exact same shape `get_repo_info()` and
# `list_workflow_runs()` already established: build the URL, go through
# `_request_with_retry()`, raise `_raise_for_status()`, validate the body
# against a Pydantic model before it leaves this module. Every one of these
# is still a GET against GitHub's own API -- this module never triggers a
# build, never merges a pull request, never creates a deployment. It only
# reads what CI/CD already decided. `platformops.releasecheck` is the module
# that combines what these functions return into one verdict; nothing here
# knows what a "ready" release looks like -- that judgment lives one layer up.
# ---------------------------------------------------------------------------


class PullRequestRef(BaseModel):
    """One side (head or base) of a pull request -- just enough to say which branch and commit."""

    ref: str
    sha: str


class PullRequestInfo(BaseModel):
    """The subset of a GitHub pull request payload `platformops.releasecheck` needs.

    `mergeable_state` is GitHub's own summary of whether a PR can be merged
    right now -- `"clean"` (no blockers), `"blocked"` (a required review or
    status check has not passed), `"dirty"` (merge conflicts), `"draft"`,
    `"behind"`, `"unstable"`, or `"unknown"` (GitHub has not finished
    computing it yet -- a real, if unhelpful, value some payloads return).
    This module reads it, it never recomputes it -- GitHub already owns that
    calculation, including branch protection rules this project can't see.
    """

    number: int
    state: str
    draft: bool
    merged: bool = False
    mergeable_state: str | None = None
    title: str
    html_url: str
    head: PullRequestRef
    base: PullRequestRef


def get_pull_request(
    owner: str,
    repo: str,
    number: int,
    *,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch one pull request's merge/review state from `GET /repos/{owner}/{repo}/pulls/{number}`.

    Raises `EndpointStatusError` for a PR number that does not exist (404),
    and `ResponseFormatError` if the response is missing a field this
    toolkit relies on. This function only reads the PR -- it never approves,
    merges or comments on it.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}"
    logger.debug("fetching pull request %s/%s#%d", owner, repo, number)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    try:
        info = PullRequestInfo.model_validate(payload)
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched pull request %s/%s#%d", owner, repo, number)
    return info.model_dump()


class CheckRun(BaseModel):
    """One check run's name, run status and conclusion.

    `status` is where the run is in its lifecycle (`"queued"`,
    `"in_progress"`, `"completed"`). `conclusion` is only meaningful once
    `status` is `"completed"` -- GitHub sets it to `None` for a run still in
    progress, which is exactly the "genuinely don't know yet" case this
    toolkit is careful to report as unknown, not as a failure.
    """

    name: str
    status: str
    conclusion: str | None = None


def list_check_runs(
    owner: str,
    repo: str,
    ref: str,
    *,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """List check runs for one commit or branch from `GET /repos/{owner}/{repo}/commits/{ref}/check-runs`.

    `ref` is a branch name, tag or full commit SHA -- GitHub resolves any of
    the three to the same commit. One page (`per_page=100`) covers every
    real-world case this toolkit's lab and tests use; a repository running
    more than 100 check runs against one commit is far outside this
    course's scope.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{ref}/check-runs?per_page=100"
    logger.debug("fetching check runs for %s/%s@%s", owner, repo, ref)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    if not isinstance(payload, dict) or "check_runs" not in payload:
        raise ResponseFormatError(f"{url} response is missing 'check_runs'")

    try:
        runs = [CheckRun.model_validate(raw) for raw in payload["check_runs"]]
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched %d check run(s) for %s/%s@%s", len(runs), owner, repo, ref)
    return [run.model_dump() for run in runs]


class Artifact(BaseModel):
    """One CI-produced build artifact's name, size and whether it has expired."""

    name: str
    size_in_bytes: int
    expired: bool


def list_artifacts(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """List build artifacts for a repository from `GET /repos/{owner}/{repo}/actions/artifacts`.

    An artifact GitHub still lists but marks `expired: true` is not
    downloadable any more -- it aged out under the repository's retention
    policy. `platformops.releasecheck` treats an expired artifact the same
    as a missing one; this function just reports what GitHub said, honestly,
    and leaves that judgment to the caller.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/artifacts?per_page=100"
    logger.debug("fetching artifacts for %s/%s", owner, repo)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    if not isinstance(payload, dict) or "artifacts" not in payload:
        raise ResponseFormatError(f"{url} response is missing 'artifacts'")

    try:
        artifacts = [Artifact.model_validate(raw) for raw in payload["artifacts"]]
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched %d artifact(s) for %s/%s", len(artifacts), owner, repo)
    return [artifact.model_dump() for artifact in artifacts]


class ReleaseInfo(BaseModel):
    """The subset of a GitHub release payload this toolkit reads."""

    tag_name: str
    name: str | None = None
    draft: bool
    prerelease: bool
    published_at: str | None = None
    html_url: str


def get_latest_release(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch the newest published release from `GET /repos/{owner}/{repo}/releases/latest`.

    A repository with no releases at all answers this endpoint with a real
    404 -- `EndpointStatusError(status_code=404)` -- which is a normal,
    expected outcome here (not every repository has cut a release yet), not
    a sign anything is broken. The caller in `platformops.releasecheck`
    is the layer that turns that 404 into "no release found" instead of
    treating it as an unreachable source.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest"
    logger.debug("fetching latest release for %s/%s", owner, repo)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    try:
        info = ReleaseInfo.model_validate(payload)
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched latest release for %s/%s: %s", owner, repo, info.tag_name)
    return info.model_dump()


class Deployment(BaseModel):
    """One deployment record -- including `payload`, the free-form JSON the deploying system attached.

    GitHub's Deployments API does not define what `payload` contains; it is
    whatever the system that created the deployment chose to record there.
    This project's lab fixtures use it to carry the image reference (a tag
    or a digest) a real deployment pipeline would set -- the same metadata
    shape Module 27 builds and pushes for real.
    """

    id: int
    sha: str
    ref: str
    environment: str
    created_at: str
    payload: dict[str, Any] = {}


def list_deployments(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """List deployment records from `GET /repos/{owner}/{repo}/deployments`, most recent first.

    Unlike every other list endpoint in this module, GitHub returns this one
    as a bare JSON array, not an object with a named key -- `_raise_for_status()`
    and the retry path are identical, only the body shape differs.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/deployments?per_page=30"
    logger.debug("fetching deployments for %s/%s", owner, repo)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    if not isinstance(payload, list):
        raise ResponseFormatError(f"{url} response is not a JSON array")

    try:
        deployments = [Deployment.model_validate(raw) for raw in payload]
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched %d deployment(s) for %s/%s", len(deployments), owner, repo)
    return [deployment.model_dump() for deployment in deployments]


# ---------------------------------------------------------------------------
# Observability endpoints (Module 30) -- three more read-only calls,
# following the exact same shape every earlier endpoint in this module
# established: build the request, go through `_request_with_retry()`, raise
# `_raise_for_status()`, validate the body against a Pydantic model before
# it leaves this module. Unlike GITHUB_API, there is no one fixed public
# host here -- `base_url` is explicit on every function, because an
# observability backend's address is always local to whoever is running it.
# `query_metrics()` follows Prometheus's own HTTP query API response
# envelope -- the format Prometheus itself, and anything that speaks its
# query API (Thanos, Cortex, Mimir), actually returns. `search_logs()` uses
# a small envelope this project defines itself (`query`/`total`/`logs`)
# rather than pinning to one vendor's proprietary schema -- Loki,
# Elasticsearch and Datadog each shape a log search differently; a real
# integration sits a small adapter in front of whichever one it talks to.
# `list_alerts()` follows Prometheus Alertmanager's v2 API shape, a bare
# JSON array of alert objects. `platformops.observability` is the module
# that turns what these three functions return into one snapshot; nothing
# here decides whether a result is "healthy" -- it only reports what the
# backend said, including an honestly empty result.
# ---------------------------------------------------------------------------


class MetricSample(BaseModel):
    """One instant-vector sample: a metric's label set plus its `[unix_timestamp, string_value]` pair.

    `value` stays a two-element tuple exactly as Prometheus's wire format
    sends it -- the value itself is a JSON string, not a number, because
    Prometheus's own API represents it that way to avoid floating-point
    precision loss in transit. This model does not convert it; the caller
    decides whether and how to parse it.
    """

    metric: dict[str, str]
    value: tuple[float, str]


class MetricQueryData(BaseModel):
    """The `data` object inside a Prometheus-shaped query response."""

    result_type: str = Field(alias="resultType")
    result: list[MetricSample]


class MetricQueryResponse(BaseModel):
    """The full envelope Prometheus's `/api/v1/query` (and anything that speaks its query API) returns."""

    status: str
    data: MetricQueryData


def query_metrics(
    base_url: str,
    query: str,
    *,
    time: float | None = None,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Run one instant query against `GET {base_url}/api/v1/query`, Prometheus's own query API shape.

    An empty `result` list is a real, known answer -- "no series matched
    this query right now" -- not a failure. `platformops.observability`
    is the layer that turns that into an honest "no data found" report
    instead of treating it the same as an unreachable backend.
    """
    url = f"{base_url}/api/v1/query"
    params: dict[str, Any] = {"query": query}
    if time is not None:
        params["time"] = time
    logger.debug("querying metrics at %s: %s", base_url, query)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url, params=params)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    try:
        parsed = MetricQueryResponse.model_validate(payload)
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info(
        "fetched %d metric sample(s) for query %r", len(parsed.data.result), query
    )
    return [sample.model_dump() for sample in parsed.data.result]


class LogEntry(BaseModel):
    """One log line: a timestamp, a level, the service that emitted it, and its message.

    `correlation_id` and `trace_id` are optional -- a log-search backend
    happily returns log lines that predate this project ever attaching
    either one to anything it emits. When present, `correlation_id` is
    exactly the identifier `platformops.telemetry.traced_operation()`
    tags a log line and a trace span with, which is what lets a search
    like `correlation_id:req-...` return every line one run of this
    project's own automation wrote.
    """

    timestamp: str
    level: str
    service: str
    message: str
    correlation_id: str | None = None
    trace_id: str | None = None


class LogSearchResponse(BaseModel):
    """The envelope `search_logs()` expects back: the query that ran, a total, and the matched lines."""

    query: str
    total: int
    logs: list[LogEntry]


def search_logs(
    base_url: str,
    query: str,
    *,
    limit: int = 100,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Search log lines from `GET {base_url}/api/v1/logs/search`.

    Zero matching lines is a real, known answer this function returns as
    an empty list, not an error -- a query for a correlation ID that never
    logged anything (because the run failed before logging a single line,
    or the ID was mistyped) is exactly the "missing telemetry" case
    `platformops.observability` has to report honestly rather than
    silently.
    """
    url = f"{base_url}/api/v1/logs/search"
    params = {"query": query, "limit": limit}
    logger.debug("searching logs at %s: %s", base_url, query)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url, params=params)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    try:
        parsed = LogSearchResponse.model_validate(payload)
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched %d log line(s) for query %r", len(parsed.logs), query)
    return [entry.model_dump() for entry in parsed.logs]


class AlertStatus(BaseModel):
    """An alert's current lifecycle state -- `"active"`, `"suppressed"` or `"unprocessed"`."""

    state: str


class Alert(BaseModel):
    """One alert record, following Alertmanager's own v2 API shape.

    `labels` always carries `alertname`; this project's fixtures also set
    `service` and `severity` on it, the two labels
    `platformops.observability` filters and reports on. `annotations` is
    free-form, human-readable context (a `summary`, typically) the alerting
    rule author attached -- this project reads it, it never writes one.
    """

    fingerprint: str
    status: AlertStatus
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    starts_at: str = Field(alias="startsAt")


def list_alerts(
    base_url: str,
    *,
    service: str | None = None,
    token: str | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """List alerts from `GET {base_url}/api/v2/alerts`, the same bare-array shape Alertmanager's v2 API returns.

    An empty list is a real, known answer -- nothing is currently firing
    for this service -- and is never treated as a failure. This function
    never acknowledges, silences or resolves an alert; it only reads what
    the alerting backend currently reports.
    """
    url = f"{base_url}/api/v2/alerts"
    params: dict[str, Any] = {}
    if service is not None:
        params["filter"] = f'service="{service}"'
    logger.debug("fetching alerts at %s (service=%s)", base_url, service)
    with httpx.Client(timeout=timeout, headers=_auth_headers(token)) as client:
        response = _request_with_retry(client, "GET", url, params=params)

    _raise_for_status(response, url)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ResponseFormatError(f"{url} did not return valid JSON") from exc

    if not isinstance(payload, list):
        raise ResponseFormatError(f"{url} response is not a JSON array")

    try:
        alerts = [Alert.model_validate(raw) for raw in payload]
    except ValidationError as exc:
        raise ResponseFormatError(
            f"{url} response did not match the expected shape: {exc}"
        ) from exc

    logger.info("fetched %d alert(s) from %s", len(alerts), base_url)
    return [alert.model_dump() for alert in alerts]
