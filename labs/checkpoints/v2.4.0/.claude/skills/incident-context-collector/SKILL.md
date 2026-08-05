---
name: incident-context-collector
version: 1.5.0
description: >
  Gather a project's first-response incident context -- recent git status,
  container status, and an optional service health check -- into one short
  report. Runs scripts/incident_context.py, which calls three already-tested
  functions (git_status, docker_info, check_health) and prints one fixed
  JSON report every time, not raw output to reinterpret. Use this when
  responding to a production incident and the first few minutes would
  otherwise be spent gathering this same context by hand: has anything
  changed locally, is the container runtime up, and is the service actually
  failing its health check. Do not use this to diagnose *why* something is
  wrong, only to gather the first-response facts -- and do not use it to fix
  anything, it is read-only.
---

# Incident Context Collector

## When to use this

Use this skill when someone asks for first-response context at the start of an incident --
"what's going on with this service" or "gather what we know before we start digging." The
trigger is the start of an incident investigation, not a routine status check; for a release
readiness question, use `platformops-service-readiness` instead.

## When NOT to use this

- Not to diagnose the root cause of an incident. This skill gathers three surface signals; it
  does not analyze logs, correlate metrics, or explain why a signal looks wrong.
- Not to check release readiness before a deploy -- `platformops-service-readiness` already
  covers that combination of config, source control and CI.
- Not to fix anything. This skill only reads and reports -- it never restarts a container,
  reverts a change, or edits a file.

## Preconditions

- You are working inside a PlatformOps project: a `pyproject.toml` and `src/platformops/` are
  present, and `uv sync` has already been run.
- You know which local repository path is in scope (the current project, or one named by
  whoever asked).
- If a service's health endpoint should be checked, you know its URL -- otherwise the health
  section will honestly report `SKIPPED` rather than being checked without a target.

## Steps

1. Confirm which repository path is in scope for the git check, and whether a health-check URL
   was given. If neither was named explicitly, use the current project directory and omit the
   health check.
2. From the project root, run
   `uv run python scripts/incident_context.py --repo-path <path> [--health-url <url>]` (omit
   `--health-url` if none applies) and capture its exit code and its JSON report on stdout.
3. Read the exit code before reading anything else: `0` means nothing gathered looks wrong yet,
   `1` means at least one signal -- a dirty tree, an unreachable Docker daemon, or an unhealthy
   endpoint -- looks wrong.
4. Do not re-derive, recompute, or second-guess any field in the report. The script already
   called `git_status()`, `docker_info()` and `check_health()` and combined their answers; your
   job is to relay its `git`, `docker`, `health` and `has_concern` fields into the Output format
   below, not to run `git status` or `docker ps` yourself.
5. If the `git` or `health` section reads `UNKNOWN`, relay it as `UNKNOWN` with its `error`
   field -- that means the signal itself could not be gathered (no git repository, no reachable
   URL), not that something is wrong with the service. Do not treat it as a concern.
6. Report the four sections and the overall `has_concern` verdict in the Output format below.

## Tools this skill may use

- Run `uv run python scripts/incident_context.py --repo-path <path> [--health-url <url>]`.
- Read the repository's recent commit history with `git log` only if the script itself cannot
  run (see Failure handling) -- never as a substitute for running it.

This skill does not write, edit, or delete any file, and it does not restart, deploy, or roll
back anything. It only reads and reports.

## Output

A short first-response report, in this shape:

```
Incident Context -- <repo_path>

- Git: CLEAN | DIRTY | UNKNOWN (<changed file count, or the error>)
- Docker: AVAILABLE | UNAVAILABLE (<containers running, or the error>)
- Health: OK | UNHEALTHY | SKIPPED | UNKNOWN (<url and status code, or "no URL given">)
- Overall: concern found | nothing wrong yet
```

## Failure handling

- If `uv run python scripts/incident_context.py` cannot run at all (wrong directory, `uv` not
  found, project not synced), stop and report the environment problem. Do not fall back to
  running `git status` or `docker ps` yourself and guessing at the report -- that is exactly the
  re-reasoning this skill exists to avoid.
- If the script prints nothing, or prints text that does not parse as JSON, stop and report that
  the script failed unexpectedly. Do not invent a report to fill the gap.
- A `git` or `health` section reading `UNKNOWN` is not a script failure -- it is the script
  correctly reporting that this particular signal was not available. Relay it as `UNKNOWN`, do
  not treat it as an error to explain away.

## Optional extensions

The steps above work with any coding agent that can run a shell command and read its output --
nothing above assumes a specific harness. This section is for a convenience specific to one
agent; skip it entirely with any other agent.

- **Claude Code only:** if you have the `TodoWrite` tool available, you may use it to track
  Steps 1-6 above as a checklist while you work through them. This is a display convenience --
  it changes nothing about what the steps do or what the report contains.
