# CLAUDE.md — PlatformOps Toolkit

## Project

PlatformOps Toolkit is a command-line tool that inspects, validates and
troubleshoots services across source control, CI/CD, cloud and Kubernetes.
It is a real, working Python project, not a demo — every command in it is
tested and used the way it is documented here.

- **Language / tooling:** Python 3.12, managed with `uv`. No plain `pip`.
- **CLI framework:** Typer (`src/platformops/cli.py` is the entry point).
- **HTTP client:** `httpx`. Never add `requests` — this project standardized
  on `httpx` and does not use two HTTP libraries for the same job.
- **Data validation:** Pydantic.
- **Config files:** PyYAML.

### Repository structure

- `src/platformops/` — the package itself; `cli.py` is the CLI, everything
  else is an importable, independently tested module.
- `tests/` — one test file per source module, run with `pytest`.
- `scripts/` — shell scripts that wrap the project's quality gates
  (`verify.sh`, `check-secrets.sh`, `check-deps.sh`).

## Approved commands

- `uv run pytest` — run the test suite
- `uv run pytest -q` — quiet mode
- `uv run ruff check src/ tests/` — lint
- `uv run ruff format src/ tests/` — format
- `uv run mypy src/` — type check
- `uv run platformops --help` — test the CLI

Do not invoke `pytest`, `ruff`, or `mypy` directly. Always run them through
`uv run` so they use the project's managed environment, not whatever is on
the system `PATH`.

## Safety rules

- Never delete `uv.lock` — it pins exact dependency versions.
- Never run `rm -rf` on project directories.
- Never add dependencies without explicit approval.
- Never modify the version in `pyproject.toml` without following the
  release checklist.
- Always run the verify script before declaring a task done.

## Definition of done

1. All tests pass: `uv run pytest`
2. Linter clean: `uv run ruff check src/ tests/`
3. Types check: `uv run mypy src/`
4. No new ruff warnings introduced
5. `CHANGELOG.md` updated with an `[Unreleased]` entry
