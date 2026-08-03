# M1 — Technology Selection: Reference Answers

These are the instructor's answers, with the reasoning behind them. Your tool class does not
have to match word-for-word. What matters is that your *reasoning* still holds up against
the questions below each answer. Where a reasonable alternative exists, it is named too.

This file uses the same table format as the worksheet, so you can also run
`bash check.sh solutions.md` to see what a fully filled-in worksheet looks like to the
checker.

| # | Requirement (short) | Tool class | Why this tool | Delegate to an agent? |
|---|---------------------|------------|---------------|-----------------------|
| 1 | Weekly stale-AMI report across accounts | Python script (scheduled) | Calls AWS APIs across three accounts, filters by age, groups by tag and formats for Slack. That is too much structured data for Bash, and there is no infrastructure state to declare, so IaC does not apply. It runs on a schedule, so it becomes a scheduled job. | Delegate the AWS API calls and Slack formatting. I write the spec (accounts, age limit, grouping) and check the account and region loops and tag handling myself. |
| 2 | One-off 502 count from an ALB log bundle | Bash one-liner | Text goes in, a count comes out, and you run it once. grep, awk and sort answer this in minutes. Writing Python here spends effort on code you will delete tonight. | Nothing worth handing off. Explaining the task to an agent takes longer than typing the pipeline yourself. If the log format gets messy, ask the agent for the awk expression and check it on ten lines first. |
| 3 | Service inventory over HTTP | Python service (API) | Several teams query it live over HTTP from their own tools, so it must stay running and offer a stable contract. A script or CLI cannot serve other systems on demand. Python fits because the job is pulling data together from several sources. | Delegate the endpoint boilerplate and response formatting. I design the API contract and the data model, and I check anything that touches login or authentication myself. |
| 4 | Block Pods without resource limits at admission | Existing tool + config (policy engine) | Admission enforcement is already a solved problem. Kyverno, Gatekeeper or ValidatingAdmissionPolicy can do this with a few lines of policy configuration, including namespace exceptions. Writing a custom admission webhook in Python means owning a high-availability, cluster-critical service, just for a rule someone already built as a product. | Delegate a first draft of the policy resource and its test cases. I check the match rules and the exception list myself. A wrong selector here blocks production deploys. |
| 5 | Nightly cleanup of temp-tagged buckets with dry-run | Python CLI as a scheduled job | Deletion with tag filters, an age rule, a removal log and a dry-run mode needs real argument handling, structured logging and tests. That is beyond what is safe in Bash. It is a CLI, not a plain script, because dry-run and apply flags plus exit codes are the interface, run nightly by a scheduler. | Delegate the S3 listing and tag-filter code. I write the dry-run gate and the deletion path myself. Destructive code is exactly what I check line by line and cover with tests before it ever runs unattended. |
| 6 | Identical VPC layouts in new accounts | Existing tool + config (Terraform or Pulumi) | This is desired-state infrastructure, repeated across accounts, and it needs review before anything is created. That is the definition of declarative IaC with a plan step. A boto3 script would have to rebuild the state tracking, diffing and safe re-runs that Terraform already guarantees. | Delegate a first-pass module based on an existing account's layout. I review the plan output before every apply. That review is the real control point, not the code. |
| 7 | Daily security-group drift detection | Existing tool + config, wrapped by a scheduled script | Terraform already works out drift: a scheduled `terraform plan -detailed-exitcode` finds where the live setup no longer matches the code. The only new code is a thin wrapper that reads the exit code and sends an alert to the owning team. It is a small script, not a platform. | Delegate the wrapper (read the plan output, format the alert). I decide the schedule, the alert routing, and what counts as noise. Alert fatigue is a judgment call, not a coding task. |
| 8 | Automatic incident context collection | Python CLI, later exposed as an agent tool | Pulls deploys, Kubernetes events, alerts and ownership together into one report. This is structured API work across systems, exactly where Python fits best. Built as a CLI so responders and CI can run it today. The same function is later registered as a read-only agent tool, so an ops agent can ask for context without shell access. This is PlatformOps `incident collect`. You build it in Module 31 and open it up to agents in Module 35. | Delegate the individual collectors (deploy history, event listing) one at a time. I own the report structure, the read-only guarantee, and the tool contract the agent sees. |

---

## Check your reasoning, not just your answer

- **Scenario 2**: if you chose Python script, would you still choose it, knowing you delete
  it tonight? Python is not wrong here, just more than the job needs. The habit to build is
  *pick the smallest tool that solves it well*.
- **Scenario 4**: if you chose controller/operator, you found the right *mechanism*
  (admission control), but you missed that it is already sold as a product. Building beats
  configuring only when no existing tool already does what your rule needs.
- **Scenario 5**: a plain Python script on cron is a defensible answer. The CLI class wins
  once you need dry-run and apply flags with real exit codes. That is an interface, and
  interfaces deserve CLI treatment.
- **Scenario 7**: if you reached for a boto3 tool that compares state, you would be
  rebuilding `terraform plan`. When IaC is the source of truth, let the IaC detect the
  drift.
- **Scenario 8**: if you stopped at "script", ask yourself: who runs it during an incident,
  with what arguments, and how does an agent call it later? Interfaces (CLI now, agent tool
  later) are what make automation usable under pressure.
