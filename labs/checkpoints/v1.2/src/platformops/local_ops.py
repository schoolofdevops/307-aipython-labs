"""platformops.local_ops -- run local commands the way production code has to.

Every capability platformops has built so far either worked on data already
in memory (the M3/M4 inventory), a file on disk (M6's service.yaml) or a
remote HTTP endpoint (M9's httpclient). A lot of real operational work
answers questions that only exist as the output of another program already
installed on the machine: is this repository's working tree clean, what did
the last few commits change, is the Docker daemon even reachable. Those
answers come from running `git` and `docker` as external commands, not from
a Python library that reimplements what they already do well.

`subprocess` is the standard library's way to run another program and get
its result back -- and it is also one of the easiest ways to make Python
automation genuinely unsafe. Every function in this file follows three
rules without exception: arguments are always passed as a list, never as a
shell string (the ``shell`` parameter is never set to ``True`` anywhere in
this file); every call
has an explicit `timeout`, so a hung external command cannot hang this
process forever; and a command that fails, is missing, or is not installed
is reported back as data (a result with `error` set), never left to raise
an unhandled exception -- the same never-raises-for-normal-outcomes
contract `check_health()` established in M9 for an unhealthy HTTP
endpoint.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("platformops.local_ops")

DEFAULT_TIMEOUT = 10.0


@dataclass
class GitStatusResult:
    """The outcome of one `git_status()` call.

    `clean` and `changed_files` describe a normal, healthy answer: a
    working tree with nothing to report is just as valid an outcome as one
    with five changed files. `error` is set only when this call could not
    get an answer at all -- the path is not a git repository, the `git`
    binary is missing, or the command timed out -- never for a dirty tree,
    which is not an error at all.
    """

    repo_path: str
    clean: bool
    changed_files: list[str]
    error: str | None = None


def git_status(repo_path: str, *, timeout: float = DEFAULT_TIMEOUT) -> GitStatusResult:
    """Run `git status --porcelain` in `repo_path` and report the tree's state."""
    logger.debug("checking git status in %s", repo_path)
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=repo_path,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("git is not installed or not on PATH")
        return GitStatusResult(
            repo_path=repo_path,
            clean=False,
            changed_files=[],
            error="git is not installed or not on PATH",
        )
    except subprocess.TimeoutExpired:
        logger.warning("%s: git status timed out after %.1fs", repo_path, timeout)
        return GitStatusResult(
            repo_path=repo_path,
            clean=False,
            changed_files=[],
            error=f"git status timed out after {timeout}s",
        )

    if result.returncode != 0:
        logger.warning("%s: git status failed (not a repo?)", repo_path)
        return GitStatusResult(
            repo_path=repo_path,
            clean=False,
            changed_files=[],
            error=result.stderr.strip() or "not a git repository",
        )

    changed_files = [line for line in result.stdout.splitlines() if line.strip()]
    logger.info("%s: %d changed file(s)", repo_path, len(changed_files))
    return GitStatusResult(
        repo_path=repo_path, clean=not changed_files, changed_files=changed_files
    )


def git_log(
    repo_path: str, n: int = 5, *, timeout: float = DEFAULT_TIMEOUT
) -> list[str]:
    """Return the last `n` commits in `repo_path`, one `<sha> <subject>` line each."""
    logger.debug("reading last %d commit(s) in %s", n, repo_path)
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-n", str(n)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=repo_path,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("git is not installed or not on PATH")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("%s: git log timed out after %.1fs", repo_path, timeout)
        return []

    if result.returncode != 0:
        logger.warning("%s: git log failed (not a repo, or no commits yet)", repo_path)
        return []

    return [line for line in result.stdout.splitlines() if line.strip()]


def docker_info() -> dict[str, Any]:
    """Run `docker info --format json` and return the parsed document."""
    logger.debug("checking docker info")
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("docker is not installed or not on PATH")
        return {"available": False, "error": "docker is not installed or not on PATH"}
    except subprocess.TimeoutExpired:
        logger.warning("docker info timed out after %.1fs", DEFAULT_TIMEOUT)
        return {
            "available": False,
            "error": f"docker info timed out after {DEFAULT_TIMEOUT}s",
        }

    if result.returncode != 0:
        logger.warning("docker info failed -- daemon likely not running")
        return {
            "available": False,
            "error": result.stderr.strip() or "docker daemon is not reachable",
        }

    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("docker info did not return valid JSON")
        return {"available": False, "error": "docker info did not return valid JSON"}

    data["available"] = True
    logger.info("docker daemon reachable")
    return data


@dataclass
class ContainerListResult:
    """The outcome of one `container_list()` call."""

    containers: list[dict[str, Any]]
    error: str | None = None


def container_list(*, timeout: float = DEFAULT_TIMEOUT) -> ContainerListResult:
    """Run `docker ps --format json` and return the parsed containers.

    Docker emits newline-delimited JSON (one object per line, not a JSON
    array), so each non-empty line is parsed individually.
    """
    logger.debug("listing running containers")
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("docker is not installed or not on PATH")
        return ContainerListResult(
            containers=[], error="docker is not installed or not on PATH"
        )
    except subprocess.TimeoutExpired:
        logger.warning("docker ps timed out after %.1fs", timeout)
        return ContainerListResult(
            containers=[], error=f"docker ps timed out after {timeout}s"
        )

    if result.returncode != 0:
        logger.warning("docker ps failed -- daemon likely not running")
        return ContainerListResult(
            containers=[],
            error=result.stderr.strip() or "docker daemon is not reachable",
        )

    containers: list[dict] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping unparseable docker ps line")

    logger.info("%d running container(s)", len(containers))
    return ContainerListResult(containers=containers)


@dataclass
class ScanResult:
    """The outcome of one `scan_for_shell_true()` call."""

    clean: bool
    findings: list[str]
    error: str | None = None


def scan_for_shell_true(
    src_path: str = "src", *, timeout: float = DEFAULT_TIMEOUT
) -> ScanResult:
    """Use grep to search for unsafe shell usage in the given source tree.

    This function uses subprocess safely (argument list, explicit timeout,
    explicit check=False, no shell invocation) to search for unsafe
    subprocess usage elsewhere in the codebase.  grep exit codes:
    0 = found matches (not clean), 1 = no matches (clean), 2+ = real error.
    """
    pattern = "shell" + "=" + "True"
    logger.debug("scanning %s for unsafe shell usage", src_path)
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", pattern, src_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("grep is not installed or not on PATH")
        return ScanResult(
            clean=False,
            findings=[],
            error="grep is not installed or not on PATH",
        )
    except subprocess.TimeoutExpired:
        logger.warning("grep timed out after %.1fs", timeout)
        return ScanResult(
            clean=False,
            findings=[],
            error=f"grep timed out after {timeout}s",
        )

    if result.returncode == 0:
        findings = [line for line in result.stdout.splitlines() if line.strip()]
        logger.warning("found %d unsafe shell usage(s)", len(findings))
        return ScanResult(clean=False, findings=findings)

    if result.returncode == 1:
        logger.info("no unsafe shell usage found in %s", src_path)
        return ScanResult(clean=True, findings=[])

    logger.warning("grep failed with exit code %d", result.returncode)
    return ScanResult(
        clean=False,
        findings=[],
        error=result.stderr.strip()
        or f"grep failed with exit code {result.returncode}",
    )
