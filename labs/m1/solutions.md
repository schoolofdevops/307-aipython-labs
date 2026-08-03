# M1 — Technology Selection: Reference Answers

These are the instructor's answers with reasoning. Your tool class does not have to match
word-for-word — what matters is that your *reasoning* survives contact with the questions
below each answer. Where a reasonable alternative exists, it is named.

This file uses the same table format as the worksheet, so you can also run
`bash check.sh solutions.md` to see what a fully complete worksheet looks like to the checker.

| # | Requirement (short) | Tool class | Why this tool | Delegate to an agent? |
|---|---------------------|------------|---------------|-----------------------|
| 1 | Weekly stale-AMI report across accounts | Python script (scheduled) | Calls AWS APIs across three accounts, filters by age, groups by tag and formats for Slack — too much structured data for Bash, and there is no infrastructure state to declare, so IaC does not apply. Runs on a schedule, so it becomes a scheduled job. | Delegate the AWS API calls and Slack formatting. I write the spec (accounts, age threshold, grouping) and review the account/region loops and tag handling myself. |
| 2 | One-off 502 count from an ALB log bundle | Bash one-liner | Text in, count out, run once. grep, awk and sort answer this in minutes. Writing Python here is effort spent on code you will delete tonight. | Nothing worth delegating — describing the task to an agent takes longer than typing the pipeline. If the log format fights back, ask the agent for the awk expression and sanity-check it on ten lines first. |
| 3 | Service inventory over HTTP | Python service (API) | Multiple consumers query it live over HTTP from their own tooling, so it must stay running and expose a stable contract — a script or CLI cannot serve other systems on demand. Python fits because the job is aggregation across several sources. | Delegate endpoint boilerplate and response serialization. I design the API contract and the data model, and review anything touching auth. |
| 4 | Block Pods without resource limits at admission | Existing tool + config (policy engine) | Admission enforcement is a solved problem — Kyverno, Gatekeeper or ValidatingAdmissionPolicy do this with a few lines of policy config, including namespace exceptions. Writing a custom admission webhook in Python means owning a TLS-serving, high-availability, cluster-critical service for a rule someone already productized. | Delegate a first draft of the policy resource and its test cases. I review the match rules and the exception list — a wrong selector here blocks production deploys. |
| 5 | Nightly cleanup of temp-tagged buckets with dry-run | Python CLI as a scheduled job | Deletion with tag filters, an age rule, a removal log and dry-run needs real argument handling, structured logging and tests — beyond safe Bash. A CLI (not a bare script) because dry-run/apply flags and exit codes are the interface, run nightly by a scheduler. | Delegate the S3 listing and tag-filter code. I write the dry-run gate and the deletion path myself — destructive code is exactly what I review line-by-line and cover with tests before it ever runs unattended. |
| 6 | Identical VPC layouts in new accounts | Existing tool + config (Terraform or Pulumi) | This is desired-state infrastructure, repeated across accounts, needing review before creation — the definition of declarative IaC with a plan step. A boto3 script would have to reinvent state tracking, diffing and idempotency that Terraform already guarantees. | Delegate a first-pass module from an existing account's layout. I review the plan output before every apply — that review is the control point, not the code. |
| 7 | Daily security-group drift detection | Existing tool + config, wrapped by a scheduled script | Terraform already computes drift: a scheduled `terraform plan -detailed-exitcode` detects divergence from code. The only new code is a thin wrapper that reads the exit code and routes an alert to the owning team — a small script, not a platform. | Delegate the wrapper (parse plan output, format the alert). I decide the schedule, the alert routing and what counts as noise — alert fatigue is an operational judgment, not a coding task. |
| 8 | Automatic incident context collection | Python CLI, later exposed as an agent tool | Aggregates deploys, Kubernetes events, alerts and ownership into one report — structured API work across systems, exactly Python's ground. Built as a CLI so responders and CI can run it today; the same function is later registered as a read-only agent tool so an ops agent can request context without shell access. This is PlatformOps `incident collect` — you build it in Module 31 and expose it to agents in Module 35. | Delegate individual collectors (deploy history, event listing) one at a time. I own the report structure, the read-only guarantee, and the tool contract the agent sees. |

---

## Check your reasoning, not just your answer

- **Scenario 2** — if you chose Python script: would you still, knowing you delete it
  tonight? Python is not wrong here, just more than the job needs. The habit to build is
  *smallest tool that solves it well*.
- **Scenario 4** — if you chose controller/operator: you identified the right *mechanism*
  (admission control) but missed that it is already productized. Building beats configuring
  only when no existing tool expresses your rule.
- **Scenario 5** — a plain Python script on cron is defensible. The CLI class wins once you
  need dry-run/apply flags and meaningful exit codes — that is an interface, and interfaces
  deserve CLI treatment.
- **Scenario 7** — if you reached for a boto3 diff engine: you would be re-implementing
  `terraform plan`. When the source of truth is IaC, ask the IaC to detect drift.
- **Scenario 8** — if you stopped at "script": who runs it during an incident, with what
  arguments, and how does an agent call it later? Interfaces (CLI now, agent tool later) are
  what make automation usable under pressure.
