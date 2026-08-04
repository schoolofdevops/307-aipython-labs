# Changelog

All notable changes to the PlatformOps Toolkit are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/). Dates are omitted below -- each entry corresponds to
one course module's tagged release, not a calendar date.

## [Unreleased]

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
