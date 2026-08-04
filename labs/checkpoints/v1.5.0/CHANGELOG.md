# Changelog

All notable changes to the PlatformOps Toolkit are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/). Dates are omitted below -- each entry corresponds to
one course module's tagged release, not a calendar date.

## [Unreleased]

## [1.5.0] - Governed Agent Skill Library
### Added
- `scripts/skill_lint.py` -- a reusable lint tool for any `SKILL.md` file. Runs six mechanical
  checks in one command with a JSON report and a pass/fail verdict: frontmatter-to-body word
  ratio (under 20%), a stated trigger phrase, a Steps section with 4-8 numbered steps, the
  eight required section headings, an agent-neutral core (no agent name mentioned before an
  `## Optional extensions` section), and no write-mode file access or `git commit`/`git push`
  described in Steps. Stable exit codes: `0` every check passed, `1` one or more failed, `2`
  the file could not be read. Verified against a deliberately broken fixture as well as all
  three real skills, so a clean pass is proven, not assumed.
- `scripts/incident_context.py` -- a thin orchestrator that combines `git_status()`,
  `docker_info()` (Module 11) and `check_health()` (Module 9) into one first-response incident
  report. It imports every one of those from the tested package; it does not duplicate any of
  their logic. Stable exit codes: `0` nothing looks wrong, `1` at least one signal (a dirty
  tree, an unreachable Docker daemon, or an unhealthy endpoint) looks wrong.
- `.claude/skills/incident-context-collector/SKILL.md` -- a new skill, following the same
  quality bar as `service-readiness-review` and `platformops-service-readiness`, that runs
  `scripts/incident_context.py` and relays its combined report.
- `scripts/smoke_test_skill.sh` -- formalizes the manual cross-agent comparison Module 17-19
  each ran by hand. Given a skill name and a task description, it runs Claude Code and Codex
  non-interactively against the same skill, extracts the expected output field labels straight
  from the skill's own `## Output` section, and checks both agents' output for every label. A
  structural check, not a semantic one -- it confirms the right fields showed up, not that the
  judgments inside them are correct.
- `version` field added to all three skills' frontmatter (`service-readiness-review` 1.3.0,
  `platformops-service-readiness` 1.4.0, `incident-context-collector` 1.5.0), pinned to the
  `platformops` release each skill's content last changed in -- independent of the project's
  own version, so a skill's freshness is checkable on its own.
- `tests/test_skill_lint.py` and `tests/test_incident_context.py` -- coverage for every check
  `skill_lint.py` runs, and for a clean report, a dirty tree, an unreachable Docker daemon, an
  unhealthy endpoint, a skipped health check, and unknown-signal handling in
  `incident_context.py`.
### Changed
- `skills/CATALOGUE.md` -- added a "Shipped skills" table (all three real skills, their
  version, and the module that shipped them) and removed `incident-context-collector` from the
  candidate table now that it exists.

## [1.4.0] - Portable Service Readiness Skill
### Added
- `scripts/service_readiness.py` -- a thin orchestrator that combines three
  evidence sources into one release-readiness report: config validation
  (`platformops.config` + `platformops.servicedef`), local source-control
  state (`platformops.local_ops.git_status`), and CI status from the latest
  GitHub Actions run (`platformops.httpclient.list_workflow_runs`). It
  imports every one of those from the tested package; it does not duplicate
  any of their logic. Cloud and Kubernetes evidence are returned as a fixed
  `"status": "unknown"` -- this toolkit has no AWS or Kubernetes adapter yet
  (Module 21+ and Module 28+), and the script does not import anything or
  make a network call to pretend otherwise. `overall_confidence`
  (`high`/`medium`/`low`) is computed by `compute_confidence()` from a
  known-vs-unknown section ratio, with any FAIL/DIRTY signal capping it at
  `low` regardless of the ratio. Stable exit codes: `0` ready, `1` evidence
  gathered but something failed, `2` no evidence could be gathered at all.
- `.claude/skills/platformops-service-readiness/SKILL.md` -- a new skill,
  separate from `service-readiness-review`, that runs
  `scripts/service_readiness.py` and relays its combined report, including
  the `cloud`/`kubernetes` unknown sections verbatim rather than softening
  or omitting them.
- `tests/test_service_readiness.py` -- coverage for a clean/passing report,
  a dirty tree, a failing CI run, a config validation failure, a missing
  file, the always-unknown cloud/kubernetes sections, CI/source-control
  falling back to `UNKNOWN` when no repo is given or the check itself
  fails, and the `compute_confidence()` function directly.

## [1.3.0] - Script-Backed Agent Skill Foundation
### Added
- `scripts/review-service.py` -- a thin script that loads and validates a
  service definition file and emits one fixed-schema JSON readiness report
  with a stable exit code (`0` ready, `1` validation/observability failure,
  `2` file could not be read). Imports `validate_service` and
  `load_yaml_dict` from the package; it does not duplicate any validation
  logic itself.
### Changed
- `.claude/skills/service-readiness-review/SKILL.md` -- Step 2 now runs
  `scripts/review-service.py` instead of `platformops validate --json`;
  the agent relays the script's report instead of parsing raw JSON itself,
  so every run of the skill produces an identically worded report for the
  same input.

## [1.2.1] - Service Review Skill
### Added
- `.claude/skills/service-readiness-review/SKILL.md` — a skill that runs
  `platformops validate` against a service definition file and reports its
  readiness, including two checks a passing validation alone does not
  cover: an empty observability field, and the exact `kubernetes_namespace`
  value that was checked.
- `platformops version --json` — outputs version and Python runtime as JSON.

## [1.2.0] - AI-Assisted Engineering Harness
### Added
- `CLAUDE.md` — the project's coding-agent harness: project context,
  approved commands, safety rules and a definition of done.
- `scripts/verify.sh` — the one command a coding agent (or you) runs to
  check a change against the harness's Definition of done.

## [1.1.0] - Maintainable PlatformOps Foundation
### Added
- `CHANGELOG.md` (this file).
- `py.typed` marker (PEP 561) so a project that imports `platformops` gets real type checking too.
- Type annotations tightened in `config.py` (`dict` -> `dict[str, Any]`).
- `mypy` wired as a quality gate.
- `scripts/check-secrets.sh` -- a grep-based scanner for common credential patterns.
- `scripts/check-gates.sh` -- runs format, lint, type check, tests and secret scan in one command.
- `scripts/check-deps.sh` -- a dependency security check via `pip-audit` (Phase 2).
- Type annotations extended across the remaining modules (Phase 2, coding-agent-authored).

## [1.0.0] - Tested Automation Core
### Added
- Hand-written pytest coverage for real gaps: a parametrized service-definition test, a
  `subprocess`-safety fixture for `local_ops.py`, a config-CLI idempotency test, and an end-to-end
  `check-security` test with no mocking.
- `pytest-cov`, wired to report which lines nothing in the suite ever executes.
- Coding-agent-authored tests closing the remaining gaps in `httpclient.py`.

## [0.9.0] - Local Operations Adapter
### Added
- `local_ops.py`: `git_status()`, `docker_info()`, `container_list()` -- `subprocess` wrappers with
  no `shell=True` and an always-present timeout.

## [0.8.0] - Concurrent Health Checker
### Added
- Concurrent health checks in `httpclient.py`, with a bounded concurrency limit.

## [0.7.0] - Repository and API Inspector
### Added
- `get_repo_info()` in `httpclient.py` -- retry with backoff, and pagination handling.

## [0.6.0] - PlatformOps CLI
### Added
- `cli.py` -- the `platformops` command, built on Typer, with `--json` output for every command.

## [0.5.0] - Reliable Validation and Diagnostics
### Added
- `diagnostics.py` -- a fast, scriptable health check for one service definition file.
- Structured logging in `config.py` and `diagnostics.py`, off by default, enabled with `--verbose`.

## [0.4.0] - Service Configuration Validator
### Added
- `config.py` -- loads a service definition YAML file, resolves `PLATFORMOPS_*` environment
  overrides, writes the resolved config back out atomically.

## [0.3.0] - Service Definition Model
### Added
- `servicedef.py` -- the `ServiceDefinition` Pydantic model and `validate_service()`.

## [0.2.0] - Modular Inventory Engine
### Added
- `inventory/` package -- `data.py`, `rules.py`, `report.py`, `summary.py`.

## [0.1.0] - Infrastructure Inventory Reporter
### Added
- First working inventory report, over a small in-memory dataset.

## [0.0.0] - Project Foundation
### Added
- Project scaffold: `uv`-managed `src/` layout, `pyproject.toml`, initial `README.md`.
