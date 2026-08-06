import json
from pathlib import Path

import httpx
import respx

from platformops.releasecheck import (
    ReleaseReadinessReport,
    evaluate_release_readiness,
    gather_artifact_evidence,
    gather_check_runs_evidence,
    gather_ci_evidence,
    gather_deployment_evidence,
    gather_pr_evidence,
    gather_release_evidence,
    run_release_check,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


OWNER = "acme-platform"
REPO = "payments"

# ---------------------------------------------------------------------------
# Fetch layer -- each gather_*_evidence() function against its fixture(s).
# respx intercepts the exact httpx call the underlying httpclient function
# makes; every one of these runs fully offline, no real GitHub call, no
# token, no rate limit.
# ---------------------------------------------------------------------------


@respx.mock
def test_gather_pr_evidence_reports_a_merged_pr():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/482").mock(
        return_value=httpx.Response(200, json=_fixture("pr_approved.json"))
    )

    evidence = gather_pr_evidence(OWNER, REPO, 482)

    assert evidence["fetched"] is True
    assert evidence["merged"] is True
    assert evidence["number"] == 482


@respx.mock
def test_gather_pr_evidence_reports_a_blocked_pr():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/491").mock(
        return_value=httpx.Response(200, json=_fixture("pr_pending.json"))
    )

    evidence = gather_pr_evidence(OWNER, REPO, 491)

    assert evidence["fetched"] is True
    assert evidence["merged"] is False
    assert evidence["mergeable_state"] == "blocked"


@respx.mock
def test_gather_pr_evidence_degrades_honestly_on_a_404(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/9999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    evidence = gather_pr_evidence(OWNER, REPO, 9999)

    assert evidence == {"fetched": False, "error": evidence["error"]}
    assert "404" in evidence["error"]


@respx.mock
def test_gather_ci_evidence_matches_the_run_for_the_requested_branch():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/actions/runs").mock(
        return_value=httpx.Response(200, json=_fixture("workflow_success.json"))
    )

    evidence = gather_ci_evidence(OWNER, REPO, "main")

    assert evidence == {"fetched": True, "status": "completed", "conclusion": "success"}


@respx.mock
def test_gather_ci_evidence_reports_no_run_found_for_an_unmatched_branch():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/actions/runs").mock(
        return_value=httpx.Response(200, json=_fixture("workflow_success.json"))
    )

    evidence = gather_ci_evidence(OWNER, REPO, "some-other-branch")

    assert evidence == {"fetched": True, "status": None, "conclusion": None}


@respx.mock
def test_gather_ci_evidence_degrades_honestly_when_unreachable(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/actions/runs").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    evidence = gather_ci_evidence(OWNER, REPO, "main")

    assert evidence["fetched"] is False
    assert "error" in evidence


@respx.mock
def test_gather_check_runs_evidence_reports_failed_and_pending_names():
    respx.get(
        f"https://api.github.com/repos/{OWNER}/{REPO}/commits/main/check-runs"
    ).mock(return_value=httpx.Response(200, json=_fixture("check_runs_failed.json")))

    evidence = gather_check_runs_evidence(OWNER, REPO, "main")

    assert evidence["fetched"] is True
    assert evidence["total"] == 3
    assert evidence["failed"] == ["unit-tests"]
    assert evidence["pending"] == []


@respx.mock
def test_gather_artifact_evidence_reports_presence():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/actions/artifacts").mock(
        return_value=httpx.Response(200, json=_fixture("artifacts_present.json"))
    )

    evidence = gather_artifact_evidence(OWNER, REPO, "payments-build")

    assert evidence["fetched"] is True
    assert evidence["present"] is True
    assert evidence["matched"]["size_in_bytes"] == 15423104


@respx.mock
def test_gather_artifact_evidence_reports_absence_without_raising():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/actions/artifacts").mock(
        return_value=httpx.Response(200, json=_fixture("artifacts_missing.json"))
    )

    evidence = gather_artifact_evidence(OWNER, REPO, "payments-build")

    assert evidence == {"fetched": True, "present": False, "matched": None}


@respx.mock
def test_gather_release_evidence_reports_the_latest_tag():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest").mock(
        return_value=httpx.Response(200, json=_fixture("release_latest.json"))
    )

    evidence = gather_release_evidence(OWNER, REPO)

    assert evidence["found"] is True
    assert evidence["tag_name"] == "v1.4.2"


@respx.mock
def test_gather_release_evidence_treats_a_404_as_no_release_not_a_failure(
    monkeypatch,
):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get(
        f"https://api.github.com/repos/{OWNER}/brand-new-service/releases/latest"
    ).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

    evidence = gather_release_evidence(OWNER, "brand-new-service")

    assert evidence == {"fetched": True, "found": False}


@respx.mock
def test_gather_deployment_evidence_extracts_image_and_digest():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/deployments").mock(
        return_value=httpx.Response(200, json=_fixture("deployment.json"))
    )

    evidence = gather_deployment_evidence(OWNER, REPO, "production")

    assert evidence["found"] is True
    assert evidence["image"] == "ghcr.io/acme-platform/payments:1.4.2"
    assert evidence["digest"].startswith("sha256:")


@respx.mock
def test_gather_deployment_evidence_reports_a_missing_image_reference():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/deployments").mock(
        return_value=httpx.Response(200, json=_fixture("deployment_no_image.json"))
    )

    evidence = gather_deployment_evidence(OWNER, REPO, "production")

    assert evidence["found"] is True
    assert evidence["image"] is None


# ---------------------------------------------------------------------------
# Aggregation -- evaluate_release_readiness() is pure. Every test below
# constructs evidence dicts directly, exactly as a fetch function would have
# returned them, with no respx and no network mocking of any kind involved.
# ---------------------------------------------------------------------------

_PASS_PR = {
    "fetched": True,
    "number": 482,
    "state": "closed",
    "draft": False,
    "merged": True,
    "mergeable_state": "unknown",
}
_PASS_CI = {"fetched": True, "status": "completed", "conclusion": "success"}
_PASS_CHECKS = {"fetched": True, "total": 3, "passed": 3, "failed": [], "pending": []}
_PASS_ARTIFACT = {
    "fetched": True,
    "present": True,
    "matched": {"name": "payments-build", "size_in_bytes": 15423104, "expired": False},
}
_PASS_RELEASE = {
    "fetched": True,
    "found": True,
    "tag_name": "v1.4.2",
    "published_at": "2026-07-30T10:15:00Z",
}
_PASS_DEPLOYMENT = {
    "fetched": True,
    "found": True,
    "image": "ghcr.io/acme-platform/payments:1.4.2",
    "digest": "sha256:9f2a5c3d1e0b7a6c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c",
}


def _full_pass_report() -> ReleaseReadinessReport:
    return evaluate_release_readiness(
        service="payments",
        branch="main",
        pr_evidence=_PASS_PR,
        ci_evidence=_PASS_CI,
        checks_evidence=_PASS_CHECKS,
        artifact_evidence=_PASS_ARTIFACT,
        release_evidence=_PASS_RELEASE,
        deployment_evidence=_PASS_DEPLOYMENT,
    )


def test_evaluate_release_readiness_is_ready_when_every_gating_section_passes():
    report = _full_pass_report()

    assert report.verdict == "ready"
    assert report.pr["status"] == "PASS"
    assert report.ci["status"] == "PASS"
    assert report.checks["status"] == "PASS"
    assert report.artifacts["status"] == "PASS"
    assert report.deployment["status"] == "PASS"
    assert report.sources_failed == []


def test_evaluate_release_readiness_is_not_ready_when_ci_failed():
    report = evaluate_release_readiness(
        service="payments",
        branch="bump-processor-client",
        pr_evidence=_PASS_PR,
        ci_evidence={"fetched": True, "status": "completed", "conclusion": "failure"},
        checks_evidence=_PASS_CHECKS,
        artifact_evidence=_PASS_ARTIFACT,
        release_evidence=_PASS_RELEASE,
        deployment_evidence=_PASS_DEPLOYMENT,
    )

    assert report.verdict == "not_ready"
    assert report.ci["status"] == "FAIL"


def test_evaluate_release_readiness_is_not_ready_when_pr_is_blocked():
    report = evaluate_release_readiness(
        service="payments",
        branch="bump-processor-client",
        pr_evidence={
            "fetched": True,
            "number": 491,
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable_state": "blocked",
        },
        ci_evidence=_PASS_CI,
        checks_evidence=_PASS_CHECKS,
        artifact_evidence=_PASS_ARTIFACT,
        release_evidence=_PASS_RELEASE,
        deployment_evidence=_PASS_DEPLOYMENT,
    )

    assert report.verdict == "not_ready"
    assert report.pr["status"] == "FAIL"
    assert "blocked" in report.pr["detail"]


def test_evaluate_release_readiness_is_not_ready_when_an_artifact_is_missing():
    report = evaluate_release_readiness(
        service="payments",
        branch="main",
        pr_evidence=_PASS_PR,
        ci_evidence=_PASS_CI,
        checks_evidence=_PASS_CHECKS,
        artifact_evidence={"fetched": True, "present": False, "matched": None},
        release_evidence=_PASS_RELEASE,
        deployment_evidence=_PASS_DEPLOYMENT,
    )

    assert report.verdict == "not_ready"
    assert report.artifacts["status"] == "FAIL"


def test_evaluate_release_readiness_release_section_never_gates_the_verdict():
    """No release published yet is informational -- it must not block a ready verdict."""
    report = evaluate_release_readiness(
        service="brand-new-service",
        branch="main",
        pr_evidence=_PASS_PR,
        ci_evidence=_PASS_CI,
        checks_evidence=_PASS_CHECKS,
        artifact_evidence=_PASS_ARTIFACT,
        release_evidence={"fetched": True, "found": False},
        deployment_evidence=_PASS_DEPLOYMENT,
    )

    assert report.verdict == "ready"
    assert report.release["status"] == "INFO"


# ---------------------------------------------------------------------------
# Honest degradation -- a source that could not be fetched must never turn
# into a silent pass, and must never crash the whole report.
# ---------------------------------------------------------------------------


def test_evaluate_release_readiness_degrades_honestly_when_checks_are_unreachable():
    report = evaluate_release_readiness(
        service="payments",
        branch="main",
        pr_evidence=_PASS_PR,
        ci_evidence=_PASS_CI,
        checks_evidence={
            "fetched": False,
            "error": "payments unreachable after 5 attempt(s): connection refused",
        },
        artifact_evidence=_PASS_ARTIFACT,
        release_evidence=_PASS_RELEASE,
        deployment_evidence=_PASS_DEPLOYMENT,
    )

    assert report.verdict == "not_ready"
    assert report.checks["status"] == "UNKNOWN"
    assert "connection refused" in report.checks["detail"]
    assert report.sources_failed == ["checks"]
    assert "checks" not in report.sources_ok


def test_evaluate_release_readiness_reports_every_failed_source_not_just_one():
    report = evaluate_release_readiness(
        service="payments",
        branch="main",
        pr_evidence={"fetched": False, "error": "timed out"},
        ci_evidence=_PASS_CI,
        checks_evidence=_PASS_CHECKS,
        artifact_evidence={"fetched": False, "error": "500 server error"},
        release_evidence=_PASS_RELEASE,
        deployment_evidence=_PASS_DEPLOYMENT,
    )

    assert report.verdict == "not_ready"
    assert set(report.sources_failed) == {"pr", "artifacts"}
    assert set(report.sources_ok) == {"ci", "checks", "release", "deployment"}


# ---------------------------------------------------------------------------
# run_release_check -- the thin orchestrator. One respx-mocked pass through
# all six endpoints proves gather + evaluate wire together end to end.
# ---------------------------------------------------------------------------


@respx.mock
def test_run_release_check_returns_a_ready_report_for_the_full_pass_fixture_set():
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/482").mock(
        return_value=httpx.Response(200, json=_fixture("pr_approved.json"))
    )
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/actions/runs").mock(
        return_value=httpx.Response(200, json=_fixture("workflow_success.json"))
    )
    respx.get(
        f"https://api.github.com/repos/{OWNER}/{REPO}/commits/main/check-runs"
    ).mock(return_value=httpx.Response(200, json=_fixture("check_runs_pass.json")))
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/actions/artifacts").mock(
        return_value=httpx.Response(200, json=_fixture("artifacts_present.json"))
    )
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest").mock(
        return_value=httpx.Response(200, json=_fixture("release_latest.json"))
    )
    respx.get(f"https://api.github.com/repos/{OWNER}/{REPO}/deployments").mock(
        return_value=httpx.Response(200, json=_fixture("deployment.json"))
    )

    report = run_release_check(
        owner=OWNER, repo=REPO, branch="main", pr=482, service="payments"
    )

    assert report.verdict == "ready"
    assert report.sources_failed == []
