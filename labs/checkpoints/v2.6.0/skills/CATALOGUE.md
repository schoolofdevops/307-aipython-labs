# PlatformOps Skill Catalogue

This is a planning document. It lists skills the PlatformOps Toolkit could
use -- not skills that exist yet. Module 17 starts turning these into real
`SKILL.md` files, one at a time. Module 20 adds a governed, versioned status
to each entry once it ships, so a skill's readiness is checkable at a
glance instead of read out of git history.

Each entry passed the two boundary checks from Module 16 before it was
added here: it is not a single line that already belongs in CLAUDE.md, and
it is not a Python implementation that belongs in `src/platformops/`
instead.

## Shipped skills

| Name | Version | Shipped in | Script it runs |
|------|---------|------------|-----------------|
| `service-readiness-review` | 1.3.0 | v1.2.1 (Module 17), script-backed in v1.3.0 (Module 18) | `scripts/review-service.py` |
| `platformops-service-readiness` | 1.4.0 | v1.4.0 (Module 19) | `scripts/service_readiness.py` |
| `incident-context-collector` | 1.5.0 | v1.5.0 (Module 20) | `scripts/incident_context.py` |

Every shipped skill's own frontmatter carries the same `version` field --
this table exists for a quick overview, the `SKILL.md` file itself is the
source of truth. `scripts/skill_lint.py <path>` runs the mechanical
governance checks (frontmatter ratio, trigger phrase, step count, required
headings, agent-neutral core, read-only enforcement) that every shipped
skill above already passes.

## Candidate skills

| Name | Description | Trigger condition |
|------|-------------|--------------------|
| `changelog-entry` | Write a CHANGELOG.md entry in this project's exact Keep a Changelog format. | Right after finishing any code change, before reporting the task done. |
| `spec-writer` | Draft the seven-section spec (Goal, Functional requirements, Allowed files, Approved dependencies, Non-goals, Required tests, Acceptance criteria) for a described feature. | Before delegating a new feature to a coding agent, following the Module 14 spec shape. |
| `dependency-audit-report` | Turn a `check-deps --format json` result into a short, human-readable summary suitable for a pull-request comment. | When `check-deps` reports findings and a plain-language summary is needed for reviewers. |

## Boundary notes

- None of these entries duplicate an existing CLAUDE.md rule -- each is a
  multi-step procedure for one recurring situation, not a standing rule
  every task needs.
- None of these entries are a Python implementation in disguise -- each
  one's actual work (running `check-deps`, running `git log`, running the
  existing validation code) already exists as a command or a library
  function. The skill sequences and interprets that work; it does not
  reimplement it.
