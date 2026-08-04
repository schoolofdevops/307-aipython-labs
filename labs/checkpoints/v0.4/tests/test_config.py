import json

import pytest
import yaml

from platformops.config import ConfigError, apply_env_overrides, load_service_yaml, main
from platformops.servicedef import ServiceDefinition

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


def test_a_good_yaml_file_loads_into_a_validated_model(tmp_path):
    good = tmp_path / "service.yaml"
    good.write_text(
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

    result = load_service_yaml(good)

    assert isinstance(result, ServiceDefinition)
    assert result.name == "checkout-api"


def test_a_missing_file_raises_a_clean_configerror_not_a_traceback(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(ConfigError, match="not found"):
        load_service_yaml(missing)


def test_invalid_yaml_raises_a_clean_configerror(tmp_path):
    broken = tmp_path / "broken.yaml"
    # Two keys at the same indentation claiming the same line -- not valid
    # YAML, the kind of typo a hand-edit produces.
    broken.write_text("name: checkout-api\n  bad indent: oops\n")

    with pytest.raises(ConfigError, match="invalid YAML"):
        load_service_yaml(broken)


def test_a_file_that_fails_model_validation_surfaces_the_missing_field(tmp_path):
    bad = tmp_path / "service-bad.yaml"
    bad.write_text(
        "name: checkout-api\n"
        "repository: github.com/example/checkout-api\n"
        "environment: prod\n"
        "team_owner: payments-team\n"
        "kubernetes_namespace: checkout\n"
        'aws_account: "111122223333"\n'
        "region: us-east-1\n"
        "observability:\n"
        "  dashboard_url: https://grafana.example.com/d/checkout-api\n"
        '  alert_channel: "#checkout-alerts"\n'
    )

    errors = load_service_yaml(bad)

    assert isinstance(errors, list)
    assert any(
        error["loc"] == ("deployment_name",) and error["type"] == "missing"
        for error in errors
    )


def test_an_env_var_override_beats_the_file_value():
    file_data = {"environment": "prod", "region": "us-east-1"}

    resolved = apply_env_overrides(
        file_data, environ={"PLATFORMOPS_ENVIRONMENT": "staging"}
    )

    assert resolved["environment"] == "staging"
    assert resolved["region"] == "us-east-1"  # untouched -- no matching env var


def test_a_field_with_no_matching_env_var_keeps_the_file_value():
    file_data = {"team_owner": "payments-team"}

    resolved = apply_env_overrides(file_data, environ={})

    assert resolved["team_owner"] == "payments-team"


def test_cli_check_reports_ok_and_writes_nothing(tmp_path, capsys):
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)
    resolved_file = tmp_path / "service.resolved.yaml"

    exit_code = main([str(service_file), "--check"])

    assert exit_code == 0
    assert not resolved_file.exists()
    assert "dry run" in capsys.readouterr().out


def test_cli_without_check_writes_the_resolved_config(tmp_path):
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)
    resolved_file = tmp_path / "service.resolved.yaml"

    exit_code = main([str(service_file)])

    assert exit_code == 0
    assert resolved_file.exists()
    written = yaml.safe_load(resolved_file.read_text())
    assert written["name"] == "checkout-api"


def test_cli_env_override_flows_into_the_written_resolved_config(tmp_path, monkeypatch):
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)
    resolved_file = tmp_path / "service.resolved.yaml"
    monkeypatch.setenv("PLATFORMOPS_ENVIRONMENT", "staging")

    exit_code = main([str(service_file)])

    assert exit_code == 0
    written = yaml.safe_load(resolved_file.read_text())
    assert written["environment"] == "staging"


def test_cli_json_flag_prints_parseable_json(tmp_path, capsys):
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)

    exit_code = main([str(service_file), "--check", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["service"]["name"] == "checkout-api"
