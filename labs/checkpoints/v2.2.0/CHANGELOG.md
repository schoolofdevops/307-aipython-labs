# Changelog

All notable changes to the PlatformOps Toolkit are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/). Dates are omitted below -- each entry corresponds to
one course module's tagged release, not a calendar date.

## [Unreleased]

## [2.2.0] - Containerized PlatformOps
### Added
- `Dockerfile` -- a two-stage build. Stage 1 (`builder`, `python:3.12-slim`) installs `uv`,
  resolves `uv.lock` with `--frozen` (fails loudly instead of silently re-resolving a stale lock)
  and installs the project non-editable (`--no-dev --no-editable`) into a throwaway `.venv`. Stage
  2 (`runtime`, a fresh `python:3.12-slim`) copies in only that `.venv` -- no `uv`, no build tools,
  no `uv.lock`, no test suite cross into the shipped image -- and runs as a dedicated `platformops`
  user (uid/gid 1000, no home directory, `nologin` shell), never root. A `HEALTHCHECK` runs
  `platformops local-status --path . --json` -- the one command in this project already guaranteed
  to exit 0 and report a structured result even with no `.git` directory and no Docker socket
  inside the container (see `local_ops.py`'s never-raises contract, M11), instead of inventing a
  container-only health endpoint that the CLI itself never exercises.
- `.dockerignore` -- excludes `.venv`, caches, `.git`, tests, course/dev-tooling files
  (`.claude`, `AGENTS.md`, `CLAUDE.md`) and the `k8s/` manifests (applied with `kubectl` from
  outside the image, never baked in) from the build context. `README.md` stays in the context on
  purpose -- `pyproject.toml`'s `readme` field points at it, and the builder's
  `uv sync --no-editable` step fails without it.
- `k8s/configmap.yaml`, `k8s/secret.yaml`, `k8s/job.yaml` -- run `platformops` as a Kubernetes
  `Job` (run-to-completion, not a long-lived `Deployment`) against a `kind` cluster. The Job
  overrides the image's `ENTRYPOINT` to run `python -m platformops.config /config/service.yaml
  --check --json`, mounting the service definition from a `ConfigMap` (non-secret: a repo URL, an
  environment name) and reading `PLATFORMOPS_ENVIRONMENT` from a `Secret` via `envFrom` -- the same
  environment-override wiring `config.py` (M11) already had, now exercised from a real Secret
  instead of a shell export. The pod runs non-root (`runAsUser: 1000`), with
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, all Linux capabilities
  dropped, `automountServiceAccountToken: false` (this Job never calls the Kubernetes API -- that
  is M28's job, not this one), and explicit `resources.requests`/`resources.limits` so one bad
  container cannot starve every other pod on the node.
- SBOM and vulnerability scanning added to this project's release process, using `syft` (SBOM,
  SPDX JSON) and `grype` (vulnerability scan) against the built image -- both newly required lab
  tools, added to `course.config.json`'s `lab.tools`. `docker scout` was evaluated and rejected:
  this course's reference Docker CLI build does not ship the `scout` subcommand, so a lab built
  around it would not run on the documented environment.

### Notes
- The built image is small (multi-stage keeps `uv`, the C toolchain, and every build-time file out
  of the final layer) but not vulnerability-free: `grype` finds real, current CVEs in the
  `python:3.12-slim` base layer itself (the `python` interpreter binary, `libc6`, `perl-base`,
  `ncurses`, `libsqlite3-0`) -- none in `platformops`'s own code or its four pinned dependencies.
  The lab folds in the real, point-in-time scan output and treats it honestly: a scan finding real
  base-image CVEs is the scanner doing its job, not a defect in this release; the mitigation is
  rebuilding on a fresh base regularly, not chasing zero.

## [2.1.0] - Release Readiness Checker
### Added
- `src/platformops/releasecheck.py` -- combine a repository's own GitHub state (pull request,
  workflow, check runs, build artifact, latest release, deployment record) into one
  release-readiness verdict. This is a read-only inspector, the same discipline `cloudaudit.py`
  (M24) applies to AWS resources applied here to CI/CD state: it never merges a pull request,
  never re-runs a check, never triggers a build and never creates a deployment. Six
  `gather_*_evidence()` functions are the only functions in this module that call into
  `httpclient.py` -- each one catches its own network failures and hands back raw facts (or
  `"fetched": False`), never a verdict. `evaluate_release_readiness()` is the pure half: it takes
  six already-gathered evidence dicts and returns a `ReleaseReadinessReport`, with zero `httpx`
  import and zero calls to any `gather_*` function -- fully unit-testable with hand-built
  evidence, no fixtures or network mocking required. A source that could not be fetched degrades
  to an honest `UNKNOWN` for that section and a name in `sources_failed`; an `UNKNOWN` in any
  gating section keeps the verdict at `not_ready`, the same as an outright `FAIL` -- a fetch
  failure never becomes a silent pass.
- Five new GitHub REST endpoints in `src/platformops/httpclient.py`, following the exact
  retry/pagination/auth/pydantic-validation pattern `get_repo_info()` and `list_workflow_runs()`
  already established: `get_pull_request()`, `list_check_runs()`, `list_artifacts()`,
  `get_latest_release()` (a 404 -- no release yet -- is a real, expected answer, not a failure),
  and `list_deployments()` (the one list endpoint GitHub returns as a bare JSON array, not an
  object with a named key).
- `release-check` CLI command -- `platformops release-check payments --owner <org> --pr <n>`,
  with `--repo`, `--branch`, `--environment`, `--artifact-name` and `--json`. Exit 0 only when
  every gating section (`pr`, `ci`, `checks`, `artifacts`, `deployment`) is `PASS`; `release` is
  informational and never gates the verdict on its own.
- `tests/fixtures/github/` -- deterministic JSON fixture bodies (a merged PR and a blocked one, a
  succeeding and a failing workflow run, passing/pending/failing check runs, a present and a
  missing artifact, a latest release, and a deployment record with and without image metadata),
  used by `respx`-mocked tests for every new endpoint and for `release-check`'s lab demo. No
  GitHub token, no real network call, no rate limit, anywhere in this module's tests or lab.

## [2.0.0] - Governed Cloud Remediation
### Added
- `src/platformops/cloudremediate.py` -- fix findings `cloudaudit.py` (M24) reports, for real,
  against a real AWS-compatible endpoint. This is the first version of this project that can
  change cloud infrastructure state, not just read and report on it -- the reason for the major
  version bump. `build_remediation_plan()` reads live resource state and returns a `RemediationPlan`
  (`remediable`/`already_fixed`/`not_supported`), never writing. `execute_remediation_plan()`
  refuses to mutate anything without `approve=True` (raises `RemediationNotApprovedError`
  otherwise), re-checks the finding against live state immediately before writing (the
  idempotency guarantee -- a second run on an already-fixed finding reports `already_fixed`,
  never re-applies), and appends a structured JSON-lines audit-log record (timestamp, finding,
  resource, rule, action, before/after state, approver) for every executed remediation -- the
  rollback record. `execute_remediation_batch()` remediates more than one finding per call,
  refusing outright (never silently truncating) a batch over `DEFAULT_BATCH_CAP` (5) without an
  explicit, higher cap.
- A **remediation allowlist**, loaded from `remediation.example.yaml` -- a second safety gate,
  independent of `--approve`. Only `required-tags`, `require-encryption` and `no-public-exposure`
  have remediation code (`PLANNERS`) at all; `approved-regions` and `max-age-days` always come
  back `not_supported`, by design -- both require a human decision (a bucket cannot change region
  without a manual recreate-and-migrate; "old" is not the same as "safe to delete").
- Three new CLI commands: `remediate-plan` (always a dry run), `remediate-execute` (mutates only
  with `--approve`), `remediate-execute-batch` (bounded batch remediation with `--max-batch`).
- `remediation.example.yaml` -- the allowlist and per-rule remediation policy (default tag
  values, the KMS key remediated buckets are switched to).

## [1.9.0] - Cloud Hygiene Auditor
### Added
- `src/platformops/cloudaudit.py` -- check real AWS resources against a policy written in YAML
  (`policy.yaml`: required tags, approved regions, max age, encryption, public exposure) and
  report violations. Detection only, never remediation -- no `put_*`/`delete_*`/`create_*`/
  `modify_*` call against an audited resource anywhere in this module. `evaluate_policy()` is
  pure (zero network calls, fully unit-testable without Floci); `gather_bucket_evidence()` is
  the only function that talks to AWS, composing `ResourceEvidence` on top of
  `multiregion.ResourceRecord` rather than extending that shared dataclass. `require-encryption`
  checks for a customer-managed KMS key specifically, not "any encryption" -- real AWS (and
  Floci) already turns on SSE-S3 default encryption for every new bucket. Exceptions in the
  policy suppress a finding for one `(resource_id, rule_id)` pair without dropping it from the
  report; an exception past its `expires` date stops suppressing automatically.
- `cloud-audit`, `findings-list`, `findings-show` CLI commands -- run a policy audit and write a
  JSON report (`--output`), then list or show individual findings from it, with
  `--include-suppressed` and `--severity` filters on `findings-list`.
- `policy.example.yaml` -- a runnable example policy with five rules and one exception.

## [1.8.0] - Multi-Region Cloud Inventory
### Added
- `src/platformops/multiregion.py` -- scan EC2 instances, EBS volumes, security groups,
  Elastic IPs and S3 buckets across many regions at once. `scan_regions()` fans out across
  regions with a bounded `ThreadPoolExecutor` (`max_workers`, default 5) -- never one
  thread per region -- and every region's scan runs inside its own try/except, so a
  region that fails (a `ClientError`, an expired security token, missing credentials)
  lands in a `failed_regions` list instead of aborting the other regions' results. S3 is
  discovered once, account-wide (`list_s3_buckets()`), separate from the per-region pool,
  because `list_buckets()` is not region-scoped. Every resource type normalizes into one
  shared `ResourceRecord` shape; `to_markdown()` and `to_csv()` both render from that same
  normalized list.
- `multi-region-scan` CLI command -- `--regions`, `--profile`, `--max-workers`,
  `--format markdown|csv`, `--json`.

## [1.7.0] - Local AWS Automation Environment
### Added
- `src/platformops/awsclient.py` -- `get_aws_client()`, the one function every AWS-backed module
  in this project now builds its boto3 client through. Generalizes `cloudinventory.get_client()`
  (EC2-only) to any service name, and adds an `endpoint_url` argument: left unset, a client
  behaves exactly like talking to real AWS; set explicitly, or picked up from the `AWS_ENDPOINT_URL`
  environment variable as a fallback, every call the client makes is redirected there instead --
  used to point boto3 at a local [Floci](https://floci.io) instance for this module's lab. An
  explicit `endpoint_url=` argument always wins over the environment variable.
- `src/platformops/reportstore.py` -- S3 report storage. `ensure_bucket()` (idempotent create),
  `upload_report()` (timestamped key, collision-free), `download_report()`, `list_reports()`
  (paginated, most-recent-first), and `upload_report_to_bucket()`, the CLI-facing wrapper that
  catches `NoCredentialsError`/`ClientError` the same way `cloudinventory.scan_inventory()` does.
- `src/platformops/findingsstore.py` -- DynamoDB findings store, keyed on `service` (partition)
  + `timestamp` (sort) so "every finding for one service, most recent first" is a single query.
  `ensure_table()`, `put_finding()`, `get_finding()`, `query_findings()`.
- `src/platformops/workqueue.py` -- SQS queue automation with a dead-letter-queue pattern.
  `ensure_queue()` / `ensure_dead_letter_queue()` wire a main queue's `RedrivePolicy` at a DLQ
  with a `maxReceiveCount`; `send_message()` / `receive_messages()` / `delete_message()` are the
  basic round trip; `send_to_queue()` / `receive_from_queue()` are the CLI-facing wrappers.
  `VisibilityTimeout` and `ReceiveMessageWaitTimeSeconds` (long polling) are real, tested
  arguments, not just defined terms.
- `platformops report-upload`, `platformops queue-send` and `platformops queue-receive` -- new
  CLI commands, all accepting `--endpoint-url` (falls back to `AWS_ENDPOINT_URL`) alongside
  `--region`/`--profile`, following the same option and exit-code conventions every other command
  in this project uses.
- `tests/conftest.py` -- a shared `require_floci` fixture that skips `test_reportstore.py`,
  `test_findingsstore.py` and `test_workqueue.py` cleanly (not a hard failure) if a local Floci
  container is not reachable at `http://localhost:4566`.
- 41 new tests: `test_awsclient.py` (6, unit, mocked `boto3.Session`), `test_reportstore.py` (10),
  `test_findingsstore.py` (6) and `test_workqueue.py` (9) -- the latter three run against a real,
  running Floci container, not a mock, including a live dead-letter-queue redrive -- plus 10 new
  CLI-wiring tests in `test_cli.py`. 239 tests total, up from 198 at v1.6.0.

## [1.6.0] - AWS Resource Inventory
### Added
- `src/platformops/cloudinventory.py` -- this project's first cloud-provider integration:
  `get_client()` builds a `boto3` EC2 client through the normal credential chain (explicit
  session profile, then environment variables, then the shared config/credentials files, then an
  IAM role -- never a hardcoded key), `list_instances()` lists EC2 instances through
  `client.get_paginator("describe_instances")` (never a single unpaginated call) with server-side
  tag filtering via the `Filters` parameter, and `scan_inventory()` combines both into one
  JSON-safe report, catching `botocore.exceptions.NoCredentialsError` and
  `botocore.exceptions.ClientError` by name instead of a bare `except Exception`. Uses `client`,
  not `resource` -- the same explicit, low-level style `httpclient.py` already uses for HTTP.
  Sets an explicit retry config (`Config(retries={"max_attempts": 5, "mode": "standard"})`).
- `platformops inventory-scan` -- a new CLI command wiring `scan_inventory()` up with
  `--profile`, `--region` (required, no default), `--tag-key`/`--tag-value`, and `--json`,
  following the same option and exit-code conventions every other command in this project uses.
- `tests/test_cloudinventory.py` -- 11 tests against real `moto` EC2 fixtures (`@mock_aws`):
  session/credential wiring, listing with region/tags/launch time, pagination proven by forcing a
  small page size across 5 instances, server-side tag filtering (key+value and key-only), a real
  `ClientError` for an unknown instance ID, and a real `NoCredentialsError` -- captured outside
  `@mock_aws`, since moto fakes valid credentials the moment it is active. Plus 5 CLI-layer tests
  in `tests/test_cli.py`, monkeypatching `scan_inventory()` to test option parsing and exit codes
  without touching AWS (real or mocked).
- `boto3` (runtime) and `moto[ec2]` (dev) dependencies, plus a `[[tool.mypy.overrides]]` entry
  ignoring missing type stubs for `boto3`/`botocore.*` (neither ships stubs).
### Changed
- `course.config.json`'s M21 objectives -- dropped "waiters" (not implemented; out of scope for
  a read-only inventory scanner) and filled in the Deep Dive intent.

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
