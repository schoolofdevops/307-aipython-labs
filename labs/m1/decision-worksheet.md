# M1 — Technology Selection Worksheet

Fill one row per scenario from `scenarios.md`. Scenario 1 is completed for you as a worked
example — study it before filling the rest.

Rules:

- **Tool class** — pick from the list at the top of `scenarios.md`. Name a concrete tool if
  it helps (e.g. "Existing tool + config — Terraform"), but the class is what matters.
- **Why this tool** — one or two sentences. Say why the *smaller* classes below it are not
  enough, or why a bigger one is not needed.
- **Delegate to an agent?** — which part of the build you would hand to a coding agent, and
  which part you would write or review yourself.
- Do not use the `|` character inside a cell — it breaks the table (and the checker).

When done, run `bash check.sh` in this directory. It verifies every row is filled and
prints a PASS/FAIL summary. It checks completeness, not correctness — compare your reasoning
against `solutions.md` afterwards.

| # | Requirement (short) | Tool class | Why this tool | Delegate to an agent? |
|---|---------------------|------------|---------------|-----------------------|
| 1 | Weekly stale-AMI report across accounts | Python script (scheduled) | Calls AWS APIs across three accounts, filters by age, groups by tag and formats for Slack — too much structured data for Bash, and there is no infrastructure state to declare, so IaC does not apply. Runs on a schedule, so it becomes a scheduled job. | Delegate the AWS API calls and Slack formatting to the agent. I write the spec (accounts, age threshold, grouping) and review the account/region loops and the tag handling myself. |
| 2 | One-off 502 count from an ALB log bundle | | | |
| 3 | Service inventory over HTTP | | | |
| 4 | Block Pods without resource limits at admission | | | |
| 5 | Nightly cleanup of temp-tagged buckets with dry-run | | | |
| 6 | Identical VPC layouts in new accounts | | | |
| 7 | Daily security-group drift detection | | | |
| 8 | Automatic incident context collection | | | |
