#!/usr/bin/env python3
"""Run the mechanical governance checks a SKILL.md file must pass, as one command.

Module 17's deep dive measured a skill's frontmatter-to-body word ratio, its step count,
and whether its required sections stayed agent-neutral -- all by hand, one grep at a time.
Module 19's deep dive proved read-only enforcement the same way, by hand, again. Every new
skill this project ships needs the same checks re-run. This script is that hand-run check,
written once: point it at any `SKILL.md` file and it prints one fixed JSON report with a
pass/fail verdict for each check plus an overall verdict, so the check never has to be
re-invented per module.

This script does not know anything about what a *good* skill says -- it only checks the
mechanical shape M17's and M19's skills already proved works: a short frontmatter relative
to the body, a stated trigger phrase, a Steps section with a sane number of steps, the eight
section headings the existing skills already use, no agent name mentioned before an
"Optional extensions" section, and no write-mode file access described in the Steps section
of a skill that claims to be read-only.

Exit codes:
  0 -- every check passed
  1 -- one or more checks failed
  2 -- the target file could not be read
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REQUIRED_HEADINGS = [
    "When to use this",
    "When NOT to use this",
    "Preconditions",
    "Steps",
    "Tools this skill may use",
    "Output",
    "Failure handling",
    "Optional extensions",
]

TRIGGER_PHRASES = ["use this when", "use this skill when"]

MAX_FRONTMATTER_RATIO = 0.20
MIN_STEPS = 4
MAX_STEPS = 8

# A skill that claims to be read-only should never describe writing a file or
# committing/pushing to git in its Steps -- the same two things M19's deep dive
# grepped for by hand: a write-mode `open()` call, and `git commit`/`git push`.
WRITE_PATTERNS = [
    re.compile(r"open\([^)]*['\"]w"),
    re.compile(r"git commit"),
    re.compile(r"git push"),
]


def parse_frontmatter(text: str) -> tuple[str, str]:
    """Split a SKILL.md file into its `---`-delimited frontmatter and the body after it."""
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def extract_section(body: str, heading: str) -> str | None:
    """Return the text under a `## <heading>` line, up to the next `##` heading or EOF."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
    )
    match = pattern.search(body)
    return match.group(1) if match else None


def check_frontmatter_ratio(frontmatter: str, body: str) -> dict[str, Any]:
    frontmatter_words = len(frontmatter.split())
    total_words = frontmatter_words + len(body.split())
    ratio = frontmatter_words / total_words if total_words else 0.0
    passed = ratio < MAX_FRONTMATTER_RATIO
    return {
        "name": "frontmatter_ratio",
        "passed": passed,
        "detail": f"{ratio:.1%} of {total_words} total words are frontmatter "
        f"(must be under {MAX_FRONTMATTER_RATIO:.0%})",
    }


def check_trigger_phrase(full_text: str) -> dict[str, Any]:
    lowered = full_text.lower()
    found = any(phrase in lowered for phrase in TRIGGER_PHRASES)
    return {
        "name": "trigger_phrase",
        "passed": found,
        "detail": "trigger phrase found"
        if found
        else "no 'use this when' (or equivalent) trigger phrase found",
    }


def check_step_count(body: str) -> dict[str, Any]:
    steps_section = extract_section(body, "Steps")
    if steps_section is None:
        return {
            "name": "step_count",
            "passed": True,
            "detail": "no Steps section -- check skipped",
        }
    count = len(re.findall(r"^\d+\.\s", steps_section, re.MULTILINE))
    passed = MIN_STEPS <= count <= MAX_STEPS
    return {
        "name": "step_count",
        "passed": passed,
        "detail": f"{count} step(s) (must be {MIN_STEPS}-{MAX_STEPS})",
    }


def check_required_headings(body: str) -> dict[str, Any]:
    headings = [h.strip() for h in re.findall(r"^##\s+(.+)$", body, re.MULTILINE)]
    missing = [h for h in REQUIRED_HEADINGS if h not in headings]
    passed = not missing
    return {
        "name": "required_headings",
        "passed": passed,
        "detail": "all required headings present"
        if passed
        else f"missing: {', '.join(missing)}",
    }


def check_agent_neutral_core(body: str) -> dict[str, Any]:
    split_index = body.find("## Optional extensions")
    core = body if split_index == -1 else body[:split_index]
    mentions = re.findall(r"\bclaude\b", core, re.IGNORECASE)
    passed = len(mentions) == 0
    return {
        "name": "agent_neutral_core",
        "passed": passed,
        "detail": "no agent name before Optional extensions"
        if passed
        else f"{len(mentions)} agent-name mention(s) before Optional extensions",
    }


def check_read_only(body: str) -> dict[str, Any]:
    steps_section = extract_section(body, "Steps") or body
    findings = [
        match.group(0)
        for pattern in WRITE_PATTERNS
        for match in pattern.finditer(steps_section)
    ]
    passed = len(findings) == 0
    return {
        "name": "read_only",
        "passed": passed,
        "detail": "no write-mode file access or git commit/push in Steps"
        if passed
        else f"found: {', '.join(findings)}",
    }


def lint(path: Path) -> dict[str, Any]:
    """Run every check against one SKILL.md file and return the combined report."""
    text = path.read_text()
    frontmatter, body = parse_frontmatter(text)
    checks = [
        check_frontmatter_ratio(frontmatter, body),
        check_trigger_phrase(text),
        check_step_count(body),
        check_required_headings(body),
        check_agent_neutral_core(body),
        check_read_only(body),
    ]
    return {
        "file": str(path),
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: skill_lint.py <path to SKILL.md>", file=sys.stderr)
        return 2

    path = Path(argv[0])
    if not path.is_file():
        print(
            json.dumps({"file": str(path), "passed": False, "error": "file not found"})
        )
        return 2

    report = lint(path)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
