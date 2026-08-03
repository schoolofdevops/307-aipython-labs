# M1 — Technology Selection Worksheet

Fill in one row per scenario from `scenarios.md`. Scenario 1 is already done for you as a
worked example. Study it before you fill in the rest.

Rules:

- **Tool class**: pick from the list at the top of `scenarios.md`. Name a real tool if it
  helps (for example, "Existing tool + config — Terraform"), but the class is what matters.
- **Why this tool**: one or two sentences. Say why the *smaller* classes below it are not
  enough, or why a bigger one is not needed.
- **Delegate to an agent?**: which part of the build you would hand to a coding agent, and
  which part you would write or check yourself.
- Do not use the `|` character inside a cell. It breaks the table, and the checker too.

When you are done, run `bash check.sh` in this folder. It checks that every row is filled in
and prints a PASS/FAIL summary. It checks completeness, not correctness. Compare your
reasoning against `solutions.md` after that.

| # | Requirement (short) | Tool class | Why this tool | Delegate to an agent? |
|---|---------------------|------------|---------------|-----------------------|
| 1 | Weekly stale-AMI report across accounts | Python script (scheduled) | Calls AWS APIs across three accounts, filters by age, groups by tag and formats for Slack. That is too much structured data for Bash, and there is no infrastructure state to declare, so IaC does not apply. It runs on a schedule, so it becomes a scheduled job. | Delegate the AWS API calls and Slack formatting to the agent. I write the spec (accounts, age limit, grouping) and check the account and region loops and the tag handling myself. |
| 2 | One-off 502 count from an ALB log bundle | | | |
| 3 | Service inventory over HTTP | | | |
| 4 | Block Pods without resource limits at admission | | | |
| 5 | Nightly cleanup of temp-tagged buckets with dry-run | | | |
| 6 | Identical VPC layouts in new accounts | | | |
| 7 | Daily security-group drift detection | | | |
| 8 | Automatic incident context collection | | | |
