"""platformops.releasecheck -- combine GitHub's own CI/CD state into one release-readiness verdict.

This module never re-implements what a CI system already decided. It reads
five independent pieces of evidence GitHub already computed -- pull request
merge/review state, the latest workflow run's conclusion, individual
check-run results, whether a build artifact exists, and the deployment
record's image metadata -- and combines them into a single verdict. It never
merges a pull request, never re-runs a check, never triggers a build and
never creates a deployment. Same discipline `cloudaudit.py` (Module 24)
applies to AWS resources, applied here to a repository's own CI/CD state:
this is an inspector, not a mechanic.

The fetch/aggregate split `cloudaudit.py` established carries over exactly.
Six `gather_*_evidence()` functions are the only functions in this module
that call into `platformops.httpclient` -- each one talks to exactly one
GitHub endpoint, catches every way that call can fail, and hands back the
raw facts it found (or an honest "could not fetch this" result). None of
them decides pass or fail; none of them contains the words "ready" or
"PASS" or "FAIL" anywhere in its body. `evaluate_release_readiness()` is the
other half: a pure function that takes six already-gathered evidence dicts
and a formal service/branch name, and returns a `ReleaseReadinessReport`. It
never imports `httpx`, never calls a `gather_*` function, and can be fully
unit-tested by handing it evidence built by hand -- no network, no mocking,
no fixtures required for that half of the test suite at all.

The honest-degradation rule this module exists to enforce: a source that
could not be reached is never treated as a pass, and never crashes the
whole report. `evaluate_release_readiness()` reports a fetch failure as
`UNKNOWN` for that one section, and folds it into `sources_failed` --
exactly the discipline `service_readiness.py` (Module 19) already applies
to a cloud or Kubernetes adapter that does not exist yet, applied here to a
GitHub call that failed for a different reason (a timeout, a 500, an
auth error) instead of never having existed at all. A report that quietly
turned an unreachable check-runs endpoint into a silent pass would be worse
than useless -- it would tell a release engineer "go ahead" on evidence
that was never actually collected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from platformops.httpclient import (
    EndpointStatusError,
    EndpointUnreachableError,
    HttpCheckError,
    ResponseFormatError,
    get_latest_release,
    get_pull_request,
    list_artifacts,
    list_check_runs,
    list_deployments,
    list_workflow_runs,
)

# The same failure-conclusion set `service_readiness.py` (Module 19) already
# uses for a workflow run's `conclusion` field. `None` (still running) is
# deliberately not in this set -- an in-progress run is UNKNOWN, not FAIL.
_FAILURE_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
}

# conclusion values a completed check run can carry without it counting as a
# failure -- "neutral" and "skipped" are real, valid outcomes GitHub reports
# for a check that chose not to block a merge.
_NON_FAILING_CHECK_CONCLUSIONS = {"success", "neutral", "skipped"}

_FETCHABLE_HTTP_ERRORS = (
    EndpointStatusError,
    EndpointUnreachableError,
    ResponseFormatError,
    HttpCheckError,
)


# ---------------------------------------------------------------------------
# Fetch -- one function per evidence source. These, and only these, call
# into httpclient.py. Every one of them catches its own network failures and
# hands back a plain dict of raw facts (or "fetched": False on failure) --
# never a verdict, never the word "ready", "PASS" or "FAIL".
# ---------------------------------------------------------------------------


def gather_pr_evidence(
    owner: str, repo: str, number: int, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch one pull request's raw merge/review state. Reports what GitHub said, decides nothing."""
    try:
        pr = get_pull_request(owner, repo, number, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    return {
        "fetched": True,
        "number": pr["number"],
        "state": pr["state"],
        "draft": pr["draft"],
        "merged": pr["merged"],
        "mergeable_state": pr["mergeable_state"],
    }


def gather_ci_evidence(
    owner: str, repo: str, branch: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch the most recent workflow run for `branch`. `conclusion` is None if none is found."""
    try:
        runs = list_workflow_runs(owner, repo, token=token, max_pages=1)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}

    for run in runs:
        if run.get("head_branch") == branch:
            return {
                "fetched": True,
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
            }
    return {"fetched": True, "status": None, "conclusion": None}


def gather_check_runs_evidence(
    owner: str, repo: str, ref: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch every check run for `ref`, reduced to pass/fail/pending names -- no verdict here."""
    try:
        runs = list_check_runs(owner, repo, ref, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}

    failed = [
        run["name"]
        for run in runs
        if run["status"] == "completed"
        and run["conclusion"] not in _NON_FAILING_CHECK_CONCLUSIONS
    ]
    pending = [run["name"] for run in runs if run["status"] != "completed"]
    passed = sum(
        1
        for run in runs
        if run["status"] == "completed"
        and run["conclusion"] in _NON_FAILING_CHECK_CONCLUSIONS
    )
    return {
        "fetched": True,
        "total": len(runs),
        "passed": passed,
        "failed": failed,
        "pending": pending,
    }


def gather_artifact_evidence(
    owner: str, repo: str, artifact_name: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch the repository's artifacts and report whether `artifact_name` is present and unexpired."""
    try:
        artifacts = list_artifacts(owner, repo, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}

    matching = [a for a in artifacts if a["name"] == artifact_name and not a["expired"]]
    return {
        "fetched": True,
        "present": bool(matching),
        "matched": matching[0] if matching else None,
    }


def gather_release_evidence(
    owner: str, repo: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch the latest published release. A 404 (no release yet) is a real, known answer, not a failure."""
    try:
        release = get_latest_release(owner, repo, token=token)
    except EndpointStatusError as exc:
        if exc.status_code == 404:
            return {"fetched": True, "found": False}
        return {"fetched": False, "error": str(exc)}
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}

    return {
        "fetched": True,
        "found": True,
        "tag_name": release["tag_name"],
        "published_at": release["published_at"],
    }


def gather_deployment_evidence(
    owner: str, repo: str, environment: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch the most recent deployment record for `environment` and its image metadata.

    This reads a deployment's `payload` for an `image`/`digest` reference --
    it never creates a deployment, and it never builds, pulls or inspects
    the image itself. Validating the image actually exists in a registry is
    a container-tooling concern, out of scope here (see Module 27).
    """
    try:
        deployments = list_deployments(owner, repo, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}

    matching = [d for d in deployments if d["environment"] == environment]
    if not matching:
        return {"fetched": True, "found": False, "image": None, "digest": None}

    latest = matching[0]
    image_payload = latest.get("payload") or {}
    return {
        "fetched": True,
        "found": True,
        "image": image_payload.get("image"),
        "digest": image_payload.get("digest"),
    }


# ---------------------------------------------------------------------------
# Aggregate -- pure functions. Nothing below this line imports httpx, calls
# a gather_* function, or makes a network call of any kind. Each _evaluate_*
# helper takes one already-gathered evidence dict and returns a
# {"status": ..., "detail": ...} section; evaluate_release_readiness()
# combines all six into one ReleaseReadinessReport.
# ---------------------------------------------------------------------------


@dataclass
class ReleaseReadinessReport:
    """One release's combined readiness, with every section's status and which sources actually answered.

    `verdict` is `"ready"` only when every gating section (`pr`, `ci`,
    `checks`, `artifacts`, `deployment`) came back `PASS` -- `release` is
    informational and never gates the verdict on its own, the same way
    `service_readiness.py`'s cloud/kubernetes sections inform a report
    without being required to reach `"high"` confidence. `sources_failed`
    lists which evidence sources this run could not even reach -- a report
    with a non-empty `sources_failed` never reports `"ready"`, no matter
    what the sources that did answer said.
    """

    service: str
    branch: str
    pr: dict[str, Any]
    ci: dict[str, Any]
    checks: dict[str, Any]
    artifacts: dict[str, Any]
    release: dict[str, Any]
    deployment: dict[str, Any]
    verdict: str
    sources_ok: list[str]
    sources_failed: list[str]


def _evaluate_pr(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "PR evidence unavailable"),
        }
    if evidence["merged"]:
        return {"status": "PASS", "detail": f"PR #{evidence['number']} merged"}
    if evidence["draft"]:
        return {
            "status": "FAIL",
            "detail": f"PR #{evidence['number']} is still a draft",
        }
    if evidence["mergeable_state"] == "clean":
        return {
            "status": "PASS",
            "detail": f"PR #{evidence['number']} open, approved and mergeable",
        }
    return {
        "status": "FAIL",
        "detail": (
            f"PR #{evidence['number']} not ready to merge "
            f"(mergeable_state={evidence['mergeable_state']})"
        ),
    }


def _evaluate_ci(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "CI evidence unavailable"),
        }
    conclusion = evidence.get("conclusion")
    if conclusion == "success":
        return {"status": "PASS", "detail": "latest workflow run succeeded"}
    if conclusion in _FAILURE_CONCLUSIONS:
        return {
            "status": "FAIL",
            "detail": f"latest workflow run concluded '{conclusion}'",
        }
    return {
        "status": "UNKNOWN",
        "detail": "no completed workflow run found for this branch",
    }


def _evaluate_checks(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "check-run evidence unavailable"),
        }
    if evidence["failed"]:
        return {
            "status": "FAIL",
            "detail": f"failing check(s): {', '.join(evidence['failed'])}",
        }
    if evidence["pending"]:
        return {
            "status": "UNKNOWN",
            "detail": f"pending check(s): {', '.join(evidence['pending'])}",
        }
    if evidence["total"] == 0:
        return {"status": "UNKNOWN", "detail": "no check runs reported for this commit"}
    return {
        "status": "PASS",
        "detail": f"all {evidence['total']} check run(s) passed",
    }


def _evaluate_artifact(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "artifact evidence unavailable"),
        }
    if evidence["present"]:
        size = evidence["matched"]["size_in_bytes"]
        return {"status": "PASS", "detail": f"build artifact present ({size} bytes)"}
    return {"status": "FAIL", "detail": "expected build artifact not found"}


def _evaluate_release(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "release evidence unavailable"),
        }
    if evidence["found"]:
        return {
            "status": "INFO",
            "detail": f"latest release {evidence['tag_name']} (published {evidence['published_at']})",
        }
    return {"status": "INFO", "detail": "no release published yet"}


def _evaluate_deployment(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "deployment evidence unavailable"),
        }
    if not evidence["found"]:
        return {
            "status": "FAIL",
            "detail": "no deployment record found for this environment",
        }
    if not evidence.get("image"):
        return {"status": "FAIL", "detail": "deployment record has no image reference"}
    return {
        "status": "PASS",
        "detail": f"deployment targets image {evidence['image']}",
    }


def evaluate_release_readiness(
    *,
    service: str,
    branch: str,
    pr_evidence: dict[str, Any],
    ci_evidence: dict[str, Any],
    checks_evidence: dict[str, Any],
    artifact_evidence: dict[str, Any],
    release_evidence: dict[str, Any],
    deployment_evidence: dict[str, Any],
) -> ReleaseReadinessReport:
    """Combine six already-gathered evidence dicts into one report. Pure -- no network call, ever.

    A source that failed to fetch (`"fetched": False`) never becomes a
    silent `PASS` here -- it becomes an `UNKNOWN` section and a name in
    `sources_failed`, and an `UNKNOWN` in any gating section keeps the
    overall verdict at `"not_ready"`, the same as an outright `FAIL`. This
    is the one rule this whole module exists to enforce: incomplete
    evidence is never treated as good evidence.
    """
    sources = {
        "pr": pr_evidence,
        "ci": ci_evidence,
        "checks": checks_evidence,
        "artifacts": artifact_evidence,
        "release": release_evidence,
        "deployment": deployment_evidence,
    }
    sources_ok = [name for name, ev in sources.items() if ev.get("fetched")]
    sources_failed = [name for name, ev in sources.items() if not ev.get("fetched")]

    pr_section = _evaluate_pr(pr_evidence)
    ci_section = _evaluate_ci(ci_evidence)
    checks_section = _evaluate_checks(checks_evidence)
    artifact_section = _evaluate_artifact(artifact_evidence)
    release_section = _evaluate_release(release_evidence)
    deployment_section = _evaluate_deployment(deployment_evidence)

    # release_section is informational only -- it never gates the verdict.
    gating_sections = [
        pr_section,
        ci_section,
        checks_section,
        artifact_section,
        deployment_section,
    ]
    has_fail = any(section["status"] == "FAIL" for section in gating_sections)
    has_unknown = any(section["status"] == "UNKNOWN" for section in gating_sections)
    verdict = "ready" if not has_fail and not has_unknown else "not_ready"

    return ReleaseReadinessReport(
        service=service,
        branch=branch,
        pr=pr_section,
        ci=ci_section,
        checks=checks_section,
        artifacts=artifact_section,
        release=release_section,
        deployment=deployment_section,
        verdict=verdict,
        sources_ok=sources_ok,
        sources_failed=sources_failed,
    )


def run_release_check(
    *,
    owner: str,
    repo: str,
    branch: str,
    pr: int,
    service: str,
    environment: str = "production",
    artifact_name: str | None = None,
    token: str | None = None,
) -> ReleaseReadinessReport:
    """Gather every evidence source and evaluate it -- the one call a CLI command or a script needs.

    This is the only function in this module that both fetches evidence and
    calls the aggregation function -- it is intentionally a thin
    orchestrator, not a place where fetching and judging blend together.
    """
    resolved_artifact_name = artifact_name or f"{service}-build"

    pr_evidence = gather_pr_evidence(owner, repo, pr, token=token)
    ci_evidence = gather_ci_evidence(owner, repo, branch, token=token)
    checks_evidence = gather_check_runs_evidence(owner, repo, branch, token=token)
    artifact_evidence = gather_artifact_evidence(
        owner, repo, resolved_artifact_name, token=token
    )
    release_evidence = gather_release_evidence(owner, repo, token=token)
    deployment_evidence = gather_deployment_evidence(
        owner, repo, environment, token=token
    )

    return evaluate_release_readiness(
        service=service,
        branch=branch,
        pr_evidence=pr_evidence,
        ci_evidence=ci_evidence,
        checks_evidence=checks_evidence,
        artifact_evidence=artifact_evidence,
        release_evidence=release_evidence,
        deployment_evidence=deployment_evidence,
    )
