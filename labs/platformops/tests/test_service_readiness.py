import importlib.util
import json
import sys
from pathlib import Path

import httpx
import respx

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "service_readiness.py"
)
_spec = importlib.util.spec_from_file_location("service_readiness", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
service_readiness = importlib.util.module_from_spec(_spec)
sys.modules["service_readiness"] = service_readiness
_spec.loader.exec_module(service_readiness)

from platformops.httpclient import EndpointUnreachableError  # noqa: E402
from platformops.local_ops import GitStatusResult  # noqa: E402

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

WORKFLOW_RUNS_URL = (
    "https://api.github.com/repos/example/checkout-api/actions/runs?per_page=30"
)

WORKFLOW_RUNS_SUCCESS = {
    "workflow_runs": [
        {
            "id": 2,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "html_url": "https://github.com/example/checkout-api/actions/runs/2",
        },
        {
            "id": 1,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "html_url": "https://github.com/example/checkout-api/actions/runs/1",
        },
    ]
}

WORKFLOW_RUNS_FAILURE = {
    "workflow_runs": [
        {
            "id": 3,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": "main",
            "html_url": "https://github.com/example/checkout-api/actions/runs/3",
        },
    ]
}

WORKFLOW_RUNS_EMPTY = {"workflow_runs": []}


def _fake_git_status(*, clean, changed_files=None, error=None):
    def fake(repo_path, *, timeout=10.0):
        return GitStatusResult(
            repo_path=repo_path,
            clean=clean,
            changed_files=changed_files or [],
            error=error,
        )

    return fake


# ---------------------------------------------------------------------------
# The happy path -- and the module's central honesty rule: confidence must
# never be "high" while cloud and kubernetes are always unknown.
# ---------------------------------------------------------------------------


@respx.mock
def test_clean_repo_passing_config_passing_ci_confidence_never_high(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(service_readiness, "git_status", _fake_git_status(clean=True))
    respx.get(WORKFLOW_RUNS_URL).mock(
        return_value=httpx.Response(200, json=WORKFLOW_RUNS_SUCCESS)
    )
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, exit_code = service_readiness.gather_report(
        path, repo="example/checkout-api", repo_path="."
    )

    assert exit_code == 0
    assert report["service"] == "checkout-api"
    assert report["config"] == {"status": "PASS", "problems": []}
    assert report["source_control"]["status"] == "CLEAN"
    assert report["source_control"]["uncommitted_files"] == 0
    assert report["ci"]["status"] == "PASS"
    assert report["ci"]["latest_run_conclusion"] == "success"
    assert report["cloud"] == {
        "status": "unknown",
        "reason": "no AWS adapter yet (Module 21+)",
    }
    assert report["kubernetes"] == {
        "status": "unknown",
        "reason": "no Kubernetes adapter yet (Module 28+)",
    }
    assert report["overall_confidence"] != "high"
    assert report["overall_confidence"] == "medium"
    assert "ready to ship" in report["recommendation"]


# ---------------------------------------------------------------------------
# A dirty tree is a real signal, not an error -- it degrades confidence and
# flips the exit code, but it is not the same failure mode as a missing file.
# ---------------------------------------------------------------------------


@respx.mock
def test_dirty_repo_marks_source_control_dirty_and_degrades_confidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        service_readiness,
        "git_status",
        _fake_git_status(clean=False, changed_files=[" M service.yaml"]),
    )
    respx.get(WORKFLOW_RUNS_URL).mock(
        return_value=httpx.Response(200, json=WORKFLOW_RUNS_SUCCESS)
    )
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, exit_code = service_readiness.gather_report(
        path, repo="example/checkout-api", repo_path="."
    )

    assert exit_code == 1
    assert report["source_control"]["status"] == "DIRTY"
    assert report["source_control"]["uncommitted_files"] == 1
    assert report["overall_confidence"] == "low"
    assert "not ready" in report["recommendation"]


# ---------------------------------------------------------------------------
# A failing CI run is the same class of signal as a dirty tree: it degrades
# confidence and flips the exit code.
# ---------------------------------------------------------------------------


@respx.mock
def test_failing_ci_marks_ci_fail_and_degrades_confidence(tmp_path, monkeypatch):
    monkeypatch.setattr(service_readiness, "git_status", _fake_git_status(clean=True))
    respx.get(WORKFLOW_RUNS_URL).mock(
        return_value=httpx.Response(200, json=WORKFLOW_RUNS_FAILURE)
    )
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, exit_code = service_readiness.gather_report(
        path, repo="example/checkout-api", repo_path="."
    )

    assert exit_code == 1
    assert report["ci"]["status"] == "FAIL"
    assert report["ci"]["latest_run_conclusion"] == "failure"
    assert report["overall_confidence"] == "low"


# ---------------------------------------------------------------------------
# Config validation failure -- the file exists and is readable, but the data
# in it is invalid. This is exit 1, not exit 2 (exit 2 is reserved for "no
# evidence could be gathered at all").
# ---------------------------------------------------------------------------


@respx.mock
def test_config_validation_failure_reports_problems_and_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(service_readiness, "git_status", _fake_git_status(clean=True))
    respx.get(WORKFLOW_RUNS_URL).mock(
        return_value=httpx.Response(200, json=WORKFLOW_RUNS_SUCCESS)
    )
    path = tmp_path / "service-bad.yaml"
    path.write_text(BAD_YAML_TEXT)

    report, exit_code = service_readiness.gather_report(
        path, repo="example/checkout-api", repo_path="."
    )

    assert exit_code == 1
    assert report["config"]["status"] == "FAIL"
    assert any("deployment_name" in problem for problem in report["config"]["problems"])
    assert report["overall_confidence"] == "low"


# ---------------------------------------------------------------------------
# A missing file: no evidence at all could be gathered. This is the one case
# with its own exit code (2), matching review-service.py's contract.
# ---------------------------------------------------------------------------


def test_missing_config_file_exits_2_with_minimal_report(tmp_path):
    path = tmp_path / "does-not-exist.yaml"

    report, exit_code = service_readiness.gather_report(
        path, repo="example/checkout-api", repo_path="."
    )

    assert exit_code == 2
    assert report["config"]["status"] == "FAIL"
    assert report["source_control"] == {
        "status": "UNKNOWN",
        "branch": None,
        "uncommitted_files": None,
    }
    assert report["ci"] == {"status": "UNKNOWN", "latest_run_conclusion": None}


# ---------------------------------------------------------------------------
# cloud/kubernetes: always literal-unknown, regardless of every other input.
# No import, no network -- fixed data.
# ---------------------------------------------------------------------------


@respx.mock
def test_cloud_and_kubernetes_are_always_literal_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service_readiness,
        "git_status",
        _fake_git_status(clean=False, changed_files=["M x"]),
    )
    respx.get(WORKFLOW_RUNS_URL).mock(
        return_value=httpx.Response(200, json=WORKFLOW_RUNS_FAILURE)
    )
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, _exit_code = service_readiness.gather_report(
        path, repo="example/checkout-api", repo_path="."
    )

    assert report["cloud"] == {
        "status": "unknown",
        "reason": "no AWS adapter yet (Module 21+)",
    }
    assert report["kubernetes"] == {
        "status": "unknown",
        "reason": "no Kubernetes adapter yet (Module 28+)",
    }


# ---------------------------------------------------------------------------
# CI evidence when no --repo is given, or when the API call itself fails --
# both are UNKNOWN, never a guess.
# ---------------------------------------------------------------------------


def test_ci_is_unknown_when_no_repo_given(tmp_path, monkeypatch):
    monkeypatch.setattr(service_readiness, "git_status", _fake_git_status(clean=True))
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, exit_code = service_readiness.gather_report(path, repo=None, repo_path=".")

    assert exit_code == 0
    assert report["ci"] == {"status": "UNKNOWN", "latest_run_conclusion": None}


def test_ci_is_unknown_when_the_github_call_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(service_readiness, "git_status", _fake_git_status(clean=True))

    def raise_unreachable(owner, repo, **kwargs):
        raise EndpointUnreachableError("simulated network failure")

    monkeypatch.setattr(service_readiness, "list_workflow_runs", raise_unreachable)
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, exit_code = service_readiness.gather_report(
        path, repo="example/checkout-api", repo_path="."
    )

    assert exit_code == 0
    assert report["ci"] == {"status": "UNKNOWN", "latest_run_conclusion": None}


def test_source_control_is_unknown_when_git_status_reports_an_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        service_readiness,
        "git_status",
        _fake_git_status(clean=False, error="not a git repository"),
    )
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    report, exit_code = service_readiness.gather_report(
        path, repo=None, repo_path="/tmp/not-a-repo"
    )

    assert exit_code == 0
    assert report["source_control"] == {
        "status": "UNKNOWN",
        "branch": None,
        "uncommitted_files": None,
    }


# ---------------------------------------------------------------------------
# The confidence function itself -- pure, inspectable, and the reason
# "high" is mathematically unreachable while 2 of 5 sections are always
# unknown (max known ratio today is 3/5 = 0.6, below the 0.8 "high" bar).
# ---------------------------------------------------------------------------


def test_compute_confidence_is_low_on_any_failure_regardless_of_known_count():
    assert service_readiness.compute_confidence(5, 5, has_failure=True) == "low"


def test_compute_confidence_caps_at_medium_with_two_of_five_always_unknown():
    # best case today: config + source_control + ci all known (3/5), no failure
    assert service_readiness.compute_confidence(3, 5, has_failure=False) == "medium"


def test_compute_confidence_is_low_when_most_sections_are_unknown():
    assert service_readiness.compute_confidence(1, 5, has_failure=False) == "low"


def test_compute_confidence_reaches_high_only_above_the_eighty_percent_bar():
    assert service_readiness.compute_confidence(4, 5, has_failure=False) == "high"


# ---------------------------------------------------------------------------
# main() -- stdout is valid JSON, and the exit code matches gather_report().
# ---------------------------------------------------------------------------


@respx.mock
def test_main_prints_valid_json_to_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service_readiness, "git_status", _fake_git_status(clean=True))
    respx.get(WORKFLOW_RUNS_URL).mock(
        return_value=httpx.Response(200, json=WORKFLOW_RUNS_SUCCESS)
    )
    path = tmp_path / "service.yaml"
    path.write_text(GOOD_YAML_TEXT)

    exit_code = service_readiness.main(
        [str(path), "--repo", "example/checkout-api", "--repo-path", "."]
    )

    assert exit_code == 0
    printed = capsys.readouterr().out
    parsed = json.loads(printed)
    assert parsed["overall_confidence"] == "medium"
