import json

import pytest
from typer.testing import CliRunner

import platformops.cli as cli_module
from platformops.cli import app
from platformops.httpclient import (
    EndpointStatusError,
    EndpointUnreachableError,
    HealthResult,
)

runner = CliRunner()

GOOD_YAML_TEXT = (
    "name: checkout-api\n"
    "repository: github.com/example/checkout-api\n"
    "environment: prod\n"
    "team_owner: payments-team\n"
    "kubernetes_namespace: checkout\n"
    "deployment_name: checkout-api\n"
    'aws_account: "111122223333"\n'
    "region: us-east-1\n"
    "observability:\n"
    "  dashboard_url: https://grafana.example.com/d/checkout-api\n"
    '  alert_channel: "#checkout-alerts"\n'
)


@pytest.fixture
def good_service_yaml(tmp_path):
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)
    return path


def test_version_command_exits_0_and_prints_a_version_string():
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "platformops" in result.stdout


def test_validate_a_good_file_exits_0_and_reports_ok(good_service_yaml):
    result = runner.invoke(app, ["validate", str(good_service_yaml)])

    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_validate_a_bad_file_exits_1_and_names_the_field(tmp_path):
    bad = tmp_path / "service.yaml"
    bad.write_text("name: checkout-api\n")  # missing every other required field

    result = runner.invoke(app, ["validate", str(bad)])

    assert result.exit_code == 1
    assert "repository" in result.stdout


def test_validate_a_missing_file_exits_1_and_names_the_path(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"

    result = runner.invoke(app, ["validate", str(missing)])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_inspect_command_exits_0_and_prints_the_report():
    result = runner.invoke(app, ["inspect"])

    assert result.exit_code == 0
    assert "PlatformOps Inventory Report" in result.stdout


def test_validate_json_on_a_good_file_is_parseable_and_has_status_ok(good_service_yaml):
    result = runner.invoke(app, ["validate", str(good_service_yaml), "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["status"] == "ok"
    assert payload["service"]["name"] == "checkout-api"


def test_validate_json_on_a_missing_file_is_parseable_and_has_status_error(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"

    result = runner.invoke(app, ["validate", str(missing), "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["status"] == "error"
    assert "not found" in payload["error"]


def test_inspect_json_is_parseable_and_matches_the_plain_report_totals():
    result = runner.invoke(app, ["inspect", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["total_servers"] == 10
    assert payload["summary"]["total_servers"] == 10


def test_quiet_suppresses_the_ok_line_on_a_good_file(good_service_yaml):
    result = runner.invoke(app, ["--quiet", "validate", str(good_service_yaml)])

    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_quiet_never_hides_a_real_failure(tmp_path):
    bad = tmp_path / "service.yaml"
    bad.write_text("name: checkout-api\n")

    result = runner.invoke(app, ["--quiet", "validate", str(bad)])

    assert result.exit_code == 1
    assert "repository" in result.stdout


def test_verbose_and_quiet_together_is_a_usage_error():
    result = runner.invoke(app, ["--verbose", "--quiet", "version"])

    assert result.exit_code == 2
    assert "cannot be used together" in result.output


# ---------------------------------------------------------------------------
# http-check / repo-info / workflow-runs -- CLI wiring only. The httpclient
# functions themselves are unit-tested against a mocked transport/respx in
# test_httpclient.py; here the point is exit codes, --json, and --quiet, so
# each command's underlying httpclient call is monkeypatched instead of
# touching the network.
# ---------------------------------------------------------------------------


def test_http_check_exits_0_and_reports_ok_for_a_healthy_endpoint(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "check_health",
        lambda url, timeout=10.0: HealthResult(
            url=url, ok=True, status_code=200, latency_ms=12.3
        ),
    )

    result = runner.invoke(app, ["http-check", "https://example.com"])

    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_http_check_exits_1_and_reports_fail_for_an_unhealthy_endpoint(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "check_health",
        lambda url, timeout=10.0: HealthResult(
            url=url, ok=False, status_code=503, latency_ms=8.0
        ),
    )

    result = runner.invoke(app, ["http-check", "https://example.com"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout


def test_http_check_exits_2_for_a_url_missing_a_scheme():
    result = runner.invoke(app, ["http-check", "example.com"])

    assert result.exit_code == 2
    assert "scheme" in result.output


def test_http_check_json_is_parseable_and_carries_status_code(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "check_health",
        lambda url, timeout=10.0: HealthResult(
            url=url, ok=True, status_code=200, latency_ms=5.0
        ),
    )

    result = runner.invoke(app, ["http-check", "https://example.com", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["status"] == "ok"
    assert payload["status_code"] == 200


def test_http_check_quiet_suppresses_the_ok_line(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "check_health",
        lambda url, timeout=10.0: HealthResult(
            url=url, ok=True, status_code=200, latency_ms=5.0
        ),
    )

    result = runner.invoke(app, ["--quiet", "http-check", "https://example.com"])

    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_http_check_quiet_never_hides_a_failure(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "check_health",
        lambda url, timeout=10.0: HealthResult(
            url=url, ok=False, status_code=500, latency_ms=5.0
        ),
    )

    result = runner.invoke(app, ["--quiet", "http-check", "https://example.com"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout


def test_repo_info_exits_0_and_prints_the_summary(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "get_repo_info",
        lambda owner, repo, token=None: {
            "full_name": f"{owner}/{repo}",
            "description": "A test repo",
            "default_branch": "main",
            "open_issues_count": 3,
            "stargazers_count": 42,
            "html_url": f"https://github.com/{owner}/{repo}",
        },
    )

    result = runner.invoke(app, ["repo-info", "example/demo"])

    assert result.exit_code == 0
    assert "example/demo" in result.stdout


def test_repo_info_exits_2_for_a_slug_missing_a_slash():
    result = runner.invoke(app, ["repo-info", "not-a-slug"])

    assert result.exit_code == 2
    assert "owner/repo" in result.output


def test_repo_info_exits_1_on_a_404(monkeypatch):
    def raise_not_found(owner, repo, token=None):
        raise EndpointStatusError(f"{owner}/{repo} returned 404", status_code=404)

    monkeypatch.setattr(cli_module, "get_repo_info", raise_not_found)

    result = runner.invoke(app, ["repo-info", "example/missing"])

    assert result.exit_code == 1
    assert "404" in result.output


def test_repo_info_exits_1_when_the_endpoint_is_unreachable(monkeypatch):
    def raise_unreachable(owner, repo, token=None):
        raise EndpointUnreachableError(f"{owner}/{repo} unreachable")

    monkeypatch.setattr(cli_module, "get_repo_info", raise_unreachable)

    result = runner.invoke(app, ["repo-info", "example/demo"])

    assert result.exit_code == 1


def test_repo_info_json_is_parseable(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "get_repo_info",
        lambda owner, repo, token=None: {
            "full_name": f"{owner}/{repo}",
            "description": None,
            "default_branch": "main",
            "open_issues_count": 0,
            "stargazers_count": 0,
            "html_url": f"https://github.com/{owner}/{repo}",
        },
    )

    result = runner.invoke(app, ["repo-info", "example/demo", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["repo"]["full_name"] == "example/demo"


def test_workflow_runs_exits_0_and_prints_the_count(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "list_workflow_runs",
        lambda owner, repo, token=None, max_pages=5: [
            {
                "id": 1,
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main",
                "html_url": "https://x/1",
            }
        ],
    )

    result = runner.invoke(app, ["workflow-runs", "example/demo"])

    assert result.exit_code == 0
    assert "1 workflow run" in result.stdout


def test_workflow_runs_exits_2_for_a_slug_missing_a_slash():
    result = runner.invoke(app, ["workflow-runs", "not-a-slug"])

    assert result.exit_code == 2
