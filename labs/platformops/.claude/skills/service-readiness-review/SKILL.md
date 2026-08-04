---
name: service-readiness-review
description: >
  Review a PlatformOps service definition file (service*.yaml) for
  readiness before it ships, merges, or deploys. Runs the project's
  validator, then checks two things a passing validation can still miss:
  empty observability fields and a namespace value worth quoting back to
  the reviewer. Use this when asked to review a service*.yaml file, check
  whether a service is ready to deploy, or look over a change to a service
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
2. Read the file and check which top-level fields are present: `name`, `repository`,
   `environment`, `team_owner`, `kubernetes_namespace`, `deployment_name`, `aws_account`,
   `region`, and `observability` with its `dashboard_url` and `alert_channel`. This first pass
   is so you already know what to expect before the formal check runs.
3. From the project root, run `uv run platformops validate <path> --json` and capture the full
   output and exit code.
4. If the command exits non-zero, read the `errors` list in the JSON output. Each entry names
   the field (`loc`) and the problem (`msg`). Skip to Step 7 and report these as the reason the
   file is not ready.
5. If the command exits 0, the file passed schema validation. Still check
   `observability.dashboard_url` and `observability.alert_channel` for yourself -- the schema
   accepts an empty string for either one, so a passing validation can hide a missing on-call
   channel.
6. Also quote back the exact `kubernetes_namespace` value in your report, even on a pass. The
   validator has already rejected an invalid one; restating the value that was checked makes
   the report useful to a reviewer who has not opened the file.
7. Write the readiness report in the format described under Output, whether the file passed or
   failed.

## Tools this skill may use

- Run `uv run platformops validate <path> --json`.
- Read the target `service*.yaml` file.
- Search within the file for field names and values.

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

- If `uv run platformops validate` cannot run at all (wrong directory, `uv` not found, project
  not synced), stop and report the environment problem. Do not guess at the file's validity
  from reading the YAML alone.
- If the file is not valid YAML, `validate` reports a parse error and exits non-zero. Report
  this as "cannot be reviewed" -- a parse failure is not the same as a schema failure, and the
  report should say which one happened.
- If the target file cannot be found, stop before Step 3 and report the missing precondition
  instead of reviewing a different file.

## Optional extensions

The steps above work with any coding agent that can run a shell command and read a file --
nothing above assumes a specific harness. This section is for a convenience specific to one
agent; skip it entirely with any other agent.

- **Claude Code only:** if you have the `TodoWrite` tool available, you may use it to track
  Steps 1-7 above as a checklist while you work through them. This is a display convenience --
  it changes nothing about what the steps do or what the report contains.
