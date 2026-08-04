import json
import logging
import os
import sys

import pytest
import yaml

from platformops.config import (
    ConfigError,
    ConfigNotFoundError,
    ConfigParseError,
    ConfigPermissionError,
    apply_env_overrides,
    load_service_yaml,
    main,
)
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


def test_a_missing_file_raises_the_specific_confignotfounderror_subclass(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(ConfigNotFoundError):
        load_service_yaml(missing)


def test_invalid_yaml_raises_a_clean_configerror(tmp_path):
    broken = tmp_path / "broken.yaml"
    # Two keys at the same indentation claiming the same line -- not valid
    # YAML, the kind of typo a hand-edit produces.
    broken.write_text("name: checkout-api\n  bad indent: oops\n")

    with pytest.raises(ConfigError, match="invalid YAML"):
        load_service_yaml(broken)


def test_invalid_yaml_raises_the_specific_configparseerror_subclass(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: checkout-api\n  bad indent: oops\n")

    with pytest.raises(ConfigParseError):
        load_service_yaml(broken)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="permission bits do not block reads for root, or work the same way on Windows",
)
def test_a_file_this_process_cannot_read_raises_a_configpermissionerror(tmp_path):
    unreadable = tmp_path / "no-read.yaml"
    unreadable.write_text(GOOD_YAML_TEXT)
    unreadable.chmod(0o000)

    try:
        with pytest.raises(ConfigPermissionError, match="permission denied"):
            load_service_yaml(unreadable)
    finally:
        unreadable.chmod(0o644)  # restore, so tmp_path cleanup can remove it


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


def test_cli_without_verbose_hides_debug_and_info_log_lines(tmp_path, caplog):
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)

    with caplog.at_level(logging.DEBUG):
        main([str(service_file), "--check"])

    # basicConfig only runs once per process; what we can assert here without
    # depending on handler state is the level main() asked for.
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_cli_with_verbose_sets_debug_level(tmp_path):
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)

    main([str(service_file), "--check", "--verbose"])

    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_cli_debug_log_line_never_contains_a_field_value(tmp_path, capsys):
    # The log line should name the file being loaded, never the contents of
    # that file -- a service definition could hold something sensitive.
    # `logging.basicConfig` writes to stderr, so read the log text there
    # rather than through `caplog` -- `--verbose` reconfigures the root
    # logger's handler (see the `force=True` comment in config.py), which
    # would otherwise detach caplog's own capture handler.
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)

    main([str(service_file), "--check", "--verbose"])

    log_text = capsys.readouterr().err
    assert "checkout-api" not in log_text
    assert str(service_file) in log_text


def test_running_the_config_cli_twice_produces_the_same_resolved_file(tmp_path):
    service_file = tmp_path / "service.yaml"
    service_file.write_text(GOOD_YAML_TEXT)
    resolved_file = tmp_path / "service.resolved.yaml"

    main([str(service_file)])
    first_write = resolved_file.read_text()

    main([str(service_file)])
    second_write = resolved_file.read_text()

    assert first_write == second_write
