import asyncio
import threading
import time

import httpx
import pytest
import respx

from platformops.httpclient import (
    BASE_BACKOFF_SECONDS,
    EndpointStatusError,
    EndpointUnreachableError,
    HealthResult,
    ResponseFormatError,
    _next_page_url,
    _sleep_backoff,
    check_health,
    check_health_async,
    check_many,
    check_many_async,
    check_many_sequential,
    get_repo_info,
    list_workflow_runs,
    summarize_health_results,
)

REPO_PAYLOAD = {
    "full_name": "httpx/httpx",
    "description": "A next generation HTTP client for Python.",
    "default_branch": "master",
    "open_issues_count": 12,
    "stargazers_count": 13000,
    "html_url": "https://github.com/httpx/httpx",
}


# ---------------------------------------------------------------------------
# check_health -- a single GET, no retries. httpx.MockTransport swaps out the
# real network transport, so these never make an actual request.
# ---------------------------------------------------------------------------


def test_check_health_reports_ok_for_a_2xx_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    result = check_health("https://example.com/health", transport=transport)

    assert result.ok is True
    assert result.status_code == 200
    assert result.latency_ms is not None
    assert result.error is None


def test_check_health_reports_not_ok_for_a_5xx_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(503))

    result = check_health("https://example.com/health", transport=transport)

    assert result.ok is False
    assert result.status_code == 503
    assert result.error is None  # a response WAS received, just an unhealthy one


def test_check_health_reports_connection_failure_without_raising():
    def raise_connect_error(request):
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(raise_connect_error)

    result = check_health("https://unreachable.example.com", transport=transport)

    assert result.ok is False
    assert result.status_code is None
    assert "connection failed" in result.error


def test_check_health_reports_timeout_without_raising():
    def raise_timeout(request):
        raise httpx.TimeoutException("timed out", request=request)

    transport = httpx.MockTransport(raise_timeout)

    result = check_health("https://slow.example.com", transport=transport)

    assert result.ok is False
    assert "timed out" in result.error


# ---------------------------------------------------------------------------
# get_repo_info -- success, permanent failure, retried transient failure,
# response validation. respx intercepts by URL, which reads closer to the
# real request than httpx.MockTransport for a multi-call scenario like retry.
# ---------------------------------------------------------------------------


@respx.mock
def test_get_repo_info_returns_the_expected_fields_on_success():
    respx.get("https://api.github.com/repos/httpx/httpx").mock(
        return_value=httpx.Response(200, json=REPO_PAYLOAD)
    )

    info = get_repo_info("httpx", "httpx")

    assert info["full_name"] == "httpx/httpx"
    assert info["default_branch"] == "master"
    assert info["stargazers_count"] == 13000


@respx.mock
def test_get_repo_info_raises_endpoint_status_error_on_404(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get("https://api.github.com/repos/httpx/does-not-exist").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(EndpointStatusError) as exc_info:
        get_repo_info("httpx", "does-not-exist")

    assert exc_info.value.status_code == 404


@respx.mock
def test_get_repo_info_raises_endpoint_status_error_on_401_and_does_not_retry(
    monkeypatch,
):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    route = respx.get("https://api.github.com/repos/private/repo").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )

    with pytest.raises(EndpointStatusError) as exc_info:
        get_repo_info("private", "repo")

    assert exc_info.value.status_code == 401
    assert route.call_count == 1  # 401 is a permanent client error -- never retried


@respx.mock
def test_get_repo_info_retries_a_429_and_succeeds_on_the_next_attempt(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    route = respx.get("https://api.github.com/repos/httpx/httpx").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json=REPO_PAYLOAD),
        ]
    )

    info = get_repo_info("httpx", "httpx")

    assert info["full_name"] == "httpx/httpx"
    assert route.call_count == 2


@respx.mock
def test_get_repo_info_retries_a_500_and_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    monkeypatch.setattr("platformops.httpclient.MAX_RETRIES", 2)
    route = respx.get("https://api.github.com/repos/httpx/httpx").mock(
        return_value=httpx.Response(500)
    )

    with pytest.raises(EndpointStatusError) as exc_info:
        get_repo_info("httpx", "httpx")

    assert exc_info.value.status_code == 500
    assert route.call_count == 3  # the first attempt plus 2 retries


@respx.mock
def test_get_repo_info_raises_response_format_error_on_malformed_json(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get("https://api.github.com/repos/httpx/httpx").mock(
        return_value=httpx.Response(200, content=b"not json at all")
    )

    with pytest.raises(ResponseFormatError):
        get_repo_info("httpx", "httpx")


@respx.mock
def test_get_repo_info_raises_response_format_error_on_empty_body(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get("https://api.github.com/repos/httpx/httpx").mock(
        return_value=httpx.Response(200, content=b"")
    )

    with pytest.raises(ResponseFormatError):
        get_repo_info("httpx", "httpx")


@respx.mock
def test_get_repo_info_raises_response_format_error_on_a_shape_missing_required_fields(
    monkeypatch,
):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get("https://api.github.com/repos/httpx/httpx").mock(
        return_value=httpx.Response(
            200, json={"full_name": "httpx/httpx"}
        )  # missing everything else
    )

    with pytest.raises(ResponseFormatError):
        get_repo_info("httpx", "httpx")


@respx.mock
def test_get_repo_info_connection_failure_exhausts_retries_and_raises_unreachable(
    monkeypatch,
):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    monkeypatch.setattr("platformops.httpclient.MAX_RETRIES", 1)
    respx.get("https://api.github.com/repos/httpx/httpx").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(EndpointUnreachableError):
        get_repo_info("httpx", "httpx")


@respx.mock
def test_get_repo_info_does_not_log_the_token_value(monkeypatch, caplog):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get("https://api.github.com/repos/httpx/httpx").mock(
        return_value=httpx.Response(200, json=REPO_PAYLOAD)
    )

    with caplog.at_level("DEBUG", logger="platformops.httpclient"):
        get_repo_info("httpx", "httpx", token="super-secret-token-value")

    assert "super-secret-token-value" not in caplog.text


# ---------------------------------------------------------------------------
# list_workflow_runs -- Link-header pagination.
# ---------------------------------------------------------------------------


@respx.mock
def test_list_workflow_runs_follows_link_header_pagination(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    page_1_url = "https://api.github.com/repos/owner/repo/actions/runs?per_page=30"
    page_2_url = (
        "https://api.github.com/repos/owner/repo/actions/runs?per_page=30&page=2"
    )

    respx.get(page_1_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 1,
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": "main",
                        "html_url": "https://x/1",
                    }
                ]
            },
            headers={"Link": f'<{page_2_url}>; rel="next", <{page_2_url}>; rel="last"'},
        )
    )
    respx.get(page_2_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 2,
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "failure",
                        "head_branch": "main",
                        "html_url": "https://x/2",
                    }
                ]
            },
        )
    )

    runs = list_workflow_runs("owner", "repo")

    assert [run["id"] for run in runs] == [1, 2]


@respx.mock
def test_list_workflow_runs_stops_at_max_pages_even_if_more_are_available(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    page_1_url = "https://api.github.com/repos/owner/repo/actions/runs?per_page=30"

    respx.get(page_1_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 1,
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "success",
                        "head_branch": "main",
                        "html_url": "https://x/1",
                    }
                ]
            },
            headers={
                "Link": f'<{page_1_url}>; rel="next"'
            },  # would page forever without the ceiling
        )
    )

    runs = list_workflow_runs("owner", "repo", max_pages=1)

    assert len(runs) == 1


@respx.mock
def test_list_workflow_runs_raises_response_format_error_when_key_is_missing(
    monkeypatch,
):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    page_1_url = "https://api.github.com/repos/owner/repo/actions/runs?per_page=30"
    respx.get(page_1_url).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(ResponseFormatError):
        list_workflow_runs("owner", "repo")


# ---------------------------------------------------------------------------
# check_many_sequential / check_many -- the thread-pool path. check_health
# itself is already covered above; here we monkeypatch it so these tests
# exercise only check_many_sequential's/check_many's own concurrency and
# ordering logic, never the network.
# ---------------------------------------------------------------------------


def test_check_many_sequential_checks_every_url_in_order(monkeypatch):
    calls = []

    def fake_check_health(url, *, timeout=None):
        calls.append(url)
        return HealthResult(url=url, ok=True, status_code=200, latency_ms=1.0)

    monkeypatch.setattr("platformops.httpclient.check_health", fake_check_health)

    urls = ["https://a.example", "https://b.example", "https://c.example"]
    results = check_many_sequential(urls)

    assert calls == urls  # sequential means called strictly in order
    assert [result.url for result in results] == urls
    assert all(result.ok for result in results)


def test_check_many_preserves_input_order_even_when_a_slower_url_finishes_first(
    monkeypatch,
):
    def fake_check_health(url, *, timeout=None):
        if url == "https://slow.example":
            time.sleep(0.05)
        return HealthResult(url=url, ok=True, status_code=200, latency_ms=1.0)

    monkeypatch.setattr("platformops.httpclient.check_health", fake_check_health)

    urls = ["https://slow.example", "https://fast-a.example", "https://fast-b.example"]
    results = check_many(urls, max_workers=3)

    assert [result.url for result in results] == urls


def test_check_many_never_exceeds_max_workers_in_flight(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_check_health(url, *, timeout=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return HealthResult(url=url, ok=True, status_code=200, latency_ms=1.0)

    monkeypatch.setattr("platformops.httpclient.check_health", fake_check_health)

    urls = [f"https://svc-{i}.example" for i in range(10)]
    check_many(urls, max_workers=3)

    assert 1 < peak <= 3  # genuinely concurrent, but never past the cap


def test_check_many_returns_an_empty_list_for_no_urls():
    assert check_many([]) == []


# ---------------------------------------------------------------------------
# check_health_async / check_many_async -- the asyncio path. Each async test
# is a plain sync test function wrapping its coroutine in asyncio.run(),
# which needs no extra pytest plugin. check_health_async is exercised
# directly via respx (which patches both sync and async httpx transports);
# check_many_async's own concurrency, ordering, partial-failure aggregation
# and batch-timeout cancellation are exercised by monkeypatching
# check_health_async, the same pattern used for check_many above.
# ---------------------------------------------------------------------------


@respx.mock
def test_check_health_async_reports_ok_for_a_2xx_response():
    respx.get("https://example.com/health").mock(return_value=httpx.Response(200))

    async def run():
        async with httpx.AsyncClient() as client:
            return await check_health_async("https://example.com/health", client=client)

    result = asyncio.run(run())

    assert result.ok is True
    assert result.status_code == 200
    assert result.latency_ms is not None


@respx.mock
def test_check_health_async_reports_connection_failure_without_raising():
    respx.get("https://unreachable.example.com").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await check_health_async(
                "https://unreachable.example.com", client=client
            )

    result = asyncio.run(run())

    assert result.ok is False
    assert result.status_code is None
    assert "connection failed" in result.error


def test_check_many_async_preserves_input_order(monkeypatch):
    async def fake_check_health_async(url, *, client, timeout=None):
        return HealthResult(url=url, ok=True, status_code=200, latency_ms=1.0)

    monkeypatch.setattr(
        "platformops.httpclient.check_health_async", fake_check_health_async
    )

    urls = ["https://a.example", "https://b.example", "https://c.example"]
    results = asyncio.run(check_many_async(urls))

    assert [result.url for result in results] == urls


def test_check_many_async_never_exceeds_max_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def fake_check_health_async(url, *, client, timeout=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return HealthResult(url=url, ok=True, status_code=200, latency_ms=1.0)

    monkeypatch.setattr(
        "platformops.httpclient.check_health_async", fake_check_health_async
    )

    urls = [f"https://svc-{i}.example" for i in range(10)]
    asyncio.run(check_many_async(urls, max_concurrency=3))

    assert 1 < peak <= 3


def test_check_many_async_aggregates_partial_failures_without_raising(monkeypatch):
    async def fake_check_health_async(url, *, client, timeout=None):
        if url == "https://bad.example":
            return HealthResult(url=url, ok=False, status_code=503, latency_ms=5.0)
        return HealthResult(url=url, ok=True, status_code=200, latency_ms=5.0)

    monkeypatch.setattr(
        "platformops.httpclient.check_health_async", fake_check_health_async
    )

    urls = ["https://good-a.example", "https://bad.example", "https://good-b.example"]
    results = asyncio.run(check_many_async(urls))
    summary = summarize_health_results(results)

    assert summary.total == 3
    assert summary.ok == 2
    assert summary.failed == 1


def test_check_many_async_batch_timeout_cancels_slow_checks_and_still_reports_them(
    monkeypatch,
):
    async def fake_check_health_async(url, *, client, timeout=None):
        if url == "https://slow.example":
            await asyncio.sleep(10)  # never finishes before batch_timeout below
        return HealthResult(url=url, ok=True, status_code=200, latency_ms=5.0)

    monkeypatch.setattr(
        "platformops.httpclient.check_health_async", fake_check_health_async
    )

    urls = ["https://fast.example", "https://slow.example"]
    results = asyncio.run(check_many_async(urls, batch_timeout=0.05))
    by_url = {result.url: result for result in results}

    assert by_url["https://fast.example"].ok is True
    assert by_url["https://slow.example"].ok is False
    assert "cancelled" in by_url["https://slow.example"].error


# ---------------------------------------------------------------------------
# check_health -- generic httpx.HTTPError branch (neither TimeoutException
# nor ConnectError). ProtocolError is a concrete HTTPError subclass that is
# neither of the two already-tested branches.
# ---------------------------------------------------------------------------


def test_check_health_reports_generic_http_error_without_raising():
    def raise_protocol_error(request):
        raise httpx.ProtocolError("unexpected EOF", request=request)

    transport = httpx.MockTransport(raise_protocol_error)

    result = check_health("https://broken.example.com", transport=transport)

    assert result.ok is False
    assert result.status_code is None
    assert "unexpected EOF" in result.error


# ---------------------------------------------------------------------------
# check_health_async -- TimeoutException and generic HTTPError branches.
# ---------------------------------------------------------------------------


@respx.mock
def test_check_health_async_reports_timeout_without_raising():
    respx.get("https://slow.example.com").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await check_health_async("https://slow.example.com", client=client)

    result = asyncio.run(run())

    assert result.ok is False
    assert result.status_code is None
    assert "timed out" in result.error


@respx.mock
def test_check_health_async_reports_generic_http_error_without_raising():
    respx.get("https://broken.example.com").mock(
        side_effect=httpx.ProtocolError("unexpected EOF")
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await check_health_async("https://broken.example.com", client=client)

    result = asyncio.run(run())

    assert result.ok is False
    assert result.status_code is None
    assert "unexpected EOF" in result.error


# ---------------------------------------------------------------------------
# _sleep_backoff -- direct unit test (monkeypatch time.sleep so this test
# runs instantly instead of actually sleeping for several seconds).
# ---------------------------------------------------------------------------


def test_sleep_backoff_does_not_actually_sleep_and_grows_with_attempt(monkeypatch):
    captured_durations = []

    monkeypatch.setattr(
        "platformops.httpclient.time.sleep",
        lambda duration: captured_durations.append(duration),
    )

    _sleep_backoff(0)
    _sleep_backoff(1)

    assert len(captured_durations) == 2
    assert captured_durations[1] > BASE_BACKOFF_SECONDS * 1


# ---------------------------------------------------------------------------
# _next_page_url -- three cases: no header, header without rel="next",
# header with rel="next" among other entries.
# ---------------------------------------------------------------------------


def test_next_page_url_returns_none_when_link_header_is_missing():
    response = httpx.Response(200, headers={})

    assert _next_page_url(response) is None


def test_next_page_url_returns_none_when_link_header_has_no_rel_next():
    response = httpx.Response(
        200,
        headers={
            "Link": '<https://api.github.com/repos/o/r/actions/runs?page=1>; rel="prev", '
            '<https://api.github.com/repos/o/r/actions/runs?page=5>; rel="last"'
        },
    )

    assert _next_page_url(response) is None


def test_next_page_url_returns_the_next_url_when_present():
    next_url = "https://api.github.com/repos/o/r/actions/runs?page=3"
    response = httpx.Response(
        200,
        headers={
            "Link": f'<{next_url}>; rel="next", '
            '<https://api.github.com/repos/o/r/actions/runs?page=5>; rel="last"'
        },
    )

    assert _next_page_url(response) == next_url
