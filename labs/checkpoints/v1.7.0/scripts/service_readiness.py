#!/usr/bin/env python3
"""Combine evidence from three tested adapters into one release-readiness report.

This script is a thin orchestrator, the same discipline `review-service.py`
established in Module 18: it never validates a service definition, never
parses `git status` output, and never talks to an HTTP API itself. It only
imports functions that already do those jobs, tested on their own in Module
5 (`servicedef`), Module 6 (`config`), Module 9 (`httpclient`) and Module 11
(`local_ops`), and combines their answers into one JSON report.

Two of the five sections in that report -- `cloud` and `kubernetes` -- are
not adapter calls at all. `platformops` has no AWS client and no Kubernetes
client yet (those arrive in Module 21+ and Module 28+), so this script does
not import anything, does not open a socket, and does not pretend to check
either one. It returns a fixed dict saying so. A report that guesses an
answer it does not have, or silently drops the section instead of naming the
gap, is worse than one that says "unknown" -- that is the point this module
exists to make, not a limitation to work around.

`overall_confidence` is computed from how many of the five sections came
back with a real, known answer versus how many are unknown -- see
`compute_confidence()`. With two sections always unknown today, the highest
confidence this script can ever report is "medium" -- see the Deep Dive for
the arithmetic.

Exit codes:
  0 -- evidence gathered, nothing failed (config passed, tree clean or its
       state unknown, CI passed or its state unknown)
  1 -- evidence gathered, but something failed (config invalid, tree dirty,
       or the latest CI run did not succeed)
  2 -- no evidence could be gathered at all (the service definition file
       itself could not be read)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from platformops.config import ConfigError, load_yaml_dict
from platformops.httpclient import (
    EndpointStatusError,
    EndpointUnreachableError,
    ResponseFormatError,
    list_workflow_runs,
)
from platformops.local_ops import git_status
from platformops.servicedef import validate_service

CLOUD_SECTION: dict[str, str] = {
    "status": "unknown",
    "reason": "no AWS adapter yet (Module 21+)",
}
KUBERNETES_SECTION: dict[str, str] = {
    "status": "unknown",
    "reason": "no Kubernetes adapter yet (Module 28+)",
}

# GitHub Actions run conclusions that mean the run did not succeed. `None`
# (the run is still in progress) is deliberately not in this set -- an
# in-progress run is UNKNOWN, not FAIL.
_FAILURE_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
}

# The confidence tiers below are read off a simple known/total ratio. With
# `cloud` and `kubernetes` always unknown, the best this script can ever see
# is 3 known sections out of 5 (ratio 0.6) -- below the 0.8 "high" bar. That
# is not a special case for those two sections; it falls out of the ratio.
_HIGH_CONFIDENCE_RATIO = 0.8
_MEDIUM_CONFIDENCE_RATIO = 0.4


def _gather_config(path: Path) -> tuple[dict[str, Any], str | None, bool]:
    """Load and validate one service definition file.

    Returns `(config_section, service_name, hard_failure)`. `hard_failure`
    is `True` only when the file itself could not be read -- the same
    distinction `review-service.py` draws between "the data is bad" (exit 1
    there) and "there is no data to read" (exit 2 there).
    """
    try:
        data = load_yaml_dict(path)
    except ConfigError as exc:
        return {"status": "FAIL", "problems": [str(exc)]}, None, True

    result = validate_service(data)
    if isinstance(result, list):
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in result
        ]
        raw_name = data.get("name")
        service_name = raw_name if isinstance(raw_name, str) else None
        return {"status": "FAIL", "problems": problems}, service_name, False

    return {"status": "PASS", "problems": []}, result.name, False


def _gather_source_control(repo_path: str) -> dict[str, Any]:
    """Check the local working tree with `git_status()`.

    `git_status()` never raises -- a missing repo, a missing `git` binary,
    or a timeout all come back as `GitStatusResult.error` set, which this
    function reports as UNKNOWN rather than guessing at a state it was
    never told. `git_status()` does not expose the current branch name (only
    clean/dirty state and the changed-file list), so `branch` is honestly
    `None` here rather than a value invented to fill the field.
    """
    result = git_status(repo_path)
    if result.error:
        return {"status": "UNKNOWN", "branch": None, "uncommitted_files": None}
    return {
        "status": "CLEAN" if result.clean else "DIRTY",
        "branch": None,
        "uncommitted_files": len(result.changed_files),
    }


def _gather_ci(repo: str | None) -> dict[str, Any]:
    """Check the latest GitHub Actions run for `owner/repo`, if one was given.

    No `--repo` at all, an unreachable API, or a repository with no runs
    yet are all reported as UNKNOWN -- the same "say what you don't know"
    discipline the cloud and kubernetes sections use, applied here to a real
    adapter instead of a fixed placeholder.
    """
    if not repo or "/" not in repo:
        return {"status": "UNKNOWN", "latest_run_conclusion": None}

    owner, _, name = repo.partition("/")
    if not owner or not name:
        return {"status": "UNKNOWN", "latest_run_conclusion": None}

    try:
        runs = list_workflow_runs(owner, name)
    except (EndpointStatusError, EndpointUnreachableError, ResponseFormatError):
        return {"status": "UNKNOWN", "latest_run_conclusion": None}

    if not runs:
        return {"status": "UNKNOWN", "latest_run_conclusion": None}

    conclusion = runs[0].get("conclusion")
    if conclusion == "success":
        status = "PASS"
    elif conclusion in _FAILURE_CONCLUSIONS:
        status = "FAIL"
    else:
        status = (
            "UNKNOWN"  # still running, or a conclusion this script does not recognize
        )
    return {"status": status, "latest_run_conclusion": conclusion}


def _is_known(status: str) -> bool:
    return status not in ("UNKNOWN", "unknown")


def compute_confidence(known_count: int, total_count: int, *, has_failure: bool) -> str:
    """Turn a known/total section count into "high" | "medium" | "low".

    `has_failure` overrides the ratio outright: a concrete FAIL or DIRTY is
    worth more than any amount of unknown evidence elsewhere, so it always
    caps confidence at "low" -- there is no ratio at which "some evidence
    failed" should still read as trustworthy.

    Otherwise this is a plain ratio against two fixed thresholds. Nothing
    here names `cloud` or `kubernetes` specially -- the reason confidence
    can never reach "high" today is that the ratio itself cannot: 3 known
    sections out of 5 is 0.6, below `_HIGH_CONFIDENCE_RATIO`.
    """
    if has_failure:
        return "low"
    ratio = known_count / total_count
    if ratio >= _HIGH_CONFIDENCE_RATIO:
        return "high"
    if ratio >= _MEDIUM_CONFIDENCE_RATIO:
        return "medium"
    return "low"


def build_recommendation(has_failure: bool, confidence: str) -> str:
    """One human-readable sentence, derived from the same two inputs as confidence."""
    if has_failure:
        return "not ready to ship -- fix required (see the failing section above)"
    if confidence == "low":
        return "not enough evidence to recommend shipping -- gather more evidence first"
    return (
        "ready to ship, with unknowns -- cloud and kubernetes evidence is not "
        "available yet (see the cloud and kubernetes sections)"
    )


def _build_report(
    *,
    service: str,
    config: dict[str, Any],
    source_control: dict[str, Any],
    ci: dict[str, Any],
) -> dict[str, Any]:
    known_flags = [
        _is_known(config["status"]),
        _is_known(source_control["status"]),
        _is_known(ci["status"]),
        _is_known(CLOUD_SECTION["status"]),
        _is_known(KUBERNETES_SECTION["status"]),
    ]
    known_count = sum(known_flags)
    total_count = len(known_flags)

    has_failure = (
        config["status"] == "FAIL"
        or source_control["status"] == "DIRTY"
        or ci["status"] == "FAIL"
    )

    confidence = compute_confidence(known_count, total_count, has_failure=has_failure)
    recommendation = build_recommendation(has_failure, confidence)

    return {
        "service": service,
        "config": config,
        "source_control": source_control,
        "ci": ci,
        "cloud": CLOUD_SECTION,
        "kubernetes": KUBERNETES_SECTION,
        "overall_confidence": confidence,
        "recommendation": recommendation,
    }


def gather_report(
    path: Path, *, repo: str | None, repo_path: str
) -> tuple[dict[str, Any], int]:
    """Gather all five sections of evidence for one service definition file.

    Returns the JSON-ready report dict and the exit code that goes with it.
    """
    config_section, service_name, hard_failure = _gather_config(Path(path))

    if hard_failure:
        report = _build_report(
            service=service_name or str(path),
            config=config_section,
            source_control={
                "status": "UNKNOWN",
                "branch": None,
                "uncommitted_files": None,
            },
            ci={"status": "UNKNOWN", "latest_run_conclusion": None},
        )
        return report, 2

    source_control = _gather_source_control(repo_path)
    ci = _gather_ci(repo)

    report = _build_report(
        service=service_name or "<unknown>",
        config=config_section,
        source_control=source_control,
        ci=ci,
    )

    has_failure = (
        config_section["status"] == "FAIL"
        or source_control["status"] == "DIRTY"
        or ci["status"] == "FAIL"
    )
    return report, (1 if has_failure else 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="service_readiness.py",
        description=(
            "Combine config, source-control and CI evidence for one service "
            "definition into a single release-readiness report."
        ),
    )
    parser.add_argument(
        "path", type=Path, help="path to a service definition YAML file"
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/name of the GitHub repository to check for CI evidence",
    )
    parser.add_argument(
        "--repo-path",
        default=".",
        help="local path to check with `git status` for source-control evidence",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)

    report, exit_code = gather_report(
        args.path, repo=args.repo, repo_path=args.repo_path
    )
    print(json.dumps(report, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
