import json

import pytest
from typer.testing import CliRunner

from platformops.cli import app

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
