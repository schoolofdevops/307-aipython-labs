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
from platformops.local_ops import ContainerListResult, GitStatusResult, ScanResult

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


def test_version_json_outputs_valid_json_with_required_keys():
    result = runner.invoke(app, ["version", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "version" in data
    assert "python" in data


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


# ---------------------------------------------------------------------------
# health-check-many -- CLI wiring only, same discipline as http-check above.
# check_many / check_many_sequential / check_many_async are unit-tested
# against fake check_health(_async) calls in test_httpclient.py; here the
# point is exit codes, --mode dispatch, --json and the batch-timeout/mode
# usage check, so each is monkeypatched instead of touching the network.
# ---------------------------------------------------------------------------


def test_health_check_many_exits_0_when_every_endpoint_is_healthy(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "check_many",
        lambda urls, timeout=10.0, max_workers=8: [
            HealthResult(url=url, ok=True, status_code=200, latency_ms=5.0)
            for url in urls
        ],
    )

    result = runner.invoke(
        app, ["health-check-many", "https://a.example", "https://b.example"]
    )

    assert result.exit_code == 0
    assert "2/2 healthy" in result.stdout


def test_health_check_many_exits_1_and_still_reports_every_result(monkeypatch):
    def fake_check_many(urls, timeout=10.0, max_workers=8):
        return [
            HealthResult(url=urls[0], ok=True, status_code=200, latency_ms=5.0),
            HealthResult(url=urls[1], ok=False, status_code=503, latency_ms=5.0),
        ]

    monkeypatch.setattr(cli_module, "check_many", fake_check_many)

    result = runner.invoke(
        app, ["health-check-many", "https://a.example", "https://b.example"]
    )

    assert result.exit_code == 1
    assert "1/2 healthy" in result.stdout
    assert "https://a.example: OK" in result.stdout
    assert "https://b.example: FAIL" in result.stdout


def test_health_check_many_exits_2_for_a_url_missing_a_scheme():
    result = runner.invoke(app, ["health-check-many", "https://a.example", "not-a-url"])

    assert result.exit_code == 2
    assert "scheme" in result.output


def test_health_check_many_json_is_parseable(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "check_many",
        lambda urls, timeout=10.0, max_workers=8: [
            HealthResult(url=url, ok=True, status_code=200, latency_ms=5.0)
            for url in urls
        ],
    )

    result = runner.invoke(app, ["health-check-many", "https://a.example", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["status"] == "ok"
    assert payload["mode"] == "threaded"
    assert payload["total"] == 1
    assert payload["results"][0]["url"] == "https://a.example"


def test_health_check_many_sequential_mode_dispatches_to_check_many_sequential(
    monkeypatch,
):
    monkeypatch.setattr(
        cli_module,
        "check_many_sequential",
        lambda urls, timeout=10.0: [
            HealthResult(url=url, ok=True, status_code=200, latency_ms=5.0)
            for url in urls
        ],
    )

    result = runner.invoke(
        app, ["health-check-many", "https://a.example", "--mode", "sequential"]
    )

    assert result.exit_code == 0
    assert "healthy (sequential)" in result.stdout


async def _fake_check_many_async(
    urls, timeout=10.0, max_concurrency=8, batch_timeout=None
):
    return [
        HealthResult(url=url, ok=True, status_code=200, latency_ms=5.0) for url in urls
    ]


def test_health_check_many_async_mode_dispatches_to_check_many_async(monkeypatch):
    monkeypatch.setattr(cli_module, "check_many_async", _fake_check_many_async)

    result = runner.invoke(
        app, ["health-check-many", "https://a.example", "--mode", "async"]
    )

    assert result.exit_code == 0
    assert "healthy (async)" in result.stdout


def test_health_check_many_invalid_mode_exits_2():
    result = runner.invoke(
        app, ["health-check-many", "https://a.example", "--mode", "bogus"]
    )

    assert result.exit_code == 2
    assert "sequential, threaded or async" in result.output


def test_health_check_many_batch_timeout_without_async_mode_is_a_usage_error():
    result = runner.invoke(
        app,
        [
            "health-check-many",
            "https://a.example",
            "--mode",
            "threaded",
            "--batch-timeout",
            "1.0",
        ],
    )

    assert result.exit_code == 2
    assert "--batch-timeout only applies to --mode async" in result.output


def test_local_status_json_reports_git_and_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli_module,
        "git_status",
        lambda path: GitStatusResult(
            repo_path=path, clean=True, changed_files=[], error=None
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "docker_info",
        lambda: {"available": True, "ServerVersion": "27.0.0"},
    )
    monkeypatch.setattr(
        cli_module,
        "container_list",
        lambda: ContainerListResult(containers=[]),
    )

    result = runner.invoke(app, ["local-status", "--path", str(tmp_path), "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["git"]["clean"] is True
    assert payload["docker"]["available"] is True
    assert payload["containers"] == []


def test_local_status_reports_a_dirty_tree_in_plain_text(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli_module,
        "git_status",
        lambda path: GitStatusResult(
            repo_path=path, clean=False, changed_files=[" M src/foo.py"], error=None
        ),
    )
    monkeypatch.setattr(cli_module, "docker_info", lambda: {"available": True})
    monkeypatch.setattr(
        cli_module,
        "container_list",
        lambda: ContainerListResult(containers=[]),
    )

    result = runner.invoke(app, ["local-status", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "1 changed file" in result.stdout


def test_local_status_reports_when_docker_is_unreachable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli_module,
        "git_status",
        lambda path: GitStatusResult(
            repo_path=path, clean=True, changed_files=[], error=None
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "docker_info",
        lambda: {"available": False, "error": "docker daemon is not reachable"},
    )
    monkeypatch.setattr(
        cli_module,
        "container_list",
        lambda: ContainerListResult(containers=[]),
    )

    result = runner.invoke(app, ["local-status", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "docker: docker daemon is not reachable" in result.stdout


# --- check-security CLI ---


def test_check_security_exits_0_when_clean(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "scan_for_shell_true",
        lambda path: ScanResult(clean=True, findings=[]),
    )

    result = runner.invoke(app, ["check-security"])

    assert result.exit_code == 0
    assert "clean" in result.stdout


def test_check_security_exits_1_when_findings(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "scan_for_shell_true",
        lambda path: ScanResult(
            clean=False,
            findings=["src/bad.py:3:    subprocess.run(cmd, shell=True)"],
        ),
    )

    result = runner.invoke(app, ["check-security"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "shell=True" in result.stdout


def test_check_security_json_reports_clean(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "scan_for_shell_true",
        lambda path: ScanResult(clean=True, findings=[]),
    )

    result = runner.invoke(app, ["check-security", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["clean"] is True
    assert payload["findings"] == []


def test_check_security_end_to_end_against_a_real_directory_with_a_violation(tmp_path):
    bad_file = tmp_path / "bad.py"
    bad_file.write_text('import subprocess\nsubprocess.run("echo hi", shell=True)\n')

    result = runner.invoke(app, ["check-security", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "shell=True" in result.stdout


def test_check_security_end_to_end_against_a_real_clean_directory(tmp_path):
    (tmp_path / "clean.py").write_text("print('hello')\n")

    result = runner.invoke(app, ["check-security", "--path", str(tmp_path)])

    assert result.exit_code == 0
