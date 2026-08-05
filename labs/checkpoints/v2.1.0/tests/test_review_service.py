import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "review-service.py"
_spec = importlib.util.spec_from_file_location("review_service", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
review_service = importlib.util.module_from_spec(_spec)
sys.modules["review_service"] = review_service
_spec.loader.exec_module(review_service)

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

BAD_YAML_TEXT = (
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

EMPTY_OBSERVABILITY_YAML_TEXT = (
    "name: checkout-api\n"
    "repository: github.com/example/checkout-api\n"
    "environment: prod\n"
    "team_owner: payments-team\n"
    "kubernetes_namespace: checkout\n"
    "deployment_name: checkout-api\n"
    'aws_account: "111122223333"\n'
    "region: us-east-1\n"
    "observability:\n"
    '  dashboard_url: ""\n'
    '  alert_channel: ""\n'
)


def test_a_good_service_definition_passes_with_exit_code_0(tmp_path):
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, exit_code = review_service.review(path)

    assert exit_code == 0
    assert report["validation"] == "PASS"
    assert report["problems"] == []
    assert report["observability"] == {
        "dashboard_url_set": True,
        "alert_channel_set": True,
    }
    assert report["namespace"] == "checkout"
    assert report["recommendation"] == "ready to ship"


def test_a_schema_invalid_service_definition_fails_with_exit_code_1(tmp_path):
    path = tmp_path / "service-bad.yaml"
    path.write_text(BAD_YAML_TEXT)

    report, exit_code = review_service.review(path)

    assert exit_code == 1
    assert report["validation"] == "FAIL"
    assert any("deployment_name" in problem for problem in report["problems"])
    assert report["recommendation"] == "fix required before shipping"


def test_a_schema_failure_still_reports_the_real_observability_values(tmp_path):
    path = tmp_path / "service-bad.yaml"
    path.write_text(BAD_YAML_TEXT)

    report, exit_code = review_service.review(path)

    assert exit_code == 1
    assert report["observability"] == {
        "dashboard_url_set": True,
        "alert_channel_set": True,
    }


def test_a_missing_file_fails_with_exit_code_2(tmp_path):
    path = tmp_path / "does-not-exist.yaml"

    report, exit_code = review_service.review(path)

    assert exit_code == 2
    assert report["validation"] == "FAIL"
    assert report["namespace"] is None


def test_empty_observability_fields_fail_with_exit_code_1_even_though_schema_passes(
    tmp_path,
):
    path = tmp_path / "service-empty-obs.yaml"
    path.write_text(EMPTY_OBSERVABILITY_YAML_TEXT)

    report, exit_code = review_service.review(path)

    assert exit_code == 1
    assert report["validation"] == "FAIL"
    assert report["observability"] == {
        "dashboard_url_set": False,
        "alert_channel_set": False,
    }
    assert report["namespace"] == "checkout"


def test_the_report_is_valid_json_when_printed(tmp_path, capsys):
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    exit_code = review_service.main([str(path)])

    assert exit_code == 0
    printed = capsys.readouterr().out
    parsed = json.loads(printed)
    assert parsed["validation"] == "PASS"
