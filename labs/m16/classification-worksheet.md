# Classify the Ten Examples

For each example, write one classification in the **Your answer** column. Use exactly one of
these five labels:

- **repository instruction** — read by the agent, in full, every task
- **skill** — read by the agent, in full, only when the task matches it
- **python library** — code that runs; the agent does not read it as instructions
- **cli command** — run directly, by you or the agent
- **mcp tool** — a capability called over the Model Context Protocol, from outside the project

## Examples

| # | Example | Your answer |
|---|---------|-------------|
| 1 | The `## Safety rules` section inside `~/platformops/CLAUDE.md` — "Never delete `uv.lock` without explicit approval." |  |
| 2 | `uv run pytest -q` — the exact test command listed under CLAUDE.md's Approved commands. |  |
| 3 | A markdown file at `.claude/skills/service-readiness/SKILL.md`, with a name, a description, and numbered steps for reviewing a service definition file before it ships — loaded by the agent only when you ask for that specific review. |  |
| 4 | `src/platformops/httpclient.py` — the module with `check_health()`, `get_repo_info()`, and the retry/backoff logic other code calls. |  |
| 5 | `Typer`, the third-party package `cli.py` imports to build the command-line interface. |  |
| 6 | `scripts/verify.sh` — the one command that runs lint, types, tests and the secret scan in sequence. |  |
| 7 | A file at `.claude/skills/changelog-entry/SKILL.md` that tells the agent, step by step, how to add a new CHANGELOG.md entry in this project's exact format — read only right before the agent reports a task done. |  |
| 8 | The `## Definition of done` checklist inside CLAUDE.md — tests pass, linter clean, types check, changelog updated. |  |
| 9 | A server, planned for later in this course, that exposes `check-security` and `check-deps` as callable tools any MCP-compatible agent can invoke over a standard protocol, without importing `platformops` as a Python package. |  |
| 10 | A local server that wraps `kubectl` commands so a coding agent can query a `kind` cluster without shelling out itself — a capability the agent calls over the MCP protocol, not by importing Python. |  |
