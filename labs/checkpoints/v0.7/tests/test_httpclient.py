import httpx
import pytest
import respx

from platformops.httpclient import (
    EndpointStatusError,
    EndpointUnreachableError,
    ResponseFormatError,
    check_health,
    get_repo_info,
    list_workflow_runs,
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
