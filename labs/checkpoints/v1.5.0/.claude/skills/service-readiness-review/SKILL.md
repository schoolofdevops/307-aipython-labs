---
name: service-readiness-review
version: 1.3.0
description: >
  Review a PlatformOps service definition file (service*.yaml) for
  readiness before it ships, merges, or deploys. Runs
  scripts/review-service.py, which validates the file and checks two
  things a passing schema validation can still miss -- empty observability
  fields and a namespace value worth quoting back to the reviewer -- and
  returns one fixed JSON report every time, not raw output to reinterpret.
  Use this when asked to review a service*.yaml file, check whether a
  service is ready to deploy, or look over a change to a service
  definition before it merges. Do not use this for Kubernetes manifests,
  CI workflow files, or any YAML file that is not a PlatformOps service
  definition -- and do not use it to edit the file, only to report on it.
---

# Service Readiness Review

## When to use this

Use this skill when someone asks you to review a `service*.yaml` file, decide whether a
service is ready to deploy, or check a change to a service definition before it merges. The
trigger is the request, not the file extension alone -- a `.yaml` file that is not a service
definition does not qualify.

## When NOT to use this

- Not for Kubernetes manifests, Helm values files, or CI workflow YAML -- these are not
  PlatformOps service definitions, even though they are also YAML.
- Not as a substitute for `scripts/verify.sh` before a code change. That script gates changes
  to `src/platformops/`; this skill reviews one service definition file.
- Not to fix or edit the file. This skill only reports what it finds -- it does not write to
  the reviewed file or to any other file.

## Preconditions

- You are working inside a PlatformOps project: a `pyproject.toml` and `src/platformops/` are
  present, and `uv sync` has already been run.
- The target file exists and you can read it.
- The target file is a service definition -- named `service*.yaml`, or explicitly identified
  as one by whoever asked for the review.

## Steps

1. Confirm the target file matches `service*.yaml` (or was explicitly named as a service
   definition) and note its path.
2. From the project root, run `uv run python scripts/review-service.py <path>` and capture its
   exit code and its JSON report on stdout.
3. Read the exit code before reading anything else: `0` means the file is ready to ship, `1`
   means the report's `problems` list explains what to fix, `2` means the file itself could not
   be read (missing, unreadable, or not valid YAML) -- there is no service definition to check
   yet.
4. Do not re-derive, recompute, or second-guess any field in the report. The script already
   ran the schema validation and the observability checks; your job is to relay its `file`,
   `validation`, `problems`, `observability`, `namespace`, and `recommendation` fields into the
   Output format below, not to re-read the YAML and check them yourself.
5. If the exit code was `2`, report the single entry in `problems` as the reason the file
   cannot be reviewed, and stop -- do not attempt to fill in the other Output fields from a
   report you did not get.

## Tools this skill may use

- Run `uv run python scripts/review-service.py <path>`.
- Read the target `service*.yaml` file only if the script itself cannot run (see Failure
  handling) -- never as a substitute for running it.

This skill does not write, edit, or delete any file. It only reads and reports.

## Output

A short readiness report, in this shape:

```
Service Readiness Review -- <file>

- Validation: PASS | FAIL
- Problems found: <one per line, field + message -- or "none">
- Observability: dashboard_url set (yes/no), alert_channel set (yes/no)
- Namespace checked: <the exact kubernetes_namespace value>
- Recommendation: ready to ship | fix required before shipping
```

## Failure handling

- If `uv run python scripts/review-service.py` cannot run at all (wrong directory, `uv` not
  found, project not synced), stop and report the environment problem. Do not fall back to
  reading the YAML yourself and guessing at its validity -- that is exactly the re-reasoning
  this skill exists to avoid.
- If the script prints nothing, or prints text that does not parse as JSON, stop and report
  that the script failed unexpectedly. Do not invent a report to fill the gap.
- Exit code `2` already covers "file missing" and "not valid YAML" -- do not add your own
  extra check for either case before running the script.

## Optional extensions

The steps above work with any coding agent that can run a shell command and read its output --
nothing above assumes a specific harness. This section is for a convenience specific to one
agent; skip it entirely with any other agent.

- **Claude Code only:** if you have the `TodoWrite` tool available, you may use it to track
  Steps 1-5 above as a checklist while you work through them. This is a display convenience --
  it changes nothing about what the steps do or what the report contains.
