#!/usr/bin/env python3
"""Gather the first few minutes of incident context into one JSON report.

The first minutes of responding to an incident are usually spent answering the same three
questions by hand: did something just change in this repository, is the container runtime
even up, and is the service actually failing its health check. This script asks all three at
once, using functions this project already built and already tested -- `git_status()` and
`docker_info()` from Module 11, `check_health()` from Module 9 -- and combines their answers
into one report. It does not reimplement any of the three; it only imports, calls, and
combines them, the same thin-script discipline `service_readiness.py` established in
Module 19.

None of the three underlying functions raise for an expected failure (a dirty tree, an
unreachable Docker daemon, an unhealthy endpoint are all normal outcomes reported as data).
This script follows the same rule for its own report: a concrete, known problem (a dirty
tree, an unreachable Docker daemon, an unhealthy endpoint) is a "concern" this script flags.
A signal that could not be gathered at all (git_status()'s own error, or an unreachable
health-check URL) is reported honestly as UNKNOWN -- not guessed at, and not counted as a
concern on its own.

Exit codes:
  0 -- evidence gathered, nothing looks wrong
  1 -- evidence gathered, at least one signal looks wrong (dirty tree, Docker unreachable,
       or an unhealthy health check)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from platformops.httpclient import check_health
from platformops.local_ops import docker_info, git_status


def _gather_git(repo_path: str) -> dict[str, Any]:
    result = git_status(repo_path)
    if result.error:
        return {"status": "UNKNOWN", "changed_files": None, "error": result.error}
    return {
        "status": "CLEAN" if result.clean else "DIRTY",
        "changed_files": result.changed_files,
    }


def _gather_docker() -> dict[str, Any]:
    info = docker_info()
    if not info.get("available"):
        return {"status": "UNAVAILABLE", "error": info.get("error")}
    return {
        "status": "AVAILABLE",
        "containers_running": info.get("ContainersRunning"),
    }


def _gather_health(url: str | None) -> dict[str, Any]:
    if not url:
        return {
            "status": "SKIPPED",
            "url": None,
            "status_code": None,
            "latency_ms": None,
        }

    result = check_health(url)
    if result.error:
        return {
            "status": "UNKNOWN",
            "url": url,
            "status_code": None,
            "latency_ms": None,
            "error": result.error,
        }
    return {
        "status": "OK" if result.ok else "UNHEALTHY",
        "url": url,
        "status_code": result.status_code,
        "latency_ms": result.latency_ms,
    }


def gather_report(
    *, repo_path: str, health_url: str | None
) -> tuple[dict[str, Any], int]:
    """Gather git, Docker and health-check evidence into one report.

    Returns the JSON-ready report dict and the exit code that goes with it.
    """
    git_section = _gather_git(repo_path)
    docker_section = _gather_docker()
    health_section = _gather_health(health_url)

    has_concern = (
        git_section["status"] == "DIRTY"
        or docker_section["status"] == "UNAVAILABLE"
        or health_section["status"] == "UNHEALTHY"
    )

    report = {
        "repo_path": repo_path,
        "git": git_section,
        "docker": docker_section,
        "health": health_section,
        "has_concern": has_concern,
    }
    return report, (1 if has_concern else 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incident_context.py",
        description=(
            "Gather recent git status, container status and a health-check result "
            "into one first-response incident report."
        ),
    )
    parser.add_argument(
        "--repo-path", default=".", help="local path to check with `git status`"
    )
    parser.add_argument(
        "--health-url",
        default=None,
        help="URL to probe with a health check (omit to skip this section)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)

    report, exit_code = gather_report(
        repo_path=args.repo_path, health_url=args.health_url
    )
    print(json.dumps(report, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
