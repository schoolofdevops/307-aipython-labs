# PlatformOps Skill Catalogue

This is a planning document. It lists skills the PlatformOps Toolkit could
use -- not skills that exist yet. Module 17 starts turning these into real
`SKILL.md` files, one at a time.

Each entry passed the two boundary checks from Module 16 before it was
added here: it is not a single line that already belongs in CLAUDE.md, and
it is not a Python implementation that belongs in `src/platformops/`
instead.

## Candidate skills

| Name | Description | Trigger condition |
|------|-------------|--------------------|
| `changelog-entry` | Write a CHANGELOG.md entry in this project's exact Keep a Changelog format. | Right after finishing any code change, before reporting the task done. |
| `service-readiness-review` | Walk a service definition file (`service.yaml`) through the project's validation and readiness checks before it ships. | Before deploying, or merging a change to, a `service*.yaml` file. |
| `spec-writer` | Draft the seven-section spec (Goal, Functional requirements, Allowed files, Approved dependencies, Non-goals, Required tests, Acceptance criteria) for a described feature. | Before delegating a new feature to a coding agent, following the Module 14 spec shape. |
| `dependency-audit-report` | Turn a `check-deps --format json` result into a short, human-readable summary suitable for a pull-request comment. | When `check-deps` reports findings and a plain-language summary is needed for reviewers. |
| `incident-context-collector` | Gather the project's first-response context -- recent git history, container status, and the last failed health check -- into one short report. | When responding to a production incident and the first few minutes are spent gathering context by hand. |

## Boundary notes

- None of these five duplicate an existing CLAUDE.md rule -- each is a
  multi-step procedure for one recurring situation, not a standing rule
  every task needs.
- None of these five are a Python implementation in disguise -- each one's
  actual work (running `check-deps`, running `git log`, running the
  existing validation code) already exists as a command or a library
  function. The skill sequences and interprets that work; it does not
  reimplement it.
