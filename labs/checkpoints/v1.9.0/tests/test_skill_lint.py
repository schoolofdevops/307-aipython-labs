import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "skill_lint.py"
_spec = importlib.util.spec_from_file_location("skill_lint", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
skill_lint = importlib.util.module_from_spec(_spec)
sys.modules["skill_lint"] = skill_lint
_spec.loader.exec_module(skill_lint)

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID_SKILL = """---
name: test-skill
description: >
  Do a thing well. Use this when someone asks for the thing to be done.
---

# Test Skill

## When to use this

Use this skill when asked to do the thing.

## When NOT to use this

Not for unrelated things.

## Preconditions

- Some precondition is met.

## Steps

1. Confirm the request.
2. Gather the input.
3. Run the check.
4. Report the result.

## Tools this skill may use

- Run a read-only command.

## Output

A short report in a fixed shape.

## Failure handling

- If the command cannot run, report the environment problem.

## Optional extensions

- **Claude Code only:** you may track the steps above with TodoWrite.
"""


def _write(tmp_path, text):
    path = tmp_path / "SKILL.md"
    path.write_text(text)
    return path


def test_valid_skill_passes_every_check(tmp_path):
    path = _write(tmp_path, VALID_SKILL)

    report = skill_lint.lint(path)

    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"])


def test_missing_trigger_phrase_fails_that_check(tmp_path):
    text = VALID_SKILL.replace(
        "Use this when someone asks for the thing to be done.",
        "This is handy for the thing.",
    ).replace("Use this skill when asked to do the thing.", "Handy for the thing.")
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    trigger_check = next(c for c in report["checks"] if c["name"] == "trigger_phrase")
    assert trigger_check["passed"] is False


def test_too_few_steps_fails_step_count_check(tmp_path):
    text = VALID_SKILL.replace(
        "1. Confirm the request.\n2. Gather the input.\n3. Run the check.\n4. Report the result.\n",
        "1. Confirm the request.\n2. Report the result.\n",
    )
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    step_check = next(c for c in report["checks"] if c["name"] == "step_count")
    assert step_check["passed"] is False


def test_too_many_steps_fails_step_count_check(tmp_path):
    extra_steps = "".join(f"{n}. Step number {n}.\n" for n in range(1, 10))
    text = VALID_SKILL.replace(
        "1. Confirm the request.\n2. Gather the input.\n3. Run the check.\n4. Report the result.\n",
        extra_steps,
    )
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    step_check = next(c for c in report["checks"] if c["name"] == "step_count")
    assert step_check["passed"] is False


def test_missing_required_heading_fails_that_check(tmp_path):
    text = VALID_SKILL.replace("## Failure handling\n\n", "")
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    heading_check = next(
        c for c in report["checks"] if c["name"] == "required_headings"
    )
    assert heading_check["passed"] is False
    assert "Failure handling" in heading_check["detail"]


def test_agent_name_before_optional_extensions_fails_neutral_core_check(tmp_path):
    text = VALID_SKILL.replace(
        "## Steps\n",
        "## Steps\n\nClaude Code should do this carefully.\n",
    )
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    neutral_check = next(
        c for c in report["checks"] if c["name"] == "agent_neutral_core"
    )
    assert neutral_check["passed"] is False


def test_agent_name_after_optional_extensions_still_passes(tmp_path):
    path = _write(tmp_path, VALID_SKILL)

    report = skill_lint.lint(path)

    neutral_check = next(
        c for c in report["checks"] if c["name"] == "agent_neutral_core"
    )
    assert neutral_check["passed"] is True


def test_write_mode_open_in_steps_fails_read_only_check(tmp_path):
    text = VALID_SKILL.replace(
        "1. Confirm the request.\n",
        "1. Confirm the request.\n2. Save it with open(path, 'w') as f.\n",
    )
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    read_only_check = next(c for c in report["checks"] if c["name"] == "read_only")
    assert read_only_check["passed"] is False


def test_git_commit_in_steps_fails_read_only_check(tmp_path):
    text = VALID_SKILL.replace(
        "1. Confirm the request.\n",
        "1. Confirm the request.\n2. Run git commit to save the change.\n",
    )
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    read_only_check = next(c for c in report["checks"] if c["name"] == "read_only")
    assert read_only_check["passed"] is False


def test_frontmatter_heavy_skill_fails_ratio_check(tmp_path):
    heavy_description = " ".join(f"padding{n}" for n in range(80))
    text = f"""---
name: test-skill
description: >
  {heavy_description} Use this when needed.
---

# Test Skill

## When to use this

Use this skill sometimes.

## When NOT to use this

Not otherwise.

## Preconditions

- None.

## Steps

1. Do it.
2. Check it.
3. Report it.
4. Stop.

## Tools this skill may use

- A command.

## Output

Short.

## Failure handling

- Report it.

## Optional extensions

None.
"""
    path = _write(tmp_path, text)

    report = skill_lint.lint(path)

    assert report["passed"] is False
    ratio_check = next(c for c in report["checks"] if c["name"] == "frontmatter_ratio")
    assert ratio_check["passed"] is False


def test_main_exits_0_for_passing_skill(tmp_path, capsys):
    path = _write(tmp_path, VALID_SKILL)

    exit_code = skill_lint.main([str(path)])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["passed"] is True


def test_main_exits_1_for_failing_skill(tmp_path, capsys):
    text = VALID_SKILL.replace("## Failure handling\n\n", "")
    path = _write(tmp_path, text)

    exit_code = skill_lint.main([str(path)])

    assert exit_code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["passed"] is False


def test_main_exits_2_for_missing_file(tmp_path, capsys):
    path = tmp_path / "does-not-exist.md"

    exit_code = skill_lint.main([str(path)])

    assert exit_code == 2


def test_real_service_readiness_review_skill_passes(capsys):
    path = REPO_ROOT / ".claude" / "skills" / "service-readiness-review" / "SKILL.md"

    exit_code = skill_lint.main([str(path)])

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0, printed
    assert printed["passed"] is True


def test_real_platformops_service_readiness_skill_passes(capsys):
    path = (
        REPO_ROOT / ".claude" / "skills" / "platformops-service-readiness" / "SKILL.md"
    )

    exit_code = skill_lint.main([str(path)])

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0, printed
    assert printed["passed"] is True
