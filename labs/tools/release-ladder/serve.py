#!/usr/bin/env python3
"""Release Ladder — serve.py

Shows the learner where they really are on the PlatformOps release ladder
(v0.0 -> v3.0, 37 tagged releases across 10 parts of the course) plus two
lenses: a Project Foundation panel (M2) with the real state of the
learner's own `~/platformops` project, and an Inventory Reporter panel (M3)
that actually runs the learner's own `src/platformops/inventory.py` and
shows its real output.

Zero third-party dependencies. Python 3 standard library only. Mostly
read-only: it runs `git tag` / `git status` / `git rev-parse` (never a
write command) and reads files on disk. The one exception is the M3 lens,
which runs the learner's own `python -m platformops.inventory` (their v0.1
report) so it can show the real report — it never runs ruff, pytest, or
anything that installs or writes to their project.

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
    return {
        "platformops_dir": directory,
        "foundation": foundation,
        "ladder": ladder,
        "inventory": inventory,
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
