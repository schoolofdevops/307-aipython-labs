# M1 — Eight Requirements from a Real Platform Team

Read each requirement as if it landed in your team's backlog this sprint. For each one, pick
the **smallest tool class that solves it well** and write your reasoning in
`decision-worksheet.md`. There is no trick here: some of these should NOT be Python.

Tool classes to choose from:

- **Bash one-liner / Bash script**: glue and one-off text processing, wrapping existing CLIs
- **Python script**: structured data, API calls, more than about 30 lines of logic
- **Python CLI**: a script other people run, with flags, help text and exit codes
- **Python service (API)**: other systems call it over HTTP, and it stays running
- **Scheduled job (worker)**: a script or CLI run on a schedule (cron, CI, or a Kubernetes CronJob)
- **Controller / operator**: watches the current state all the time and fixes it
- **Existing tool + config**: Terraform, Ansible, a policy engine — you write configuration, not code
- **Agent tool**: a capability exposed to a coding or ops agent, with a human approving actions

---

## Scenario 1 — Weekly stale-AMI report

Every Monday, security wants a report of AMIs older than 90 days across your three AWS
accounts, grouped by team tag, posted to a Slack channel. The list changes weekly; nobody
should run this by hand.

## Scenario 2 — One-off 502 count from a log bundle

During an incident review you are handed a 4 GB folder of ALB (load balancer) access logs and
asked one question: how many 502 responses hit the checkout listener yesterday, per hour? You
need the answer this afternoon, and you will most likely never run this again.

## Scenario 3 — Service inventory over HTTP

Three teams (FinOps, security, and the internal developer portal) want to ask "which
services exist, who owns them, and which account and namespace are they in", live, over
HTTP, from their own tools. The data comes from several sources and changes every day.

## Scenario 4 — Block Pods without resource limits

Platform policy: no Pod may be admitted to any production cluster without CPU and memory
limits. This must be enforced at admission time, cluster-wide, with exceptions for two
system namespaces.

## Scenario 5 — Nightly cleanup of temporary buckets

Developers create S3 buckets tagged `temp=true` for test data. Anything tagged `temp=true`
and older than 7 days must be emptied and deleted every night, with a log of what was
removed. Deletion mistakes here are expensive, so the run must support a dry-run mode.

## Scenario 6 — Identical network layouts in new accounts

Every new AWS account needs the same VPC, subnet, routing and NAT layout. You will do this
for six accounts this quarter and more next year. The layout itself changes rarely, but it
must be reviewable before anything is created.

## Scenario 7 — Security-group drift detection

Your VPCs and security groups are managed in Terraform, but engineers sometimes "fix"
security groups by hand during incidents. You need to detect, every day, when the live
security groups differ from what the code says they should be, and alert the team that owns
them. Detection only, no automatic repair.

## Scenario 8 — Incident context in one place

Every time the payments service pages, the responder spends the first 15 minutes collecting
the same facts: recent deploys, Kubernetes events, current alerts, and who owns what. You
want that collected automatically into one report the moment an incident starts. And
eventually, you want an ops agent to be able to request it too.
