# Diff Review — multi-region scanner (Phase 1 → Phase 2)

Review the diff in `labs/m23/rigged-diff.patch`. It extends Phase 1's sequential,
EC2+EBS-only `multiregion.py` into a concurrent, five-resource-type scanner. The diff
contains **two** seeded flaws, the same two bug classes this module's objectives name:

- **unbounded-concurrency** — the thread pool has no real cap, or accepts a cap it never
  actually enforces
- **pagination-bug** — a `NextToken`/paginator loop that used to walk every page got
  replaced with a single, unpaginated call, silently dropping resources past the first page

## Findings

| # | Function | Category | What is wrong | Fix instruction |
|---|----------|----------|----------------|------------------|
| 1 |          |          |                |                  |
| 2 |          |          |                |                  |
