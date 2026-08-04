#!/usr/bin/env python3
"""Release Ladder — serve.py

Shows the learner where they really are on the PlatformOps release ladder
(v0.0 -> v3.0, 37 tagged releases across 10 parts of the course) plus five
lenses: a Project Foundation panel (M2) with the real state of the
learner's own `~/platformops` project, an Inventory Reporter panel (M3)
that actually runs the learner's own `src/platformops/inventory.py` and
shows its real output, a Module Map panel (M4) that shows the real
package structure of `src/platformops/inventory/` once the M4 refactor
splits the single file into a package, a Service Definitions panel
(M5) that reads the real shape of `src/platformops/servicedef.py` (its
`ServiceDefinition` field count and whether constraint markers like
`Literal[` and `pattern=` are present) and runs the learner's own
`python -m platformops.servicedef` demo, a Configuration panel (M6)
that reads whether `src/platformops/config.py`, `service.yaml` and
`service-bad.yaml` exist, runs the learner's own
`python -m platformops.config service.yaml` demo, and reports (by name
only, never by value) whether any of the `PLATFORMOPS_*` env-override
variables Module 6 teaches are set in this server's own environment, and a
Diagnostics panel (M7) that reads whether `src/platformops/diagnostics.py`
exists, its tests, and which `ConfigError` subclasses it declares, then
runs the learner's own six-scenario
`python -m platformops.diagnostics service.yaml` check and shows its real
output plus a parsed exit-code chip.

Zero third-party dependencies. Python 3 standard library only. Mostly
read-only: it runs `git tag` / `git status` / `git rev-parse` (never a
write command) and reads files on disk. The exceptions are the M3, M5, M6
and M7 lenses, which run the learner's own `python -m platformops.inventory`,
`python -m platformops.servicedef`, `python -m platformops.config` and
`python -m platformops.diagnostics` (their own modules) so they can show
the real output — they never run ruff, pytest, or anything that installs
to their project (the M6 and M7 modules can write a small
`service.resolved.yaml`, the same file the learner's own command would
write; this tool does nothing beyond running it). The M4 lens only reads
file text (line counts, `def ` counts) — it never imports or executes the
learner's package.

Usage:
    python3 serve.py                        # uses ~/platformops (or $PLATFORMOPS_DIR)
    python3 serve.py /path/to/platformops    # explicit path (first CLI arg wins)
    PLATFORMOPS_DIR=/path/to/platformops python3 serve.py
    PORT=9000 python3 serve.py               # change the port (default 8307)

Then open http://127.0.0.1:8307/
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")

DEFAULT_PORT = 8307
DEFAULT_HOST = "127.0.0.1"

# ---------------------------------------------------------------------------
# The release ladder: 10 parts of the course, 37 tagged releases, v0.0 -> v3.0.
# Source data: labs/m1/architecture-map.md (the 4-phase ladder table) and
# course.config.json (per-module lab_intent, which names each release and
# title). The 10-part grouping below is this tool's own display grouping —
# it slices the same 37 releases into ten readable sections; it does not
# introduce or rename any release.
# ---------------------------------------------------------------------------
LADDER = [
    {"part": 1, "title": "Orientation", "modules": "M1", "releases": []},
    {
        "part": 2,
        "title": "Python Foundations",
        "modules": "M2–M7",
        "releases": [
            ("v0.0", "M2", "Project Foundation"),
            ("v0.1", "M3", "Infrastructure Inventory Reporter"),
            ("v0.2", "M4", "Modular Inventory Engine"),
            ("v0.3", "M5", "Service Definition Model"),
            ("v0.4", "M6", "Service Configuration Validator"),
            ("v0.5", "M7", "Reliable Validation and Diagnostics"),
        ],
    },
    {
        "part": 3,
        "title": "Building Real Tools",
        "modules": "M8–M12",
        "releases": [
            ("v0.6", "M8", "PlatformOps CLI"),
            ("v0.7", "M9", "Repository and API Inspector"),
            ("v0.8", "M10", "Concurrent Health Checker"),
            ("v0.9", "M11", "Local Operations Adapter"),
            ("v0.10", "M12", "Tested Automation Core"),
        ],
    },
    {
        "part": 4,
        "title": "Professional-Grade Python",
        "modules": "M13",
        "releases": [
            ("v1.0", "M13", "Maintainable PlatformOps Foundation"),
        ],
    },
    {
        "part": 5,
        "title": "Working With Coding Agents",
        "modules": "M14–M15",
        "releases": [
            ("v1.1", "M15", "AI-Assisted Engineering Harness"),
        ],
    },
    {
        "part": 6,
        "title": "Agent Skills",
        "modules": "M16–M20",
        "releases": [
            ("v1.2", "M16", "Agent Skills Architecture"),
            ("v1.2.1", "M17", "Service Review Skill"),
            ("v1.3", "M18", "Script-Backed Agent Skill Foundation"),
            ("v1.4", "M19", "Portable Service Readiness Skill"),
            ("v1.5", "M20", "Governed Agent Skill Library"),
        ],
    },
    {
        "part": 7,
        "title": "Cloud Automation With AWS",
        "modules": "M21–M25",
        "releases": [
            ("v1.6", "M21", "AWS Resource Inventory"),
            ("v1.7", "M22", "Local AWS Automation Environment"),
            ("v1.8", "M23", "Multi-Region Cloud Inventory"),
            ("v1.9", "M24", "Cloud Hygiene Auditor"),
            ("v2.0", "M25", "Governed Cloud Remediation"),
        ],
    },
    {
        "part": 8,
        "title": "CI/CD and Kubernetes",
        "modules": "M26–M29",
        "releases": [
            ("v2.1", "M26", "Release Readiness Checker"),
            ("v2.2", "M27", "Containerized PlatformOps"),
            ("v2.3", "M28", "Kubernetes Health Inspector"),
            ("v2.4", "M29", "Governed Kubernetes Operations"),
        ],
    },
    {
        "part": 9,
        "title": "Observability and Platform APIs",
        "modules": "M30–M34",
        "releases": [
            ("v2.5", "M30", "Observable Automation"),
            ("v2.6", "M31", "Incident Context Collector"),
            ("v2.7", "M32", "PlatformOps Internal API"),
            ("v2.8", "M33", "AI Workload Inspector"),
            ("v2.9", "M34", "AI Release Readiness"),
        ],
    },
    {
        "part": 10,
        "title": "Agentic Operations and Capstone",
        "modules": "M35–M39",
        "releases": [
            ("v2.10", "M35", "Agent Tool Library"),
            ("v2.11", "M36", "PlatformOps MCP Server"),
            ("v2.12", "M37", "Governed Agentic Operations"),
            ("v2.13", "M38", "Production Release Candidate"),
            ("v3.0", "M39", "Production PlatformOps Toolkit"),
        ],
    },
]


def flat_releases():
    """Ordered (version, module, title) tuples across the whole ladder."""
    out = []
    for part in LADDER:
        out.extend(part["releases"])
    return out


TOTAL_RELEASES = len(flat_releases())  # expected 37


def get_platformops_dir() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return os.path.expanduser(sys.argv[1].strip())
    env = os.environ.get("PLATFORMOPS_DIR", "").strip()
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/platformops")


def run_git(directory: str, args: list[str]) -> tuple[bool, str]:
    """Run a read-only git command. Returns (ok, stdout-stripped)."""
    try:
        proc = subprocess.run(
            ["git", "-C", directory, *args],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return False, ""
        return True, proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def read_git_state(directory: str) -> dict:
    is_repo, _ = run_git(directory, ["rev-parse", "--is-inside-work-tree"])
    if not is_repo:
        return {"is_repo": False, "tags": [], "head_short": None, "dirty": None}

    ok, tags_out = run_git(directory, ["tag"])
    tags = [t for t in tags_out.splitlines() if t.strip()] if ok else []

    ok_head, head_short = run_git(directory, ["rev-parse", "--short", "HEAD"])
    head_short = head_short if ok_head and head_short else None

    ok_status, status_out = run_git(directory, ["status", "--porcelain"])
    dirty = bool(status_out.strip()) if ok_status else None

    return {"is_repo": True, "tags": tags, "head_short": head_short, "dirty": dirty}


def read_pyproject(directory: str) -> dict:
    path = os.path.join(directory, "pyproject.toml")
    if not os.path.isfile(path):
        return {"found": False, "name": None, "version": None, "ruff_configured": False}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {"found": False, "name": None, "version": None, "ruff_configured": False}

    name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
    version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return {
        "found": True,
        "name": name_match.group(1) if name_match else None,
        "version": version_match.group(1) if version_match else None,
        "ruff_configured": "[tool.ruff]" in text,
    }


def read_foundation(directory: str) -> dict:
    """The M2 lens: real, cheap-to-derive facts about ~/platformops.
    Never runs ruff/pytest/uv — only reads files, tags and status.
    """
    exists = os.path.isdir(directory)
    git_state = read_git_state(directory) if exists else {
        "is_repo": False, "tags": [], "head_short": None, "dirty": None,
    }
    pyproject = read_pyproject(directory) if exists else {
        "found": False, "name": None, "version": None, "ruff_configured": False,
    }
    venv_exists = os.path.isdir(os.path.join(directory, ".venv")) if exists else False

    key_files = {}
    for rel in (
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "README.md",
        ".gitignore",
        "tests",
        "src/platformops/__init__.py",
    ):
        key_files[rel] = os.path.exists(os.path.join(directory, rel)) if exists else False

    test_file_count = 0
    if exists:
        test_file_count = len(glob.glob(os.path.join(directory, "tests", "test_*.py")))

    latest_tag = git_state["tags"][-1] if git_state["tags"] else None

    return {
        "path": directory,
        "exists": exists,
        "git": git_state,
        "latest_tag": latest_tag,
        "pyproject": pyproject,
        "venv_exists": venv_exists,
        "key_files": key_files,
        "test_file_count": test_file_count,
    }


INVENTORY_TIMEOUT_SECS = 15
STDERR_SNIPPET_MAX_CHARS = 400


def find_inventory_tests(directory: str) -> int:
    """Count test files for the inventory module (e.g. tests/test_inventory.py).

    Matches `tests/test_inventory*.py` so small naming variants still count.
    """
    if not os.path.isdir(directory):
        return 0
    return len(glob.glob(os.path.join(directory, "tests", "test_inventory*.py")))


def run_inventory_report(directory: str) -> dict:
    """Run `python -m platformops.inventory` inside `directory` and capture
    its output. Read-only from this tool's point of view: it runs the
    learner's own module, never edits anything.

    Tries `uv run python -m platformops.inventory` first (the normal way a
    learner runs their project). If `uv` is not on PATH, or the project has
    no `.venv` yet, it falls back to plain `python3 -m platformops.inventory`
    with `PYTHONPATH` pointed at the project's `src/` folder — this lets the
    lens work even before the learner has run `uv sync`, and it needs no
    network access.
    """
    uv_path = shutil.which("uv")
    venv_exists = os.path.isdir(os.path.join(directory, ".venv"))

    env = None
    if uv_path and venv_exists:
        cmd = [uv_path, "run", "python", "-m", "platformops.inventory"]
        command_label = "uv run python -m platformops.inventory"
    else:
        cmd = [sys.executable, "-m", "platformops.inventory"]
        command_label = (
            "python3 -m platformops.inventory  (fallback: uv or .venv not found, "
            "using PYTHONPATH=src)"
        )
        env = dict(os.environ)
        src_dir = os.path.join(directory, "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")

    result = {
        "attempted": True,
        "command": command_label,
        "ok": False,
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr_snippet": None,
        "error": None,
    }

    try:
        proc = subprocess.run(
            cmd,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=INVENTORY_TIMEOUT_SECS,
            env=env,
        )
        result["exit_code"] = proc.returncode
        result["ok"] = proc.returncode == 0
        result["stdout"] = proc.stdout or ""
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            result["stderr_snippet"] = stderr[:STDERR_SNIPPET_MAX_CHARS]
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["error"] = (
            f"The command took longer than {INVENTORY_TIMEOUT_SECS}s and was stopped."
        )
    except OSError as exc:
        result["error"] = f"Could not run the command ({exc})."

    return result


def parse_inventory_quick_facts(stdout: str) -> dict | None:
    """Best-effort, defensive parse of a few headline numbers out of the
    report text. Returns None if nothing recognizable was found. This never
    raises: if the report format changes, the raw text is still shown as-is
    by the caller regardless of what this function returns.
    """
    if not stdout or not stdout.strip():
        return None

    facts: dict = {}
    try:
        m = re.search(r"(?m)^Total servers:\s*(\d+)", stdout)
        if m:
            facts["total_servers"] = int(m.group(1))

        m = re.search(r"(?m)^\s*Total CPU \(cores\):\s*(\d+)", stdout)
        if m:
            facts["total_cpu"] = int(m.group(1))

        m = re.search(r"(?m)^\s*Total memory \(GB\):\s*(\d+)", stdout)
        if m:
            facts["total_memory_gb"] = int(m.group(1))

        sec = re.search(r"Missing owner tag:\s*\n((?:.*\n)*?)\n", stdout)
        if sec:
            lines = [ln for ln in sec.group(1).splitlines() if ln.strip().startswith("-")]
            facts["missing_owner_count"] = len(lines)

        sec = re.search(r"low memory[^\n]*:\s*\n((?:.*\n)*?)\n", stdout)
        if sec:
            lines = [ln for ln in sec.group(1).splitlines() if ln.strip().startswith("-")]
            facts["low_memory_prod_count"] = len(lines)
    except Exception:  # noqa: BLE001 - parsing is a bonus, never fatal
        pass

    return facts or None


def read_inventory(directory: str) -> dict:
    """The M3 lens: real facts about the learner's `src/platformops/inventory.py`.

    If the file does not exist yet, nothing is run — the lens just reports
    that and waits. If it exists, this actually runs the learner's own
    report (see `run_inventory_report`) and shows it verbatim, plus a few
    quick facts parsed out of it when that is cheap and safe to do.
    """
    module_path = os.path.join(directory, "src", "platformops", "inventory.py")
    module_exists = os.path.isfile(module_path)
    test_file_count = find_inventory_tests(directory)

    if not module_exists:
        return {
            "module_exists": False,
            "test_file_count": test_file_count,
            "report": None,
            "quick_facts": None,
        }

    report = run_inventory_report(directory)
    quick_facts = parse_inventory_quick_facts(report["stdout"]) if report["ok"] else None

    return {
        "module_exists": True,
        "test_file_count": test_file_count,
        "report": report,
        "quick_facts": quick_facts,
    }


MODULE_MAP_EXPECTED_FILES = [
    "__init__.py",
    "__main__.py",
    "data.py",
    "rules.py",
    "summary.py",
    "report.py",
]

DEF_LINE_RE = re.compile(r"^def\s+\w+\s*\(")


def count_lines_and_defs(path: str) -> tuple[int, int] | None:
    """Return (line_count, top_level_def_count) for a file, or None if it
    cannot be read. Top-level defs are found with a simple regex match on
    lines starting with `def ` (no indentation) — this never imports or
    executes the learner's code, it only reads text.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    line_count = len(lines)
    def_count = sum(1 for ln in lines if DEF_LINE_RE.match(ln))
    return line_count, def_count


def read_module_map(directory: str) -> dict:
    """The M4 lens: the real package structure of `src/platformops/inventory/`
    (the v0.2 refactor that splits the old single-file `inventory.py` into a
    package). Read-only: only reads file text and directory listings, never
    imports or executes learner code.
    """
    package_dir = os.path.join(directory, "src", "platformops", "inventory")
    old_single_file = os.path.join(directory, "src", "platformops", "inventory.py")

    package_exists = os.path.isdir(package_dir)
    old_single_file_exists = os.path.isfile(old_single_file)

    files_out = []
    for name in MODULE_MAP_EXPECTED_FILES:
        path = os.path.join(package_dir, name)
        exists = package_exists and os.path.isfile(path)
        line_count = None
        def_count = None
        if exists:
            counts = count_lines_and_defs(path)
            if counts is not None:
                line_count, def_count = counts
        files_out.append({
            "name": name,
            "exists": exists,
            "line_count": line_count,
            "def_count": def_count,
        })

    return {
        "package_exists": package_exists,
        "old_single_file_exists": old_single_file_exists,
        "files": files_out,
    }


def find_servicedef_tests(directory: str) -> int:
    """Count test files for the service definition module (e.g.
    tests/test_servicedef.py). Matches `tests/test_servicedef*.py` so small
    naming variants still count.
    """
    if not os.path.isdir(directory):
        return 0
    return len(glob.glob(os.path.join(directory, "tests", "test_servicedef*.py")))


SERVICEDEF_FIELD_LINE_RE = re.compile(r"^(\w+)\s*:\s*.+$")
SERVICEDEF_CLASS_RE = re.compile(
    r"class\s+ServiceDefinition\b[^\n:]*:\n(.*?)(?=\n\S|\Z)", re.S
)


def count_servicedef_fields(text: str) -> int | None:
    """Defensive, regex-only count of `ServiceDefinition`'s annotated class
    fields (e.g. `name: str`). Reads the class body as plain text up to the
    first method (`def `) or decorator and counts lines at the class's own
    indent level that look like `<word>: <something>`. Never imports or
    executes the learner's code. Returns None if the class cannot be found.
    """
    try:
        m = SERVICEDEF_CLASS_RE.search(text)
        if not m:
            return None
        base_indent = None
        count = 0
        for ln in m.group(1).splitlines():
            if not ln.strip():
                continue
            indent = len(ln) - len(ln.lstrip())
            if base_indent is None:
                base_indent = indent
            if indent != base_indent:
                continue  # a wrapped/nested line, not a top-level field
            stripped = ln.strip()
            if stripped.startswith("def ") or stripped.startswith("@"):
                break  # fields section ended, methods start here
            if SERVICEDEF_FIELD_LINE_RE.match(stripped):
                count += 1
        return count
    except Exception:  # noqa: BLE001 - parsing is a bonus, never fatal
        return None


def read_servicedef_source(directory: str) -> dict:
    """Real, cheap-to-derive facts about the learner's
    `src/platformops/servicedef.py`: does it exist, how many test files does
    it have, how many fields does `ServiceDefinition` declare, and does the
    file contain the constraint markers (`Literal[`, `pattern=`) that Phase B
    of Module 5 adds. Only reads file text — never imports or executes it.
    """
    module_path = os.path.join(directory, "src", "platformops", "servicedef.py")
    module_exists = os.path.isfile(module_path)
    test_file_count = find_servicedef_tests(directory)

    field_count = None
    has_literal_constraint = False
    has_pattern_constraint = False

    if module_exists:
        try:
            with open(module_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        field_count = count_servicedef_fields(text)
        has_literal_constraint = "Literal[" in text
        has_pattern_constraint = "pattern=" in text

    return {
        "module_exists": module_exists,
        "test_file_count": test_file_count,
        "field_count": field_count,
        "has_literal_constraint": has_literal_constraint,
        "has_pattern_constraint": has_pattern_constraint,
    }


def run_servicedef_demo(directory: str) -> dict:
    """Run `python -m platformops.servicedef` inside `directory` and capture
    its output. Read-only from this tool's point of view: it runs the
    learner's own module, never edits anything.

    Uses the same uv-then-python3-fallback pattern as `run_inventory_report`:
    tries `uv run python -m platformops.servicedef` first, and falls back to
    plain `python3 -m platformops.servicedef` with `PYTHONPATH` pointed at
    the project's `src/` folder when `uv` is not on PATH or there is no
    `.venv` yet.
    """
    uv_path = shutil.which("uv")
    venv_exists = os.path.isdir(os.path.join(directory, ".venv"))

    env = None
    if uv_path and venv_exists:
        cmd = [uv_path, "run", "python", "-m", "platformops.servicedef"]
        command_label = "uv run python -m platformops.servicedef"
    else:
        cmd = [sys.executable, "-m", "platformops.servicedef"]
        command_label = (
            "python3 -m platformops.servicedef  (fallback: uv or .venv not found, "
            "using PYTHONPATH=src)"
        )
        env = dict(os.environ)
        src_dir = os.path.join(directory, "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")

    result = {
        "attempted": True,
        "command": command_label,
        "ok": False,
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr_snippet": None,
        "error": None,
    }

    try:
        proc = subprocess.run(
            cmd,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=INVENTORY_TIMEOUT_SECS,
            env=env,
        )
        result["exit_code"] = proc.returncode
        result["ok"] = proc.returncode == 0
        result["stdout"] = proc.stdout or ""
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            result["stderr_snippet"] = stderr[:STDERR_SNIPPET_MAX_CHARS]
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["error"] = (
            f"The command took longer than {INVENTORY_TIMEOUT_SECS}s and was stopped."
        )
    except OSError as exc:
        result["error"] = f"Could not run the command ({exc})."

    return result


def parse_servicedef_result_lines(stdout: str) -> dict | None:
    """Best-effort, defensive parse of the demo's `OK -- ...` / `FAIL -- ...`
    lines into two lists. Returns None if nothing recognizable was found.
    This never raises: if the output format changes, the raw text is still
    shown as-is by the caller regardless of what this function returns.
    """
    if not stdout or not stdout.strip():
        return None
    try:
        ok_lines = re.findall(r"(?m)^\s*OK -- (.+)$", stdout)
        fail_lines = re.findall(r"(?m)^\s*FAIL -- (.+)$", stdout)
    except Exception:  # noqa: BLE001 - parsing is a bonus, never fatal
        return None
    if not ok_lines and not fail_lines:
        return None
    return {"ok_lines": ok_lines, "fail_lines": fail_lines}


def read_servicedef(directory: str) -> dict:
    """The M5 lens: real facts about the learner's
    `src/platformops/servicedef.py` (source facts, always) plus a live run
    of its demo (`python -m platformops.servicedef`) once the file exists.
    """
    source = read_servicedef_source(directory)
    if not source["module_exists"]:
        return {**source, "demo": None, "result_lines": None}

    demo = run_servicedef_demo(directory)
    result_lines = parse_servicedef_result_lines(demo["stdout"]) if demo["ok"] else None
    return {**source, "demo": demo, "result_lines": result_lines}


CONFIG_ENV_PREFIX_RE = re.compile(r'(?m)^ENV_PREFIX\s*=\s*"([^"]*)"')
CONFIG_ENV_FIELDS_RE = re.compile(r'(?ms)^ENV_OVERRIDE_FIELDS\s*=\s*\(([^)]*)\)')
DEFAULT_ENV_PREFIX = "PLATFORMOPS_"
DEFAULT_ENV_OVERRIDE_FIELDS = ("environment", "region", "team_owner", "kubernetes_namespace")


def find_config_tests(directory: str) -> int:
    """Count test files for the config module (e.g. tests/test_config.py).

    Matches `tests/test_config*.py` so small naming variants still count.
    """
    if not os.path.isdir(directory):
        return 0
    return len(glob.glob(os.path.join(directory, "tests", "test_config*.py")))


def parse_env_override_names(text: str | None) -> list[str]:
    """Which PLATFORMOPS_<FIELD> env var names Module 6's override list
    names. A defensive, regex-only parse of `config.py`'s own `ENV_PREFIX`
    and `ENV_OVERRIDE_FIELDS` constants when the file exists and declares
    them; falls back to the module's documented default list otherwise, so
    this still shows something useful before the learner has customized (or
    even written) the file. Never imports the learner's code.
    """
    prefix = DEFAULT_ENV_PREFIX
    fields = list(DEFAULT_ENV_OVERRIDE_FIELDS)
    if text:
        try:
            m = CONFIG_ENV_PREFIX_RE.search(text)
            if m and m.group(1):
                prefix = m.group(1)
            m = CONFIG_ENV_FIELDS_RE.search(text)
            if m:
                found = re.findall(r'"([^"]+)"', m.group(1))
                if found:
                    fields = found
        except Exception:  # noqa: BLE001 - parsing is a bonus, never fatal
            pass
    return [f"{prefix}{field.upper()}" for field in fields]


def read_env_overrides(text: str | None) -> list[dict]:
    """The env-override facts for the M6 lens: for each PLATFORMOPS_<FIELD>
    env var Module 6's override list names, whether it is currently set in
    *this server process's own environment*. Names only, never values — the
    tool never prints what an env var is set to, only whether it is set, so
    this tab stays safe to leave open even if a learner's shell exports
    something sensitive under a similar-looking name.
    """
    names = parse_env_override_names(text)
    return [{"name": name, "set": name in os.environ} for name in names]


def read_config_source(directory: str) -> dict:
    """Real, cheap-to-derive facts about the learner's
    `src/platformops/config.py`, plus the two fixture YAML files Module 6's
    lab has the learner create (`service.yaml`, `service-bad.yaml`). Only
    reads file text and checks existence — never imports or executes
    anything. Returns the raw source text too (private, stripped before the
    JSON response) so the caller can derive the env-override field names
    from it without a second file read.
    """
    module_path = os.path.join(directory, "src", "platformops", "config.py")
    module_exists = os.path.isfile(module_path)
    test_file_count = find_config_tests(directory)
    service_yaml_exists = os.path.isfile(os.path.join(directory, "service.yaml"))
    service_bad_yaml_exists = os.path.isfile(os.path.join(directory, "service-bad.yaml"))

    text = None
    if module_exists:
        try:
            with open(module_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = None

    return {
        "module_exists": module_exists,
        "test_file_count": test_file_count,
        "service_yaml_exists": service_yaml_exists,
        "service_bad_yaml_exists": service_bad_yaml_exists,
        "_text": text,
    }


def run_config_demo(directory: str) -> dict:
    """Run `python -m platformops.config service.yaml` inside `directory`
    and capture its output — the M6 lens. Uses the same uv-then-python3
    fallback pattern as the Inventory Reporter and Service Definitions
    lenses.

    Unlike those two, `platformops.config` depends on `pyyaml`, a
    third-party package — not something the plain-`python3` fallback path
    has, on purpose: that path only points `PYTHONPATH` at the learner's
    `src/`, nothing installed. To make that "no dependencies" promise real
    rather than accidental (it should not quietly succeed just because
    pyyaml happens to already be installed somewhere else on the machine
    running this tool), the fallback runs with Python's `-S` flag, which
    skips site-packages entirely. That way a learner who has not yet run
    `uv add pyyaml` / `uv sync` sees the same `ModuleNotFoundError` this
    lens is built to detect and explain gracefully — everywhere, not just
    on machines that happen to lack a global pyyaml install.
    """
    uv_path = shutil.which("uv")
    venv_exists = os.path.isdir(os.path.join(directory, ".venv"))

    env = None
    if uv_path and venv_exists:
        cmd = [uv_path, "run", "python", "-m", "platformops.config", "service.yaml"]
        command_label = "uv run python -m platformops.config service.yaml"
    else:
        cmd = [sys.executable, "-S", "-m", "platformops.config", "service.yaml"]
        command_label = (
            "python3 -m platformops.config service.yaml  (fallback: uv or .venv not "
            "found, using PYTHONPATH=src)"
        )
        env = dict(os.environ)
        src_dir = os.path.join(directory, "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")

    result = {
        "attempted": True,
        "command": command_label,
        "ok": False,
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr_snippet": None,
        "error": None,
        "missing_dependency_hint": None,
    }

    try:
        proc = subprocess.run(
            cmd,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=INVENTORY_TIMEOUT_SECS,
            env=env,
        )
        result["exit_code"] = proc.returncode
        result["ok"] = proc.returncode == 0
        result["stdout"] = proc.stdout or ""
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            result["stderr_snippet"] = stderr[:STDERR_SNIPPET_MAX_CHARS]
            if "ModuleNotFoundError" in stderr:
                result["missing_dependency_hint"] = (
                    "This fallback path deliberately has no third-party packages "
                    "installed yet (no pyyaml). Run it yourself: "
                    "uv run python -m platformops.config service.yaml"
                )
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["error"] = (
            f"The command took longer than {INVENTORY_TIMEOUT_SECS}s and was stopped."
        )
    except OSError as exc:
        result["error"] = f"Could not run the command ({exc})."

    return result


def read_config(directory: str) -> dict:
    """The M6 lens: real facts about the learner's
    `src/platformops/config.py` and the `service.yaml` / `service-bad.yaml`
    fixtures Module 6 has them create, the env-override facts (see
    `read_env_overrides`), and — once the file exists — a live run of
    `python -m platformops.config service.yaml` (see `run_config_demo`).
    """
    source = read_config_source(directory)
    text = source.pop("_text")
    env_overrides = read_env_overrides(text)

    if not source["module_exists"]:
        return {**source, "env_overrides": env_overrides, "demo": None}

    demo = run_config_demo(directory)
    return {**source, "env_overrides": env_overrides, "demo": demo}


CONFIG_ERROR_SUBCLASS_RE = re.compile(r'class\s+(\w+)\(ConfigError\)')


def find_diagnostics_tests(directory: str) -> int:
    """Count test files for the diagnostics module (e.g. tests/test_diagnostics.py).

    Matches `tests/test_diagnostics*.py` so small naming variants still count.
    """
    if not os.path.isdir(directory):
        return 0
    return len(glob.glob(os.path.join(directory, "tests", "test_diagnostics*.py")))


def parse_config_error_subclasses(text: str | None) -> list[str]:
    """Defensive, regex-only scan for `class <Name>(ConfigError)` declarations in
    `diagnostics.py`'s own text — the typed validation-failure hierarchy Module 7
    introduces. Never imports the learner's code; if the file declares none (or
    cannot be read), returns an empty list rather than raising.
    """
    if not text:
        return []
    try:
        return CONFIG_ERROR_SUBCLASS_RE.findall(text)
    except Exception:  # noqa: BLE001 - parsing is a bonus, never fatal
        return []


def read_diagnostics_source(directory: str) -> dict:
    """Real, cheap-to-derive facts about the learner's
    `src/platformops/diagnostics.py`: does it exist, how many test files does it
    have, and which `ConfigError` subclasses it declares. Only reads file text —
    never imports or executes it.
    """
    module_path = os.path.join(directory, "src", "platformops", "diagnostics.py")
    module_exists = os.path.isfile(module_path)
    test_file_count = find_diagnostics_tests(directory)

    text = None
    if module_exists:
        try:
            with open(module_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = None

    return {
        "module_exists": module_exists,
        "test_file_count": test_file_count,
        "config_error_subclasses": parse_config_error_subclasses(text),
    }


def run_diagnostics_demo(directory: str) -> dict:
    """Run `python -m platformops.diagnostics service.yaml` inside `directory`
    and capture its output — the M7 lens, Module 7's six-scenario reliability
    check run against the learner's own `service.yaml`. Uses the same
    uv-then-python3(-S) fallback pattern as the Configuration lens
    (`run_config_demo`): tries `uv run python -m platformops.diagnostics
    service.yaml` first, and falls back to plain `python3 -S -m
    platformops.diagnostics service.yaml` with `PYTHONPATH` pointed at the
    project's `src/` folder when `uv` is not on PATH or there is no `.venv`
    yet. The `-S` flag skips site-packages, same as the Configuration lens, so
    a learner who has not yet run `uv add pyyaml` / `uv sync` sees the same
    `ModuleNotFoundError` this lens is built to detect and explain gracefully
    — everywhere, not just on machines that happen to lack a global pyyaml
    install.
    """
    uv_path = shutil.which("uv")
    venv_exists = os.path.isdir(os.path.join(directory, ".venv"))

    env = None
    if uv_path and venv_exists:
        cmd = [uv_path, "run", "python", "-m", "platformops.diagnostics", "service.yaml"]
        command_label = "uv run python -m platformops.diagnostics service.yaml"
    else:
        cmd = [sys.executable, "-S", "-m", "platformops.diagnostics", "service.yaml"]
        command_label = (
            "python3 -m platformops.diagnostics service.yaml  (fallback: uv or "
            ".venv not found, using PYTHONPATH=src)"
        )
        env = dict(os.environ)
        src_dir = os.path.join(directory, "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")

    result = {
        "attempted": True,
        "command": command_label,
        "ok": False,
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr_snippet": None,
        "error": None,
        "missing_dependency_hint": None,
    }

    try:
        proc = subprocess.run(
            cmd,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=INVENTORY_TIMEOUT_SECS,
            env=env,
        )
        result["exit_code"] = proc.returncode
        result["ok"] = proc.returncode == 0
        result["stdout"] = proc.stdout or ""
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            result["stderr_snippet"] = stderr[:STDERR_SNIPPET_MAX_CHARS]
            if "ModuleNotFoundError" in stderr:
                result["missing_dependency_hint"] = (
                    "This fallback path deliberately has no third-party packages "
                    "installed yet (no pyyaml). Run it yourself: "
                    "uv run python -m platformops.diagnostics service.yaml"
                )
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["error"] = (
            f"The command took longer than {INVENTORY_TIMEOUT_SECS}s and was stopped."
        )
    except OSError as exc:
        result["error"] = f"Could not run the command ({exc})."

    return result


def read_diagnostics(directory: str) -> dict:
    """The M7 lens: real facts about the learner's
    `src/platformops/diagnostics.py` (source facts, always) plus — once the
    file exists — a live run of its six-scenario `python -m
    platformops.diagnostics service.yaml` check against the learner's own
    `service.yaml` (see `run_diagnostics_demo`).
    """
    source = read_diagnostics_source(directory)
    if not source["module_exists"]:
        return {**source, "demo": None}

    demo = run_diagnostics_demo(directory)
    return {**source, "demo": demo}


def build_ladder_state(tags_present: list[str]) -> dict:
    tag_set = set(tags_present)
    flat = flat_releases()

    last_reached_index = -1
    for i, (version, _module, _title) in enumerate(flat):
        if version in tag_set:
            last_reached_index = i

    parts_out = []
    idx = 0
    for part in LADDER:
        releases_out = []
        for version, module, title in part["releases"]:
            if idx < last_reached_index:
                status = "reached"
            elif idx == last_reached_index:
                status = "current"
            elif idx == last_reached_index + 1:
                status = "next"
            else:
                status = "upcoming"
            releases_out.append({
                "version": version,
                "module": module,
                "title": title,
                "status": status,
                "index": idx,
            })
            idx += 1
        parts_out.append({
            "part": part["part"],
            "title": part["title"],
            "modules": part["modules"],
            "releases": releases_out,
        })

    current = flat[last_reached_index] if last_reached_index >= 0 else None
    nxt = flat[last_reached_index + 1] if last_reached_index + 1 < len(flat) else None

    return {
        "total_releases": TOTAL_RELEASES,
        "reached_count": last_reached_index + 1,
        "current_version": current[0] if current else None,
        "current_title": current[2] if current else None,
        "next_version": nxt[0] if nxt else None,
        "next_title": nxt[2] if nxt else None,
        "parts": parts_out,
    }


def build_state() -> dict:
    directory = get_platformops_dir()
    foundation = read_foundation(directory)
    ladder = build_ladder_state(foundation["git"]["tags"])
    inventory = read_inventory(directory)
    module_map = read_module_map(directory)
    servicedef = read_servicedef(directory)
    config = read_config(directory)
    diagnostics = read_diagnostics(directory)
    return {
        "platformops_dir": directory,
        "foundation": foundation,
        "ladder": ladder,
        "inventory": inventory,
        "module_map": module_map,
        "servicedef": servicedef,
        "config": config,
        "diagnostics": diagnostics,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ReleaseLadder/0.1"

    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("[release-ladder] " + (fmt % args) + "\n")

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: str):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "index.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            self._send_html(INDEX_HTML)
        elif route == "/api/state":
            try:
                self._send_json(build_state())
            except Exception as exc:  # noqa: BLE001 - always answer the learner's page
                self._send_json({"error": str(exc)}, status=500)
        elif route == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404, "not found")


def main():
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    directory = get_platformops_dir()
    url = f"http://{DEFAULT_HOST}:{port}/"

    print("+--------------------------------------------------------------")
    print("|  Release Ladder")
    print("|")
    print(f"|  reading   : {directory}")
    print("|              (override: PLATFORMOPS_DIR=/path python3 serve.py,")
    print("|               or pass the path as the first argument)")
    print(f"|  open      : {url}")
    print(f"|  port busy?: PORT=8308 python3 serve.py")
    print("|  Ctrl-C stops the server. Read-only: no writes, ever.")
    print("+--------------------------------------------------------------")

    httpd = ThreadingHTTPServer((DEFAULT_HOST, port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
