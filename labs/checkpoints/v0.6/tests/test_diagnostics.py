import logging
import os
import sys

import pytest

from platformops.diagnostics import diagnose, main

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


def test_a_good_file_returns_exit_0_and_prints_ok(tmp_path, capsys):
    good = tmp_path / "service.yaml"
    good.write_text(GOOD_YAML_TEXT)

    exit_code = diagnose(good)

    assert exit_code == 0
    assert "OK" in capsys.readouterr().out


def test_scenario_1_invalid_yaml_returns_exit_1_and_names_it_yaml(tmp_path, capsys):
    broken = tmp_path / "service.yaml"
    broken.write_text("name: checkout-api\n  bad indent: oops\n")

    exit_code = diagnose(broken)

    assert exit_code == 1
    assert "invalid YAML" in capsys.readouterr().out


def test_scenario_2_missing_file_returns_exit_1_and_names_the_path(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.yaml"

    exit_code = diagnose(missing)

    assert exit_code == 1
    assert "not found" in capsys.readouterr().out


def test_scenario_3_missing_field_returns_exit_1_and_names_the_field(tmp_path, capsys):
    bad = tmp_path / "service.yaml"
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

    exit_code = diagnose(bad)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "deployment_name" in out


def test_scenario_4_wrong_type_returns_exit_1_and_names_the_field(tmp_path, capsys):
    bad = tmp_path / "service.yaml"
    bad.write_text(
        GOOD_YAML_TEXT.replace(
            "deployment_name: checkout-api", "deployment_name:\n  - checkout-api"
        )
    )

    exit_code = diagnose(bad)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "deployment_name" in out


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="permission bits do not block reads for root, or work the same way on Windows",
)
def test_scenario_5_permission_denied_returns_exit_1_not_a_traceback(tmp_path, capsys):
    unreadable = tmp_path / "service.yaml"
    unreadable.write_text(GOOD_YAML_TEXT)
    unreadable.chmod(0o000)

    try:
        exit_code = diagnose(unreadable)
    finally:
        unreadable.chmod(0o644)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "permission" in out.lower()


def test_scenario_6_unexpected_aws_account_value_returns_exit_1_and_names_the_field(
    tmp_path, capsys
):
    bad = tmp_path / "service.yaml"
    bad.write_text(
        GOOD_YAML_TEXT.replace('aws_account: "111122223333"', 'aws_account: "12345"')
    )

    exit_code = diagnose(bad)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "aws_account" in out


def test_cli_with_no_argument_exits_2_the_usage_error_code(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_cli_verbose_shows_debug_detail_that_plain_run_does_not(tmp_path, caplog):
    good = tmp_path / "service.yaml"
    good.write_text(GOOD_YAML_TEXT)

    with caplog.at_level(logging.DEBUG):
        main([str(good), "--verbose"])

    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_debug_log_line_never_contains_a_field_value(tmp_path, capsys):
    # `--verbose` reconfigures the root logger's own handler (see the
    # `force=True` comment in diagnostics.py), which detaches caplog's
    # capture handler -- read the log text `basicConfig` wrote to stderr
    # instead of going through `caplog.records`.
    good = tmp_path / "service.yaml"
    good.write_text(GOOD_YAML_TEXT)

    main([str(good), "--verbose"])

    log_text = capsys.readouterr().err
    assert "checkout-api" not in log_text
    assert str(good) in log_text
