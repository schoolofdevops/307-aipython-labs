---
name: platformops-service-readiness
description: >
  Produce a release-readiness report for a PlatformOps service definition
  file (service*.yaml), combining config validation, local source-control
  state, and CI status from GitHub Actions into one confidence-scored
  report. Runs scripts/service_readiness.py, which gathers all evidence and
  prints one fixed JSON report every time, not raw output to reinterpret.
  Cloud and Kubernetes evidence are explicitly reported as unknown -- this
  toolkit has no AWS or Kubernetes adapter yet -- never guessed or silently
  left out. Use this when asked whether a service is ready to release or
  deploy, or for a release-readiness report covering config, git and CI.
  Do not use this for a plain schema check (use service-readiness-review
  for that) or to check cloud or Kubernetes state, which this skill cannot
  do yet. Read-only: this skill never edits, commits, or deploys anything.
---

# PlatformOps Service Readiness

## When to use this

Use this skill when someone asks whether a service is ready to release or deploy, or wants a
combined readiness report that looks at more than the service definition file by itself --
config validity, whether the local repository has uncommitted changes, and whether the latest
CI run passed. The trigger is a request for an overall release/readiness call, not just "is this
YAML valid" (that narrower question belongs to `service-readiness-review`).

## When NOT to use this

- Not for a plain schema check on a `service*.yaml` file with nothing else in scope -- use
  `service-readiness-review` for that; it is faster and does not need a `--repo` argument.
- Not to check AWS or Kubernetes state. This toolkit has no adapter for either yet (they arrive
  in a later module), and this skill's `cloud` and `kubernetes` sections always report
  `"status": "unknown"` for that reason -- it never guesses at either one.
- Not to fix, edit, commit, or deploy anything. This skill only reads and reports.

## Preconditions

- You are working inside a PlatformOps project: a `pyproject.toml` and `src/platformops/` are
  present, and `uv sync` has already been run.
- The target file exists and is a service definition -- named `service*.yaml`, or explicitly
  identified as one by whoever asked for the review.
- If a GitHub repository is in scope for the CI check, you know its `owner/name` -- otherwise
  the CI section will honestly report `UNKNOWN` rather than being skipped without explanation.

## Steps

1. Confirm the target file matches `service*.yaml` (or was explicitly named as a service
   definition) and note its path.
2. Confirm whether a GitHub repository is in scope for the CI check. If one was named, note its
   `owner/name`; if none was given, proceed without it -- the report will mark CI `UNKNOWN`
   rather than silently omitting the section.
3. From the project root, run
   `uv run python scripts/service_readiness.py <path> --repo <owner/name>` (omit `--repo` if
   none applies) and capture its exit code and its JSON report on stdout.
4. Read the exit code before reading anything else: `0` means evidence was gathered and nothing
   failed, `1` means evidence was gathered and something failed (bad config, a dirty tree, or a
   failing CI run), `2` means the service definition file itself could not be read -- there is
   no evidence to report yet.
5. Do not re-derive, recompute, or second-guess any field in the report -- not the `config`,
   `source_control`, or `ci` verdicts, and especially not `overall_confidence` or
   `recommendation`. The script already gathered every section and computed the confidence
   score; your job is to relay its report into the Output format below.
6. Relay the `cloud` and `kubernetes` sections exactly as printed -- both always read
   `"status": "unknown"` with a `reason` naming the future module. Do not soften this into "not
   checked" or drop the sections; the report exists specifically to name what it does not know.
7. If the exit code was `2`, report the single entry in `config.problems` as the reason no
   readiness call could be made, and stop -- do not attempt to fill in the other Output fields
   from a report that was never gathered.

## Tools this skill may use

- Run `uv run python scripts/service_readiness.py <path> [--repo <owner/name>]`.
- Read the target `service*.yaml` file only if the script itself cannot run (see Failure
  handling) -- never as a substitute for running it.

This skill does not write, edit, or delete any file, and it does not commit, push, or deploy
anything. It only reads and reports.

## Output

A short readiness report, in this shape:

```
Service Readiness Report -- <service>

- Config: PASS | FAIL (<problems, one per line, or "none">)
- Source control: CLEAN | DIRTY | UNKNOWN (<uncommitted file count, or "not available">)
- CI: PASS | FAIL | UNKNOWN (latest run: <conclusion, or "not available">)
- Cloud: unknown -- <reason from the report>
- Kubernetes: unknown -- <reason from the report>
- Overall confidence: high | medium | low
- Recommendation: <the recommendation field, verbatim>
```

## Failure handling

- If `uv run python scripts/service_readiness.py` cannot run at all (wrong directory, `uv` not
  found, project not synced), stop and report the environment problem. Do not fall back to
  reading the YAML, running `git status`, or querying GitHub yourself -- that is exactly the
  re-reasoning this skill exists to avoid.
- If the script prints nothing, or prints text that does not parse as JSON, stop and report
  that the script failed unexpectedly. Do not invent a report to fill the gap.
- Exit code `2` already covers "the service definition file could not be read" -- do not add
  your own extra check for that case before running the script.
- A `source_control` or `ci` section reading `UNKNOWN` is not a script failure -- it is the
  script correctly reporting that this particular signal was not available (no `--repo` given,
  the repository unreachable, or not a git working tree). Relay it as `UNKNOWN`, do not treat it
  as an error to explain away.

## Optional extensions

The steps above work with any coding agent that can run a shell command and read its output --
nothing above assumes a specific harness. This section is for a convenience specific to one
agent; skip it entirely with any other agent.

- **Claude Code only:** if you have the `TodoWrite` tool available, you may use it to track
  Steps 1-7 above as a checklist while you work through them. This is a display convenience --
  it changes nothing about what the steps do or what the report contains.
