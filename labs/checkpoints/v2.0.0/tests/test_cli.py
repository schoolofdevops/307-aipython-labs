import json

import pytest
from typer.testing import CliRunner

import platformops.cli as cli_module
from platformops.cli import app
from platformops.cloudremediate import (
    BatchTooLargeError,
    RemediationAction,
    RemediationNotApprovedError,
    RemediationPlan,
    RemediationResult,
)
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


# ---------------------------------------------------------------------------
# inventory-scan -- CLI wiring only. scan_inventory() itself is unit-tested
# against real moto EC2 fixtures in test_cloudinventory.py; here the point
# is option parsing, exit codes and --json, so scan_inventory is
# monkeypatched instead of touching AWS (real or mocked).
# ---------------------------------------------------------------------------


def test_inventory_scan_requires_region():
    result = runner.invoke(app, ["inventory-scan"])

    assert result.exit_code != 0


def test_inventory_scan_exits_0_and_prints_the_count(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "scan_inventory",
        lambda **kwargs: {
            "status": "ok",
            "region": "us-east-1",
            "count": 1,
            "instances": [
                {
                    "instance_id": "i-abc123",
                    "state": "running",
                    "region": "us-east-1",
                    "tags": {"env": "prod"},
                    "launch_time": "2026-01-01T00:00:00+00:00",
                }
            ],
        },
    )

    result = runner.invoke(app, ["inventory-scan", "--region", "us-east-1"])

    assert result.exit_code == 0
    assert "1 instance(s) in us-east-1" in result.stdout
    assert "i-abc123" in result.stdout


def test_inventory_scan_json_prints_the_full_report(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "scan_inventory",
        lambda **kwargs: {
            "status": "ok",
            "region": "us-east-1",
            "count": 0,
            "instances": [],
        },
    )

    result = runner.invoke(app, ["inventory-scan", "--region", "us-east-1", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["status"] == "ok"
    assert payload["count"] == 0


def test_inventory_scan_exits_1_and_reports_the_error_message(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "scan_inventory",
        lambda **kwargs: {
            "status": "error",
            "error": "no-credentials",
            "message": "Unable to locate credentials",
        },
    )

    result = runner.invoke(app, ["inventory-scan", "--region", "us-east-1"])

    assert result.exit_code == 1
    assert "Unable to locate credentials" in result.output


def test_inventory_scan_tag_value_without_tag_key_exits_2():
    result = runner.invoke(
        app, ["inventory-scan", "--region", "us-east-1", "--tag-value", "prod"]
    )

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# multi-region-scan -- CLI wiring only. scan_regions() itself is unit-tested
# against real moto fixtures in test_multiregion.py; here the point is
# option parsing, exit codes, --format and --json, so scan_regions is
# monkeypatched instead of touching AWS (real or mocked).
# ---------------------------------------------------------------------------


def _fake_multiregion_result(*, failed_regions=None):
    return {
        "status": "ok" if not failed_regions else "partial",
        "regions_scanned": ["us-east-1"],
        "failed_regions": failed_regions or [],
        "count": 1,
        "resources": [
            {
                "resource_type": "ec2-instance",
                "resource_id": "i-abc123",
                "region": "us-east-1",
                "tags": {"env": "prod"},
                "state": "running",
            }
        ],
    }


def test_multi_region_scan_requires_regions():
    result = runner.invoke(app, ["multi-region-scan"])

    assert result.exit_code != 0


def test_multi_region_scan_prints_a_markdown_table_by_default(monkeypatch):
    monkeypatch.setattr(
        cli_module, "scan_regions", lambda regions, **kwargs: _fake_multiregion_result()
    )

    result = runner.invoke(app, ["multi-region-scan", "--regions", "us-east-1"])

    assert result.exit_code == 0
    assert "| Resource Type |" in result.stdout
    assert "i-abc123" in result.stdout


def test_multi_region_scan_prints_csv_with_format_csv(monkeypatch):
    monkeypatch.setattr(
        cli_module, "scan_regions", lambda regions, **kwargs: _fake_multiregion_result()
    )

    result = runner.invoke(
        app, ["multi-region-scan", "--regions", "us-east-1", "--format", "csv"]
    )

    assert result.exit_code == 0
    assert "resource_type,resource_id,region,state,tags" in result.stdout
    assert "i-abc123" in result.stdout


def test_multi_region_scan_json_prints_the_full_report(monkeypatch):
    monkeypatch.setattr(
        cli_module, "scan_regions", lambda regions, **kwargs: _fake_multiregion_result()
    )

    result = runner.invoke(
        app, ["multi-region-scan", "--regions", "us-east-1", "--json"]
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["status"] == "ok"
    assert payload["count"] == 1


def test_multi_region_scan_exits_1_when_a_region_failed(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "scan_regions",
        lambda regions, **kwargs: _fake_multiregion_result(
            failed_regions=[
                {
                    "region": "eu-west-1",
                    "error": "expired-token",
                    "message": "the security token has expired",
                }
            ]
        ),
    )

    result = runner.invoke(
        app, ["multi-region-scan", "--regions", "us-east-1,eu-west-1"]
    )

    assert result.exit_code == 1
    assert "expired-token" in result.output


def test_multi_region_scan_invalid_format_exits_2():
    result = runner.invoke(
        app, ["multi-region-scan", "--regions", "us-east-1", "--format", "xml"]
    )

    assert result.exit_code == 2


def test_multi_region_scan_passes_max_workers_through(monkeypatch):
    captured = {}

    def fake_scan_regions(regions, **kwargs):
        captured.update(kwargs)
        return _fake_multiregion_result()

    monkeypatch.setattr(cli_module, "scan_regions", fake_scan_regions)

    result = runner.invoke(
        app,
        ["multi-region-scan", "--regions", "us-east-1", "--max-workers", "2"],
    )

    assert result.exit_code == 0
    assert captured["max_workers"] == 2


# ---------------------------------------------------------------------------
# report-upload -- CLI wiring only. upload_report_to_bucket() itself is
# tested against a real Floci S3 bucket in test_reportstore.py; here the
# point is option parsing, file reading and exit codes, so it is
# monkeypatched instead of touching S3 (real or Floci).
# ---------------------------------------------------------------------------


def test_report_upload_exits_0_and_prints_the_key(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"status": "ok", "service": "checkout-api"}))
    monkeypatch.setattr(
        cli_module,
        "upload_report_to_bucket",
        lambda report, **kwargs: {
            "status": "ok",
            "bucket": kwargs["bucket"],
            "key": "reports/2026-08-05T00-00-00-000000Z.json",
        },
    )

    result = runner.invoke(
        app,
        [
            "report-upload",
            str(report_path),
            "--bucket",
            "test-bucket",
            "--region",
            "us-east-1",
        ],
    )

    assert result.exit_code == 0
    assert "reports/2026-08-05T00-00-00-000000Z.json" in result.stdout


def test_report_upload_json_prints_the_full_report(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(
        cli_module,
        "upload_report_to_bucket",
        lambda report, **kwargs: {
            "status": "ok",
            "bucket": kwargs["bucket"],
            "key": "reports/x.json",
        },
    )

    result = runner.invoke(
        app,
        [
            "report-upload",
            str(report_path),
            "--bucket",
            "test-bucket",
            "--region",
            "us-east-1",
            "--json",
        ],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["key"] == "reports/x.json"


def test_report_upload_exits_1_and_reports_the_error_message(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(
        cli_module,
        "upload_report_to_bucket",
        lambda report, **kwargs: {
            "status": "error",
            "error": "AccessDenied",
            "message": "not allowed",
        },
    )

    result = runner.invoke(
        app,
        [
            "report-upload",
            str(report_path),
            "--bucket",
            "test-bucket",
            "--region",
            "us-east-1",
        ],
    )

    assert result.exit_code == 1
    assert "not allowed" in result.output


def test_report_upload_on_a_nonexistent_file_exits_2():
    result = runner.invoke(
        app,
        [
            "report-upload",
            "/tmp/does-not-exist-platformops.json",
            "--bucket",
            "test-bucket",
            "--region",
            "us-east-1",
        ],
    )

    assert result.exit_code == 2


def test_report_upload_on_invalid_json_exits_2(tmp_path):
    report_path = tmp_path / "bad.json"
    report_path.write_text("not json")

    result = runner.invoke(
        app,
        [
            "report-upload",
            str(report_path),
            "--bucket",
            "test-bucket",
            "--region",
            "us-east-1",
        ],
    )

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# queue-send / queue-receive -- CLI wiring only, same monkeypatch discipline
# as report-upload above.
# ---------------------------------------------------------------------------


def test_queue_send_exits_0_and_prints_the_message_id(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "send_to_queue",
        lambda body, **kwargs: {
            "status": "ok",
            "queue": kwargs["queue_name"],
            "message_id": "abc-123",
        },
    )

    result = runner.invoke(
        app, ["queue-send", "work-queue", "do the thing", "--region", "us-east-1"]
    )

    assert result.exit_code == 0
    assert "abc-123" in result.stdout


def test_queue_send_exits_1_and_reports_the_error_message(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "send_to_queue",
        lambda body, **kwargs: {
            "status": "error",
            "error": "AccessDenied",
            "message": "not allowed",
        },
    )

    result = runner.invoke(
        app, ["queue-send", "work-queue", "do the thing", "--region", "us-east-1"]
    )

    assert result.exit_code == 1
    assert "not allowed" in result.output


def test_queue_receive_exits_0_and_prints_the_messages(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "receive_from_queue",
        lambda **kwargs: {
            "status": "ok",
            "queue": kwargs["queue_name"],
            "messages": [
                {
                    "message_id": "abc-123",
                    "receipt_handle": "rh-1",
                    "body": "do the thing",
                    "approximate_receive_count": 1,
                }
            ],
        },
    )

    result = runner.invoke(
        app, ["queue-receive", "work-queue", "--region", "us-east-1"]
    )

    assert result.exit_code == 0
    assert "do the thing" in result.stdout


def test_queue_receive_json_prints_the_full_report(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "receive_from_queue",
        lambda **kwargs: {
            "status": "ok",
            "queue": kwargs["queue_name"],
            "messages": [],
        },
    )

    result = runner.invoke(
        app, ["queue-receive", "work-queue", "--region", "us-east-1", "--json"]
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["messages"] == []


def test_queue_receive_exits_1_and_reports_the_error_message(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "receive_from_queue",
        lambda **kwargs: {
            "status": "error",
            "error": "AccessDenied",
            "message": "not allowed",
        },
    )

    result = runner.invoke(
        app, ["queue-receive", "work-queue", "--region", "us-east-1"]
    )

    assert result.exit_code == 1
    assert "not allowed" in result.output


# ---------------------------------------------------------------------------
# cloud-audit / findings-list / findings-show -- CLI wiring only.
# run_cloud_audit() itself is tested against a real Floci S3 bucket in
# test_cloudaudit.py; here the point is option parsing, --output, --json and
# exit codes, so it is monkeypatched instead of touching AWS.
# ---------------------------------------------------------------------------


def _fake_audit_report(*, active=1, suppressed=0):
    findings = []
    if active:
        findings.append(
            {
                "resource_id": "bad-bucket",
                "resource_type": "s3-bucket",
                "rule_id": "required-tags",
                "severity": "high",
                "evidence": "missing tags: team_owner",
                "suppressed": False,
                "suppression_reason": None,
            }
        )
    if suppressed:
        findings.append(
            {
                "resource_id": "legacy-bucket",
                "resource_type": "s3-bucket",
                "rule_id": "required-tags",
                "severity": "high",
                "evidence": "missing tags: team_owner",
                "suppressed": True,
                "suppression_reason": "cleanup tracked in OPS-4021",
            }
        )
    return {
        "status": "violations" if active else "ok",
        "summary": {
            "total_findings": len(findings),
            "active": active,
            "suppressed": suppressed,
            "by_severity": {"high": active} if active else {},
        },
        "findings": findings,
    }


def test_cloud_audit_exits_0_when_no_active_findings(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("rules: []\nexceptions: []\n")
    monkeypatch.setattr(
        cli_module, "run_cloud_audit", lambda **kwargs: _fake_audit_report(active=0)
    )

    result = runner.invoke(
        app,
        ["cloud-audit", "--policy", str(policy_path), "--region", "us-east-1"],
    )

    assert result.exit_code == 0
    assert "0 finding(s)" in result.stdout


def test_cloud_audit_exits_1_when_active_findings_exist(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("rules: []\nexceptions: []\n")
    monkeypatch.setattr(
        cli_module, "run_cloud_audit", lambda **kwargs: _fake_audit_report(active=1)
    )

    result = runner.invoke(
        app,
        ["cloud-audit", "--policy", str(policy_path), "--region", "us-east-1"],
    )

    assert result.exit_code == 1
    assert "bad-bucket" in result.stdout
    assert "HIGH" in result.stdout


def test_cloud_audit_json_prints_the_full_report(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("rules: []\nexceptions: []\n")
    monkeypatch.setattr(
        cli_module, "run_cloud_audit", lambda **kwargs: _fake_audit_report(active=1)
    )

    result = runner.invoke(
        app,
        [
            "cloud-audit",
            "--policy",
            str(policy_path),
            "--region",
            "us-east-1",
            "--json",
        ],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["status"] == "violations"
    assert payload["summary"]["active"] == 1


def test_cloud_audit_writes_the_report_to_output(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("rules: []\nexceptions: []\n")
    output_path = tmp_path / "report.json"
    monkeypatch.setattr(
        cli_module, "run_cloud_audit", lambda **kwargs: _fake_audit_report(active=1)
    )

    runner.invoke(
        app,
        [
            "cloud-audit",
            "--policy",
            str(policy_path),
            "--region",
            "us-east-1",
            "--output",
            str(output_path),
        ],
    )

    written = json.loads(output_path.read_text())
    assert written["summary"]["active"] == 1


def test_cloud_audit_exits_1_and_reports_the_error_message(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("rules: []\nexceptions: []\n")
    monkeypatch.setattr(
        cli_module,
        "run_cloud_audit",
        lambda **kwargs: {
            "status": "error",
            "error": "AccessDenied",
            "message": "not allowed",
        },
    )

    result = runner.invoke(
        app,
        ["cloud-audit", "--policy", str(policy_path), "--region", "us-east-1"],
    )

    assert result.exit_code == 1
    assert "not allowed" in result.output


def test_findings_list_excludes_suppressed_by_default(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_audit_report(active=1, suppressed=1)))

    result = runner.invoke(app, ["findings-list", str(report_path)])

    assert result.exit_code == 0
    assert "bad-bucket" in result.stdout
    assert "legacy-bucket" not in result.stdout


def test_findings_list_include_suppressed_shows_both(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_audit_report(active=1, suppressed=1)))

    result = runner.invoke(
        app, ["findings-list", str(report_path), "--include-suppressed"]
    )

    assert result.exit_code == 0
    assert "bad-bucket" in result.stdout
    assert "legacy-bucket" in result.stdout
    assert "SUPPRESSED" in result.stdout


def test_findings_list_filters_by_severity(tmp_path):
    report = _fake_audit_report(active=1, suppressed=0)
    report["findings"].append(
        {
            "resource_id": "public-bucket",
            "resource_type": "s3-bucket",
            "rule_id": "no-public-exposure",
            "severity": "critical",
            "evidence": "public exposure",
            "suppressed": False,
            "suppression_reason": None,
        }
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))

    result = runner.invoke(
        app, ["findings-list", str(report_path), "--severity", "critical"]
    )

    assert result.exit_code == 0
    assert "public-bucket" in result.stdout
    assert "bad-bucket" not in result.stdout


def test_findings_list_missing_file_exits_2():
    result = runner.invoke(app, ["findings-list", "/nonexistent/report.json"])

    assert result.exit_code == 2


def test_findings_show_prints_the_matching_finding(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_audit_report(active=1)))

    result = runner.invoke(
        app,
        ["findings-show", str(report_path), "bad-bucket", "required-tags"],
    )

    assert result.exit_code == 0
    assert "missing tags: team_owner" in result.stdout


def test_findings_show_json_includes_suppression_fields(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_audit_report(active=0, suppressed=1)))

    result = runner.invoke(
        app,
        [
            "findings-show",
            str(report_path),
            "legacy-bucket",
            "required-tags",
            "--json",
        ],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["finding"]["suppressed"] is True
    assert payload["finding"]["suppression_reason"] == "cleanup tracked in OPS-4021"


def test_findings_show_no_match_exits_1(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_audit_report(active=1)))

    result = runner.invoke(
        app,
        ["findings-show", str(report_path), "no-such-bucket", "required-tags"],
    )

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# remediate-plan / remediate-execute / remediate-execute-batch -- CLI wiring
# only. build_remediation_plan()/execute_remediation_plan() themselves are
# proven against moto and a real, running Floci in test_cloudremediate.py;
# here the point is option parsing, --approve, --json and exit codes, so the
# lower-level functions are monkeypatched instead of touching AWS.
# ---------------------------------------------------------------------------


def _fake_remediate_report(rule_id="required-tags"):
    return {
        "findings": [
            {
                "resource_id": "demo-bucket",
                "resource_type": "s3-bucket",
                "rule_id": rule_id,
                "severity": "high",
                "evidence": "missing tags: team_owner",
                "suppressed": False,
                "suppression_reason": None,
            }
        ]
    }


def _fake_remediable_plan():
    return RemediationPlan(
        finding_id="demo-bucket:required-tags",
        resource_id="demo-bucket",
        resource_type="s3-bucket",
        rule_id="required-tags",
        status="remediable",
        action=RemediationAction(
            api_call="put_bucket_tagging",
            args={"Bucket": "demo-bucket", "Tagging": {"TagSet": []}},
        ),
        before_state={"tags": {}},
    )


def _fake_not_supported_plan():
    return RemediationPlan(
        finding_id="demo-bucket:approved-regions",
        resource_id="demo-bucket",
        resource_type="s3-bucket",
        rule_id="approved-regions",
        status="not_supported",
        reason="rule 'approved-regions' is not on the remediation allowlist",
    )


def test_remediate_plan_prints_a_remediable_plan(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_remediate_report()))
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module, "build_remediation_plan", lambda *a, **k: _fake_remediable_plan()
    )

    result = runner.invoke(
        app,
        [
            "remediate-plan",
            str(report_path),
            "demo-bucket:required-tags",
            "--region",
            "us-east-1",
        ],
    )

    assert result.exit_code == 0
    assert "remediable" in result.stdout
    assert "put_bucket_tagging" in result.stdout


def test_remediate_plan_json_includes_the_action(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_remediate_report()))
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module, "build_remediation_plan", lambda *a, **k: _fake_remediable_plan()
    )

    result = runner.invoke(
        app,
        [
            "remediate-plan",
            str(report_path),
            "demo-bucket:required-tags",
            "--region",
            "us-east-1",
            "--json",
        ],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["plan"]["action"]["api_call"] == "put_bucket_tagging"


def test_remediate_plan_unknown_finding_id_fails(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_remediate_report()))
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())

    result = runner.invoke(
        app,
        [
            "remediate-plan",
            str(report_path),
            "no-such:required-tags",
            "--region",
            "us-east-1",
        ],
    )

    assert result.exit_code == 1
    assert "no finding" in result.output


def test_remediate_execute_without_approve_refuses_and_prints_the_plan_again(
    tmp_path, monkeypatch
):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_remediate_report()))
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module, "build_remediation_plan", lambda *a, **k: _fake_remediable_plan()
    )

    def _fake_execute(*args, **kwargs):
        assert kwargs.get("approve") is False
        raise RemediationNotApprovedError(_fake_remediable_plan())

    monkeypatch.setattr(cli_module, "execute_remediation_plan", _fake_execute)

    result = runner.invoke(
        app,
        [
            "remediate-execute",
            str(report_path),
            "demo-bucket:required-tags",
            "--region",
            "us-east-1",
        ],
    )

    assert result.exit_code == 1
    assert "requires --approve" in result.output
    assert (
        "put_bucket_tagging" in result.stdout
    )  # the plan is printed again, not hidden


def test_remediate_execute_with_approve_exits_0_and_prints_the_result(
    tmp_path, monkeypatch
):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_remediate_report()))
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module, "build_remediation_plan", lambda *a, **k: _fake_remediable_plan()
    )
    fake_result = RemediationResult(
        finding_id="demo-bucket:required-tags",
        resource_id="demo-bucket",
        rule_id="required-tags",
        status="executed",
        action="put_bucket_tagging",
        before_state={"tags": {}},
        after_state={"tags": {"team_owner": "platform-unassigned"}},
        verified=True,
        message="remediation applied",
        approved_by="test",
        timestamp="2026-08-05T00:00:00+00:00",
    )
    monkeypatch.setattr(
        cli_module, "execute_remediation_plan", lambda *a, **k: fake_result
    )

    result = runner.invoke(
        app,
        [
            "remediate-execute",
            str(report_path),
            "demo-bucket:required-tags",
            "--region",
            "us-east-1",
            "--approve",
        ],
    )

    assert result.exit_code == 0
    assert "executed" in result.stdout
    assert "verified: True" in result.stdout


def test_remediate_execute_not_supported_exits_1(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(_fake_remediate_report(rule_id="approved-regions"))
    )
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module, "build_remediation_plan", lambda *a, **k: _fake_not_supported_plan()
    )
    fake_result = RemediationResult(
        finding_id="demo-bucket:approved-regions",
        resource_id="demo-bucket",
        rule_id="approved-regions",
        status="not_supported",
        message="rule 'approved-regions' is not on the remediation allowlist",
    )
    monkeypatch.setattr(
        cli_module, "execute_remediation_plan", lambda *a, **k: fake_result
    )

    result = runner.invoke(
        app,
        [
            "remediate-execute",
            str(report_path),
            "demo-bucket:approved-regions",
            "--region",
            "us-east-1",
            "--approve",
        ],
    )

    assert result.exit_code == 1
    assert "not_supported" in result.stdout


def test_remediate_execute_batch_refuses_over_cap_and_mutates_nothing(
    tmp_path, monkeypatch
):
    report = _fake_remediate_report()
    report["findings"].append(
        {
            "resource_id": "demo-bucket-2",
            "resource_type": "s3-bucket",
            "rule_id": "required-tags",
            "severity": "high",
            "evidence": "missing tags: team_owner",
            "suppressed": False,
            "suppression_reason": None,
        }
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module, "build_remediation_plan", lambda *a, **k: _fake_remediable_plan()
    )

    def _fake_batch(*args, **kwargs):
        raise BatchTooLargeError(2, 1)

    monkeypatch.setattr(cli_module, "execute_remediation_batch", _fake_batch)

    result = runner.invoke(
        app,
        [
            "remediate-execute-batch",
            str(report_path),
            "demo-bucket:required-tags",
            "demo-bucket-2:required-tags",
            "--region",
            "us-east-1",
            "--approve",
            "--max-batch",
            "1",
        ],
    )

    assert result.exit_code == 1
    assert "exceeds the batch cap" in result.output


def test_remediate_execute_batch_with_approve_runs_and_exits_0(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_fake_remediate_report()))
    monkeypatch.setattr(cli_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module, "build_remediation_plan", lambda *a, **k: _fake_remediable_plan()
    )
    fake_result = RemediationResult(
        finding_id="demo-bucket:required-tags",
        resource_id="demo-bucket",
        rule_id="required-tags",
        status="executed",
        action="put_bucket_tagging",
    )
    monkeypatch.setattr(
        cli_module, "execute_remediation_batch", lambda *a, **k: [fake_result]
    )

    result = runner.invoke(
        app,
        [
            "remediate-execute-batch",
            str(report_path),
            "demo-bucket:required-tags",
            "--region",
            "us-east-1",
            "--approve",
        ],
    )

    assert result.exit_code == 0
    assert "executed" in result.stdout
