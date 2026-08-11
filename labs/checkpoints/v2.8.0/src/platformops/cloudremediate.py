"""platformops.cloudremediate -- fix real cloud resources against a written policy, deliberately.

Module 24's `cloudaudit.py` is the inspector: it finds and reports, and
never once calls a write-mode AWS API against the resource it is checking.
This module is the maintenance crew that reads its findings as a work
order. It is the first module in this project where a write call to a
cloud resource (`put_bucket_tagging`, `put_bucket_encryption`,
`put_bucket_acl`) is legitimate and expected -- and that is a real shift in
discipline, not a relaxation of it. Every mutation here goes through the
same sequence, no exceptions: build a plan (read-only), show the plan to a
human, refuse to touch anything without an explicit `--approve`, re-check
that the finding still applies right before mutating, record exactly what
changed, and verify the change actually took effect.

Not every finding this project can detect is safe to fix automatically.
`approved-regions` (an S3 bucket in the wrong region) has no automatic fix
here -- moving a bucket's region means recreating it and migrating its
data, which is too consequential to do without a human choosing to do it.
`max-age-days` (a resource that is simply old) has no automatic fix either
-- "delete it because it is old" is a judgment call, not something a
policy file should decide alone. Both come back `not_supported`, always,
by design.

`build_remediation_plan()` and `execute_remediation_plan()` are kept apart
the same way `gather_bucket_evidence()` and `evaluate_policy()` are kept
apart in Module 24: one reads live state and decides what a fix would look
like; the other, only when explicitly approved, actually calls the write
API. `build_remediation_plan()` may read current resource state (to know
whether a finding still applies) but it never writes.

Two independent safety gates stand between a finding and a mutation.
`--approve` is one -- it must be present, or `execute_remediation_plan()`
raises rather than silently doing nothing. The **remediation allowlist**
(loaded from `remediation.example.yaml`'s `allowlist` list) is the other,
and it is checked first: a rule_id absent from the allowlist is always
`not_supported`, even when a planner function exists that could remediate
it, and even when `--approve` is passed. Removing a rule from the
allowlist is enough to stop this module from ever touching that kind of
finding again -- no code change required.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import botocore.exceptions

from platformops import cloudaudit
from platformops.config import load_yaml_dict

DEFAULT_BATCH_CAP = 5
"""The hard default cap on findings remediated in one `remediate-execute-batch` call.

Small on purpose. A batch remediation with no cap at all can touch far
more of an account than anyone reviewed -- the same "bound the blast
radius" idea a canary deploy or a rolling restart already applies to
changing many things at once. Going over this cap requires an explicit,
higher `--max-batch` on the command line; the CLI refuses an over-cap
batch outright, it never silently truncates it down to the cap and runs
anyway.
"""


class FindingNotFoundError(Exception):
    """Raised when a finding_id does not match anything in a report."""


class RemediationNotApprovedError(Exception):
    """Raised by `execute_remediation_plan()` when a remediable plan is run without `approve=True`.

    Carries the plan itself (`.plan`) so a caller -- the CLI command -- can
    print the exact plan the operator was about to reject, instead of just
    a bare error string. This is deliberate: there is no code path in this
    module where an execute call without approval quietly does nothing and
    reports success. It always raises, and the CLI always shows the plan
    again and exits non-zero.
    """

    def __init__(self, plan: RemediationPlan) -> None:
        self.plan = plan
        super().__init__(
            f"remediation for '{plan.finding_id}' requires --approve -- refusing to mutate"
        )


class BatchTooLargeError(Exception):
    """Raised when a batch exceeds its cap and no explicit override was given."""

    def __init__(self, count: int, cap: int) -> None:
        self.count = count
        self.cap = cap
        super().__init__(
            f"{count} finding(s) exceeds the batch cap of {cap} -- "
            "pass a higher --max-batch to remediate more than that in one run"
        )


@dataclass
class RemediationAction:
    """The exact AWS API call a plan would make -- `api_call` names the boto3 client method, `args` its kwargs."""

    api_call: str
    args: dict[str, Any]


@dataclass
class RemediationPlan:
    """What `build_remediation_plan()` returns -- always safe to compute, never mutates anything.

    `status` is one of `remediable` (an `action` is set and ready to run),
    `already_fixed` (the finding no longer applies, re-checked against live
    state), or `not_supported` (this rule is never auto-remediated -- see
    `reason`). `before_state` captures the resource's current state at plan
    time, which becomes the rollback record's "before" half once (and if)
    the plan is actually executed.
    """

    finding_id: str
    resource_id: str
    resource_type: str
    rule_id: str
    status: str
    reason: str | None = None
    action: RemediationAction | None = None
    before_state: dict[str, Any] | None = None


@dataclass
class RemediationResult:
    """What `execute_remediation_plan()` returns -- the outcome of trying to act on a plan.

    `status` is `executed` (a mutation really happened), `already_fixed`
    (nothing to do, re-checked at execute time too), or `not_supported`.
    `verified` is only meaningful for `executed`: it is `True` only when a
    fresh read of the resource, taken after the write call returned,
    actually shows the change -- the API call not raising an error is never
    treated as proof by itself (see the Deep Dive).
    """

    finding_id: str
    resource_id: str
    rule_id: str
    status: str
    action: str | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    verified: bool | None = None
    message: str = ""
    approved_by: str | None = None
    timestamp: str | None = None


def load_remediation_config(path: Path) -> dict[str, Any]:
    """Load a remediation config YAML file -- reuses `config.load_yaml_dict()`, like `cloudaudit.load_policy()` does."""
    data = load_yaml_dict(path)
    data.setdefault("allowlist", [])
    data.setdefault("policies", {})
    data.setdefault("audit_log_path", "remediation-audit.jsonl")
    return data


def make_finding_id(finding: dict[str, Any]) -> str:
    """Build a stable finding identifier from `(resource_id, rule_id)`.

    Not a report-position index: a finding's position in a report's list
    can shift between runs (new violations, a different scan order), but
    the pair `(resource_id, rule_id)` identifies the same violation every
    time cloud-audit finds it. This is the exact string a learner types on
    the command line as `<finding-id>`.
    """
    return f"{finding['resource_id']}:{finding['rule_id']}"


def find_finding(report: dict[str, Any], finding_id: str) -> dict[str, Any]:
    """Look up one finding in a report (written by `cloud-audit --output`) by its finding_id."""
    findings: list[dict[str, Any]] = report.get("findings", [])
    for finding in findings:
        if make_finding_id(finding) == finding_id:
            return finding
    raise FindingNotFoundError(f"no finding with id '{finding_id}' in this report")


# ---------------------------------------------------------------------------
# State readers -- shared between planning (the "before" read) and
# post-execute verification (the "after" read), so a plan's before_state and
# a result's after_state are always read the same way.
# ---------------------------------------------------------------------------


def _current_tags(client: Any, bucket_name: str) -> dict[str, str]:
    """The same 'expected failure, not an error' handling `multiregion._bucket_tags()` already uses for `NoSuchTagSet`."""
    try:
        response = client.get_bucket_tagging(Bucket=bucket_name)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NoSuchTagSet":
            return {}
        raise
    return {tag["Key"]: tag["Value"] for tag in response.get("TagSet", [])}


# ---------------------------------------------------------------------------
# Planners -- one per remediable rule_id. Each reads current live state and
# returns (status, reason, action, before_state). None of them ever calls a
# write-mode AWS API -- that only happens in execute_remediation_plan(),
# and only after --approve.
# ---------------------------------------------------------------------------

_PlannerResult = tuple[
    str, "str | None", "RemediationAction | None", "dict[str, Any] | None"
]


def _plan_required_tags(
    client: Any, resource_id: str, rule_policy: dict[str, Any]
) -> _PlannerResult:
    defaults: dict[str, str] = rule_policy.get("defaults", {})
    current = _current_tags(client, resource_id)
    missing = {key: value for key, value in defaults.items() if key not in current}

    if not missing:
        return (
            "already_fixed",
            "all configured default tags are already present",
            None,
            {"tags": current},
        )

    merged = {**current, **missing}
    action = RemediationAction(
        api_call="put_bucket_tagging",
        args={
            "Bucket": resource_id,
            "Tagging": {
                "TagSet": [{"Key": k, "Value": v} for k, v in sorted(merged.items())]
            },
        },
    )
    return "remediable", None, action, {"tags": current}


def _plan_require_encryption(
    client: Any, resource_id: str, rule_policy: dict[str, Any]
) -> _PlannerResult:
    kms_key_id = rule_policy.get("kms_key_id")
    if not kms_key_id:
        return (
            "not_supported",
            "no kms_key_id configured for require-encryption in the remediation config",
            None,
            None,
        )

    encrypted = cloudaudit.bucket_uses_kms_encryption(client, resource_id)
    if encrypted:
        return (
            "already_fixed",
            "bucket already uses a customer-managed KMS key",
            None,
            {"encrypted": True},
        )

    action = RemediationAction(
        api_call="put_bucket_encryption",
        args={
            "Bucket": resource_id,
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": kms_key_id,
                        }
                    }
                ]
            },
        },
    )
    return "remediable", None, action, {"encrypted": False}


def _plan_no_public_exposure(
    client: Any, resource_id: str, rule_policy: dict[str, Any]
) -> _PlannerResult:
    public = cloudaudit.bucket_is_public(client, resource_id)
    if not public:
        return "already_fixed", "bucket ACL is already private", None, {"public": False}

    action = RemediationAction(
        api_call="put_bucket_acl", args={"Bucket": resource_id, "ACL": "private"}
    )
    return "remediable", None, action, {"public": True}


PLANNERS: dict[str, Callable[[Any, str, dict[str, Any]], _PlannerResult]] = {
    "required-tags": _plan_required_tags,
    "require-encryption": _plan_require_encryption,
    "no-public-exposure": _plan_no_public_exposure,
}
"""Every rule_id this module knows how to plan a fix for.

Being in this dict is necessary but not sufficient to be remediated -- the
rule_id must ALSO be on the allowlist. `approved-regions` and
`max-age-days` are deliberately never in this dict at all: no code here
even attempts to plan a fix for them.
"""


def _after_state_for(client: Any, rule_id: str, resource_id: str) -> dict[str, Any]:
    """Read the same evidence a planner reads, after a mutation, for verification and the audit log's `after_state`."""
    if rule_id == "required-tags":
        return {"tags": _current_tags(client, resource_id)}
    if rule_id == "require-encryption":
        return {"encrypted": cloudaudit.bucket_uses_kms_encryption(client, resource_id)}
    if rule_id == "no-public-exposure":
        return {"public": cloudaudit.bucket_is_public(client, resource_id)}
    return {}


def build_remediation_plan(
    client: Any,
    finding: dict[str, Any],
    *,
    allowlist: list[str],
    remediation_config: dict[str, Any] | None = None,
) -> RemediationPlan:
    """Build a `RemediationPlan` for one finding -- reads live state, never writes.

    The allowlist gate is checked first, before this function even looks up
    whether a planner exists. A rule_id missing from `allowlist` is always
    `not_supported`, regardless of whether `PLANNERS` has code for it.
    """
    resource_id = finding["resource_id"]
    resource_type = finding["resource_type"]
    rule_id = finding["rule_id"]
    finding_id = make_finding_id(finding)
    remediation_config = remediation_config or {}

    if rule_id not in allowlist:
        return RemediationPlan(
            finding_id=finding_id,
            resource_id=resource_id,
            resource_type=resource_type,
            rule_id=rule_id,
            status="not_supported",
            reason=(
                f"rule '{rule_id}' is not on the remediation allowlist -- remediation is "
                "refused regardless of whether code exists for it"
            ),
        )

    planner = PLANNERS.get(rule_id)
    if planner is None:
        return RemediationPlan(
            finding_id=finding_id,
            resource_id=resource_id,
            resource_type=resource_type,
            rule_id=rule_id,
            status="not_supported",
            reason=(
                f"'{rule_id}' has no automatic remediation -- this kind of change needs a "
                "human decision, not an automated one"
            ),
        )

    rule_policy = remediation_config.get("policies", {}).get(rule_id, {})
    status, reason, action, before_state = planner(client, resource_id, rule_policy)
    return RemediationPlan(
        finding_id=finding_id,
        resource_id=resource_id,
        resource_type=resource_type,
        rule_id=rule_id,
        status=status,
        reason=reason,
        action=action,
        before_state=before_state,
    )


def append_audit_log(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON-lines record -- this file IS the rollback information (see the Deep Dive)."""
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def execute_remediation_plan(
    client: Any,
    plan: RemediationPlan,
    *,
    approve: bool,
    allowlist: list[str],
    remediation_config: dict[str, Any] | None = None,
    audit_log_path: Path | None = None,
    actor: str = "cli-operator",
    now: datetime | None = None,
) -> RemediationResult:
    """Act on a plan -- refuses to mutate without `approve=True`, re-checks state before writing.

    A `not_supported` or `already_fixed` plan passes straight through --
    there is nothing to approve, because nothing would be written either
    way. A `remediable` plan with `approve=False` raises
    `RemediationNotApprovedError` rather than returning quietly: there is
    no code path here where omitting `--approve` looks like success.

    Before an approved plan actually mutates anything, this function
    rebuilds the plan from scratch against current live state (calling
    `build_remediation_plan()` again, with the SAME allowlist and config
    passed to this call) -- proof, not assumption, that the finding still
    applies. That single re-check is what makes running `remediate-execute`
    twice on the same finding safe: the second run's fresh plan comes back
    `already_fixed`, and nothing is written a second time. It is also what
    makes the allowlist gate apply at execute time too, not only when a
    caller happens to have called `build_remediation_plan()` first -- a
    hand-built "remediable" plan for a rule not on the allowlist is still
    refused here.
    """
    if plan.status == "not_supported":
        return RemediationResult(
            finding_id=plan.finding_id,
            resource_id=plan.resource_id,
            rule_id=plan.rule_id,
            status="not_supported",
            message=plan.reason or "not supported",
        )

    if plan.status == "already_fixed":
        return RemediationResult(
            finding_id=plan.finding_id,
            resource_id=plan.resource_id,
            rule_id=plan.rule_id,
            status="already_fixed",
            before_state=plan.before_state,
            after_state=plan.before_state,
            message=plan.reason or "finding no longer applies -- nothing to do",
        )

    # plan.status == "remediable"
    if not approve:
        raise RemediationNotApprovedError(plan)

    finding = {
        "resource_id": plan.resource_id,
        "resource_type": plan.resource_type,
        "rule_id": plan.rule_id,
    }
    fresh_plan = build_remediation_plan(
        client, finding, allowlist=allowlist, remediation_config=remediation_config
    )

    if fresh_plan.status == "already_fixed":
        return RemediationResult(
            finding_id=plan.finding_id,
            resource_id=plan.resource_id,
            rule_id=plan.rule_id,
            status="already_fixed",
            before_state=fresh_plan.before_state,
            after_state=fresh_plan.before_state,
            message=fresh_plan.reason or "finding no longer applies -- nothing to do",
        )

    if fresh_plan.status != "remediable" or fresh_plan.action is None:
        # The allowlist or config changed out from under this call between
        # plan and execute -- refuse, the same as a finding that was never
        # remediable in the first place.
        return RemediationResult(
            finding_id=plan.finding_id,
            resource_id=plan.resource_id,
            rule_id=plan.rule_id,
            status="not_supported",
            message=fresh_plan.reason or "no longer remediable",
        )

    action = fresh_plan.action
    before_state = fresh_plan.before_state
    getattr(client, action.api_call)(**action.args)

    after_state = _after_state_for(client, plan.rule_id, plan.resource_id)
    verified = after_state != before_state

    resolved_now = now if now is not None else datetime.now(UTC)
    record: dict[str, Any] = {
        "timestamp": resolved_now.isoformat(),
        "finding_id": plan.finding_id,
        "resource_id": plan.resource_id,
        "rule_id": plan.rule_id,
        "action": action.api_call,
        "before_state": before_state,
        "after_state": after_state,
        "approved_by": actor,
    }
    if audit_log_path is not None:
        append_audit_log(audit_log_path, record)

    return RemediationResult(
        finding_id=plan.finding_id,
        resource_id=plan.resource_id,
        rule_id=plan.rule_id,
        status="executed",
        action=action.api_call,
        before_state=before_state,
        after_state=after_state,
        verified=verified,
        message="remediation applied",
        approved_by=actor,
        timestamp=record["timestamp"],
    )


def execute_remediation_batch(
    client: Any,
    plans: list[RemediationPlan],
    *,
    approve: bool,
    allowlist: list[str],
    remediation_config: dict[str, Any] | None = None,
    audit_log_path: Path | None = None,
    actor: str = "cli-operator",
    cap: int = DEFAULT_BATCH_CAP,
    now: datetime | None = None,
) -> list[RemediationResult]:
    """Execute more than one plan in a single call -- refuses outright if `len(plans) > cap`.

    Refuses, not truncates: a batch of 8 findings with a cap of 5 raises
    `BatchTooLargeError` before touching anything, rather than silently
    remediating the first 5 and dropping the rest. Raising the cap requires
    the caller to pass a higher `cap` explicitly (the CLI's `--max-batch`).
    """
    if len(plans) > cap:
        raise BatchTooLargeError(len(plans), cap)

    return [
        execute_remediation_plan(
            client,
            plan,
            approve=approve,
            allowlist=allowlist,
            remediation_config=remediation_config,
            audit_log_path=audit_log_path,
            actor=actor,
            now=now,
        )
        for plan in plans
    ]


def plan_to_dict(plan: RemediationPlan) -> dict[str, Any]:
    """`RemediationPlan` as a plain, JSON-serializable dict -- what the CLI prints for `--json`."""
    data = asdict(plan)
    return data


def result_to_dict(result: RemediationResult) -> dict[str, Any]:
    """`RemediationResult` as a plain, JSON-serializable dict -- what the CLI prints for `--json`."""
    return asdict(result)


__all__ = [
    "DEFAULT_BATCH_CAP",
    "PLANNERS",
    "BatchTooLargeError",
    "FindingNotFoundError",
    "RemediationAction",
    "RemediationNotApprovedError",
    "RemediationPlan",
    "RemediationResult",
    "append_audit_log",
    "build_remediation_plan",
    "execute_remediation_batch",
    "execute_remediation_plan",
    "find_finding",
    "load_remediation_config",
    "make_finding_id",
    "plan_to_dict",
    "result_to_dict",
]
