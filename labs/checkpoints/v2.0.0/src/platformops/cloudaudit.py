"""platformops.cloudaudit -- check real cloud resources against a written policy, and report violations.

Every prior AWS module in this project either reports what exists
(`cloudinventory`, `multiregion`) or does something to a resource
(`reportstore`, `findingsstore`, `workqueue`). This module does neither. It
checks what already exists against a policy written down in a YAML file --
required tags, approved regions, a maximum age, whether encryption is
turned on, whether a resource is exposed to the public -- and produces a
list of `Finding` objects, each with the exact evidence that proved the
violation. It never fixes anything. A later module (Safe Cloud
Remediation) is where the fix side lives; keeping the two apart means a
tool that only ever reports cannot ever be the thing that changes
production state by accident.

That split shows up directly in this module's shape. `evaluate_policy()`
-- the detection function -- takes evidence that has already been
gathered and a loaded policy, and returns findings. It makes no network
call of any kind, so it can be unit-tested with hand-built evidence and no
Floci, no AWS, nothing running at all. `gather_bucket_evidence()` is the
only place in this module that talks to AWS -- it is the sole function
here that calls `get_bucket_encryption`, `get_bucket_acl`, or
`list_buckets`. That is not a style preference; the Deep Dive proves it
mechanically, by grepping this file for write-mode AWS calls (`put_*`,
`delete_*`, `create_*`, `modify_*`) and showing there are none.

Evidence composes with `multiregion.ResourceRecord` instead of extending
it. `ResourceRecord` (Module 23) is already used by `to_markdown()` and
`to_csv()`, and by every future module that only needs "what exists, with
what tags, in what region" -- adding audit-only fields like `encrypted` or
`public` onto that shared dataclass would put fields on it that only this
one module ever fills in, and that every other caller would have to carry
around as always-`None`. `ResourceEvidence` instead wraps one
`ResourceRecord` and adds exactly the fields this module's policy checks
need. `multiregion.list_s3_buckets()` still does the one thing it always
did -- discover buckets, with their tags and region -- and this module
never re-implements that.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import botocore.exceptions

from platformops.awsclient import get_aws_client
from platformops.config import load_yaml_dict
from platformops.multiregion import ResourceRecord, list_s3_buckets

# The well-known S3 URI for the "AllUsers" group -- a grant to this URI in a
# bucket's ACL means anyone on the internet, not just AWS accounts. This is
# the same signal the AWS console's own "Public Access" bucket badge is
# built from.
PUBLIC_ACCESS_GROUP_URI = "http://acs.amazonaws.com/groups/global/AllUsers"


@dataclass
class ResourceEvidence:
    """One resource, with everything this module's checks need to evaluate it.

    `encrypted`, `public` and `created_at` default to `None`, meaning "not
    gathered for this resource" -- a real, honest value, not a stand-in for
    `False`. A rule whose check needs one of these fields skips a resource
    that carries `None` for it, instead of reporting a false violation for
    a resource type this module never gathered that evidence for (see
    `applies_to` on a policy rule for the explicit version of the same
    idea).
    """

    resource_type: str
    resource_id: str
    region: str
    tags: dict[str, str]
    encrypted: bool | None = None
    """True if a customer-managed KMS key is set as the default encryption -- not just any encryption."""
    public: bool | None = None
    created_at: str | None = None

    @classmethod
    def from_record(
        cls,
        record: ResourceRecord,
        *,
        encrypted: bool | None = None,
        public: bool | None = None,
        created_at: str | None = None,
    ) -> ResourceEvidence:
        """Build evidence from a `multiregion.ResourceRecord`, without touching that dataclass itself."""
        return cls(
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            region=record.region,
            tags=record.tags,
            encrypted=encrypted,
            public=public,
            created_at=created_at,
        )


@dataclass
class Finding:
    """One rule, one resource, one violation -- with the evidence that proved it.

    `evidence` is always a specific string ("missing tags: team_owner",
    not a bare `True`) -- an auditor reading a report needs to see what was
    actually wrong, not just that something was. `suppressed` and
    `suppression_reason` keep a suppressed finding in the list instead of
    dropping it: an unexpired exception explains why a real violation is
    not being acted on right now, and that explanation belongs in the
    report next to the finding it excuses, not hidden by a shorter list.
    """

    resource_id: str
    resource_type: str
    rule_id: str
    severity: str
    evidence: str
    suppressed: bool = False
    suppression_reason: str | None = None


def load_policy(path: Path) -> dict[str, Any]:
    """Load a policy YAML file -- reuses `config.load_yaml_dict()`, this module's only file I/O."""
    data = load_yaml_dict(path)
    data.setdefault("rules", [])
    data.setdefault("exceptions", [])
    return data


# ---------------------------------------------------------------------------
# Detection -- pure functions. Nothing below this line makes a network call.
# Every check takes one ResourceEvidence, one rule dict and `now`, and
# returns either None (compliant, or not applicable to this resource) or a
# string describing exactly what was found.
# ---------------------------------------------------------------------------


def _check_required_tags(
    evidence: ResourceEvidence, rule: dict[str, Any], *, now: datetime
) -> str | None:
    required = rule.get("required_tags", [])
    missing = [tag for tag in required if tag not in evidence.tags]
    if missing:
        return f"missing tags: {', '.join(missing)}"
    return None


def _check_approved_regions(
    evidence: ResourceEvidence, rule: dict[str, Any], *, now: datetime
) -> str | None:
    approved = rule.get("approved_regions", [])
    if evidence.region not in approved:
        return f"region '{evidence.region}' is not in the approved list {approved}"
    return None


def _check_max_age_days(
    evidence: ResourceEvidence, rule: dict[str, Any], *, now: datetime
) -> str | None:
    if evidence.created_at is None:
        return None
    max_age_days = rule["max_age_days"]
    created = datetime.fromisoformat(evidence.created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age_days = (now - created).days
    if age_days > max_age_days:
        return (
            f"age {age_days}d exceeds max_age_days {max_age_days} "
            f"(created {evidence.created_at})"
        )
    return None


def _check_require_encryption(
    evidence: ResourceEvidence, rule: dict[str, Any], *, now: datetime
) -> str | None:
    if evidence.encrypted is None:
        return None
    if not evidence.encrypted:
        return "encryption: not using a customer-managed KMS key (AWS-managed SSE-S3 default only)"
    return None


def _check_no_public_exposure(
    evidence: ResourceEvidence, rule: dict[str, Any], *, now: datetime
) -> str | None:
    if evidence.public is None:
        return None
    if evidence.public:
        return "public exposure: ACL grants access to the AllUsers group"
    return None


CHECKS = {
    "required_tags": _check_required_tags,
    "approved_regions": _check_approved_regions,
    "max_age_days": _check_max_age_days,
    "require_encryption": _check_require_encryption,
    "no_public_exposure": _check_no_public_exposure,
}


def _exception_is_active(exception: dict[str, Any], *, now: datetime) -> bool:
    """An exception with no `expires` field never expires; one with a past `expires` date no longer suppresses."""
    expires = exception.get("expires")
    if not expires:
        return True
    expiry = datetime.strptime(expires, "%Y-%m-%d").replace(tzinfo=UTC)
    return now.date() <= expiry.date()


def _find_active_exception(
    resource_id: str,
    rule_id: str,
    exceptions: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    for exception in exceptions:
        if (
            exception.get("resource_id") == resource_id
            and exception.get("rule_id") == rule_id
            and _exception_is_active(exception, now=now)
        ):
            return exception
    return None


def evaluate_policy(
    evidence: list[ResourceEvidence],
    policy: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[Finding]:
    """Check every piece of evidence against every rule, and return every finding -- suppressed ones included.

    `now` defaults to the current time -- passed explicitly (as every test
    in this module does) whenever a specific, reproducible instant matters,
    the same convention `findingsstore.put_finding()`'s `timestamp`
    parameter already established for testable time.

    A rule's optional `applies_to` field (a list of resource types) skips
    that rule for any resource type not in the list -- `require_encryption`
    means nothing for an EC2 instance, and this keeps that explicit in the
    policy file instead of silently relying on `evidence.encrypted` being
    `None`.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    rules = policy.get("rules", [])
    exceptions = policy.get("exceptions", [])

    findings: list[Finding] = []
    for item in evidence:
        for rule in rules:
            applies_to = rule.get("applies_to")
            if applies_to is not None and item.resource_type not in applies_to:
                continue

            check = CHECKS[rule["check"]]
            violation = check(item, rule, now=resolved_now)
            if violation is None:
                continue

            active_exception = _find_active_exception(
                item.resource_id, rule["id"], exceptions, now=resolved_now
            )
            findings.append(
                Finding(
                    resource_id=item.resource_id,
                    resource_type=item.resource_type,
                    rule_id=rule["id"],
                    severity=rule["severity"],
                    evidence=violation,
                    suppressed=active_exception is not None,
                    suppression_reason=(
                        active_exception.get("reason") if active_exception else None
                    ),
                )
            )
    return findings


def build_report(findings: list[Finding]) -> dict[str, Any]:
    """Summarize findings by severity and suppression state, plus the full list -- the audit's paper trail."""
    active = [f for f in findings if not f.suppressed]
    suppressed = [f for f in findings if f.suppressed]

    by_severity: dict[str, int] = {}
    for finding in active:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1

    return {
        "status": "violations" if active else "ok",
        "summary": {
            "total_findings": len(findings),
            "active": len(active),
            "suppressed": len(suppressed),
            "by_severity": by_severity,
        },
        "findings": [asdict(f) for f in findings],
    }


__all__ = [
    "PUBLIC_ACCESS_GROUP_URI",
    "CHECKS",
    "ResourceEvidence",
    "Finding",
    "load_policy",
    "evaluate_policy",
    "build_report",
    "bucket_uses_kms_encryption",
    "bucket_is_public",
    "gather_bucket_evidence",
    "run_cloud_audit",
]


# ---------------------------------------------------------------------------
# Evidence-gathering -- the only functions in this module that touch the
# network. Kept separate from detection above so the detection function
# never needs a live AWS connection (or Floci) to be tested at all.
# ---------------------------------------------------------------------------


def bucket_uses_kms_encryption(client: Any, bucket_name: str) -> bool:
    """True only if `bucket_name`'s default encryption uses a customer-managed KMS key.

    Every S3 bucket -- on real AWS since January 2023, and on Floci, which
    matches that behavior -- already has SSE-S3 (`AES256`) default
    encryption turned on the moment it is created. A check that only asked
    "is default encryption configured at all" would never find a violation
    on a real account, because that question stopped being interesting
    once AWS made the answer always "yes". The question worth auditing is
    narrower: does this bucket use a customer-managed KMS key
    (`SSEAlgorithm: aws:kms`), the level of encryption many compliance
    policies actually require, so a security team controls key rotation
    and can see key-usage in CloudTrail -- not the AWS-managed default.

    An unencrypted bucket's `get_bucket_encryption()` call fails with
    `ServerSideEncryptionConfigurationNotFoundError` -- the same "expected
    failure, not an error" pattern `multiregion._bucket_tags()` already
    uses for `NoSuchTagSet`. On Floci and on real AWS today this should
    never actually happen (see above), but the check still handles it
    honestly rather than assuming it cannot occur. Anything else is a real
    problem and is left to raise.
    """
    try:
        response = client.get_bucket_encryption(Bucket=bucket_name)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ServerSideEncryptionConfigurationNotFoundError":
            return False
        raise
    rules = response["ServerSideEncryptionConfiguration"]["Rules"]
    return any(
        rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
        for rule in rules
    )


def bucket_is_public(client: Any, bucket_name: str) -> bool:
    """True if `bucket_name`'s ACL grants any access to the AllUsers group."""
    acl = client.get_bucket_acl(Bucket=bucket_name)
    for grant in acl.get("Grants", []):
        if grant.get("Grantee", {}).get("URI") == PUBLIC_ACCESS_GROUP_URI:
            return True
    return False


def gather_bucket_evidence(
    client: Any, records: list[ResourceRecord]
) -> list[ResourceEvidence]:
    """Turn S3 `ResourceRecord`s into `ResourceEvidence`, adding encryption, exposure and age.

    `list_buckets()` is called once, account-wide, to map every bucket name
    to its real `CreationDate` -- the same "one account-wide call, not one
    per bucket" discipline `multiregion.list_s3_buckets()` already applies
    to `list_buckets()` itself. `get_bucket_encryption()` and
    `get_bucket_acl()` are still one call per bucket -- neither has an
    account-wide equivalent.

    Records for any resource type other than `s3-bucket` pass through with
    `encrypted`, `public` and `created_at` left `None` -- this module only
    knows how to gather S3-specific evidence today; a rule that needs that
    evidence skips a resource this function never gathered it for (see
    `evaluate_policy()`'s `applies_to` handling).
    """
    creation_dates = {
        bucket["Name"]: bucket["CreationDate"]
        for bucket in client.list_buckets()["Buckets"]
    }

    evidence: list[ResourceEvidence] = []
    for record in records:
        if record.resource_type != "s3-bucket":
            evidence.append(ResourceEvidence.from_record(record))
            continue

        created_at = creation_dates.get(record.resource_id)
        evidence.append(
            ResourceEvidence.from_record(
                record,
                encrypted=bucket_uses_kms_encryption(client, record.resource_id),
                public=bucket_is_public(client, record.resource_id),
                created_at=created_at.isoformat() if created_at else None,
            )
        )
    return evidence


def run_cloud_audit(
    *,
    policy_path: Path,
    region: str,
    profile: str | None = None,
    endpoint_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The one call a CLI command or a script needs: gather S3 evidence, evaluate the policy, build the report.

    Catches the same two exception types every AWS-backed command-level
    function in this project catches by name (`cloudinventory.scan_inventory()`,
    `reportstore.upload_report_to_bucket()`), and returns the same
    `{"status": "error", ...}` shape for a failure to even run the audit --
    kept distinct from `build_report()`'s `"ok"`/`"violations"`, which both
    mean the audit ran successfully and are about what it found.
    """
    try:
        policy = load_policy(policy_path)
        client = get_aws_client(
            "s3", region=region, profile=profile, endpoint_url=endpoint_url
        )
        records = list_s3_buckets(client)
        evidence = gather_bucket_evidence(client, records)
    except botocore.exceptions.NoCredentialsError as exc:
        return {"status": "error", "error": "no-credentials", "message": str(exc)}
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        return {"status": "error", "error": code, "message": str(exc)}

    findings = evaluate_policy(evidence, policy, now=now)
    return build_report(findings)
