from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from platformops import cloudaudit
from platformops.awsclient import get_aws_client
from platformops.cloudremediate import (
    DEFAULT_BATCH_CAP,
    BatchTooLargeError,
    FindingNotFoundError,
    RemediationNotApprovedError,
    RemediationPlan,
    build_remediation_plan,
    execute_remediation_batch,
    execute_remediation_plan,
    find_finding,
    make_finding_id,
)

NOW = datetime(2026, 8, 5, tzinfo=UTC)

ALLOWLIST = ["required-tags", "require-encryption", "no-public-exposure"]

REMEDIATION_CONFIG = {
    "allowlist": ALLOWLIST,
    "policies": {
        "required-tags": {
            "defaults": {
                "team_owner": "platform-unassigned",
                "environment": "unassigned",
            }
        },
        "require-encryption": {"kms_key_id": "alias/platformops-remediation-demo"},
    },
}


def _finding(**overrides):
    finding = {
        "resource_id": "demo-bucket",
        "resource_type": "s3-bucket",
        "rule_id": "required-tags",
        "severity": "high",
        "evidence": "missing tags: team_owner, environment",
        "suppressed": False,
        "suppression_reason": None,
    }
    finding.update(overrides)
    return finding


# ---------------------------------------------------------------------------
# finding_id -- a stable identifier built from (resource_id, rule_id), not a
# report-position index. This is what a learner types on the command line.
# ---------------------------------------------------------------------------


def test_make_finding_id_combines_resource_and_rule():
    assert make_finding_id(_finding()) == "demo-bucket:required-tags"


def test_find_finding_locates_the_matching_finding_by_id():
    report = {"findings": [_finding(resource_id="a"), _finding(resource_id="b")]}

    found = find_finding(report, "b:required-tags")

    assert found["resource_id"] == "b"


def test_find_finding_raises_for_an_unknown_id():
    report = {"findings": [_finding()]}

    with pytest.raises(FindingNotFoundError):
        find_finding(report, "nope:required-tags")


# ---------------------------------------------------------------------------
# build_remediation_plan -- the allowlist gate. This is checked BEFORE
# whether a planner function exists at all -- a rule with real remediation
# code is still refused if it is not on the allowlist, and a rule with no
# planner is still refused even if someone adds it to the allowlist.
# ---------------------------------------------------------------------------


@mock_aws
def test_rule_with_planner_code_is_not_supported_when_not_on_the_allowlist():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")

    plan = build_remediation_plan(
        client, _finding(), allowlist=[], remediation_config=REMEDIATION_CONFIG
    )

    assert plan.status == "not_supported"
    assert "not on the remediation allowlist" in plan.reason
    assert plan.action is None
    # No mutation was even attempted -- the bucket still has no tags.
    with pytest.raises(client.exceptions.ClientError):
        client.get_bucket_tagging(Bucket="demo-bucket")


@mock_aws
def test_rule_with_no_planner_is_not_supported_even_when_added_to_the_allowlist():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    finding = _finding(
        rule_id="approved-regions", evidence="region 'eu-west-1' is not approved"
    )

    plan = build_remediation_plan(
        client,
        finding,
        allowlist=["approved-regions"],  # explicitly on the allowlist this time
        remediation_config=REMEDIATION_CONFIG,
    )

    assert plan.status == "not_supported"
    assert "human decision" in plan.reason


@mock_aws
def test_max_age_days_is_never_remediable_regardless_of_allowlist():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    finding = _finding(
        rule_id="max-age-days", evidence="age 400d exceeds max_age_days 90"
    )

    plan = build_remediation_plan(
        client,
        finding,
        allowlist=["max-age-days"],
        remediation_config=REMEDIATION_CONFIG,
    )

    assert plan.status == "not_supported"
    assert "human decision" in plan.reason


# ---------------------------------------------------------------------------
# build_remediation_plan -- required-tags. Pure planning: reads live state,
# never writes.
# ---------------------------------------------------------------------------


@mock_aws
def test_required_tags_plan_is_remediable_when_tags_are_missing():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")

    plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    assert plan.status == "remediable"
    assert plan.action.api_call == "put_bucket_tagging"
    tag_set = plan.action.args["Tagging"]["TagSet"]
    tags = {t["Key"]: t["Value"] for t in tag_set}
    assert tags["team_owner"] == "platform-unassigned"
    assert tags["environment"] == "unassigned"
    # Still just a plan -- nothing was written.
    with pytest.raises(client.exceptions.ClientError):
        client.get_bucket_tagging(Bucket="demo-bucket")


@mock_aws
def test_required_tags_plan_keeps_an_existing_tag_the_policy_does_not_mention():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    client.put_bucket_tagging(
        Bucket="demo-bucket",
        Tagging={"TagSet": [{"Key": "cost_center", "Value": "cc-9"}]},
    )

    plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    tags = {t["Key"]: t["Value"] for t in plan.action.args["Tagging"]["TagSet"]}
    assert tags["cost_center"] == "cc-9"
    assert tags["team_owner"] == "platform-unassigned"


@mock_aws
def test_required_tags_plan_is_already_fixed_when_all_default_tags_present():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    client.put_bucket_tagging(
        Bucket="demo-bucket",
        Tagging={
            "TagSet": [
                {"Key": "team_owner", "Value": "platform-unassigned"},
                {"Key": "environment", "Value": "unassigned"},
            ]
        },
    )

    plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    assert plan.status == "already_fixed"
    assert plan.action is None


# ---------------------------------------------------------------------------
# build_remediation_plan -- require-encryption and no-public-exposure.
# ---------------------------------------------------------------------------


@mock_aws
def test_require_encryption_plan_is_remediable_when_only_sse_s3_default_is_set():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    finding = _finding(
        rule_id="require-encryption", evidence="not using a customer-managed KMS key"
    )

    plan = build_remediation_plan(
        client, finding, allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    assert plan.status == "remediable"
    assert plan.action.api_call == "put_bucket_encryption"
    rule = plan.action.args["ServerSideEncryptionConfiguration"]["Rules"][0]
    assert rule["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"] == (
        "alias/platformops-remediation-demo"
    )


@mock_aws
def test_require_encryption_plan_is_already_fixed_once_a_kms_key_is_set():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    client.put_bucket_encryption(
        Bucket="demo-bucket",
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "aws:kms",
                        "KMSMasterKeyID": "alias/platformops-remediation-demo",
                    }
                }
            ]
        },
    )
    finding = _finding(rule_id="require-encryption")

    plan = build_remediation_plan(
        client, finding, allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    assert plan.status == "already_fixed"


@mock_aws
def test_no_public_exposure_plan_is_remediable_for_a_public_acl():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    client.put_bucket_acl(Bucket="demo-bucket", ACL="public-read")
    finding = _finding(rule_id="no-public-exposure", evidence="public exposure")

    plan = build_remediation_plan(
        client, finding, allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    assert plan.status == "remediable"
    assert plan.action.api_call == "put_bucket_acl"
    assert plan.action.args["ACL"] == "private"


@mock_aws
def test_no_public_exposure_plan_is_already_fixed_for_a_private_bucket():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    finding = _finding(rule_id="no-public-exposure")

    plan = build_remediation_plan(
        client, finding, allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    assert plan.status == "already_fixed"


# ---------------------------------------------------------------------------
# execute_remediation_plan -- refuses without --approve, mutates nothing.
# ---------------------------------------------------------------------------


@mock_aws
def test_execute_without_approve_raises_and_mutates_nothing(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )
    assert plan.status == "remediable"
    log_path = tmp_path / "audit.jsonl"

    with pytest.raises(RemediationNotApprovedError):
        execute_remediation_plan(
            client,
            plan,
            approve=False,
            allowlist=ALLOWLIST,
            remediation_config=REMEDIATION_CONFIG,
            audit_log_path=log_path,
        )

    # Still no tags -- nothing mutated.
    with pytest.raises(client.exceptions.ClientError):
        client.get_bucket_tagging(Bucket="demo-bucket")
    assert not log_path.exists()


@mock_aws
def test_execute_without_approve_on_a_not_supported_plan_never_raises(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    finding = _finding(rule_id="max-age-days")
    plan = build_remediation_plan(
        client, finding, allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )

    result = execute_remediation_plan(
        client,
        plan,
        approve=False,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=tmp_path / "audit.jsonl",
    )

    assert result.status == "not_supported"


# ---------------------------------------------------------------------------
# execute_remediation_plan -- approved execution actually mutates, captures
# before/after evidence, and writes one audit-log line.
# ---------------------------------------------------------------------------


@mock_aws
def test_execute_with_approve_mutates_and_writes_an_audit_log_entry(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )
    log_path = tmp_path / "audit.jsonl"

    result = execute_remediation_plan(
        client,
        plan,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=log_path,
        actor="test-operator",
        now=NOW,
    )

    assert result.status == "executed"
    assert result.action == "put_bucket_tagging"
    live_tags = client.get_bucket_tagging(Bucket="demo-bucket")["TagSet"]
    tags = {t["Key"]: t["Value"] for t in live_tags}
    assert tags["team_owner"] == "platform-unassigned"

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["finding_id"] == "demo-bucket:required-tags"
    assert record["resource_id"] == "demo-bucket"
    assert record["rule_id"] == "required-tags"
    assert record["action"] == "put_bucket_tagging"
    assert record["approved_by"] == "test-operator"
    assert record["before_state"]["tags"] == {}
    assert "team_owner" in record["after_state"]["tags"]
    assert record["timestamp"] == NOW.isoformat()


# ---------------------------------------------------------------------------
# Idempotency -- running execute twice on the same finding never re-applies
# the change. The second call re-checks live state and reports
# already_fixed, and the audit log gains no second entry.
# ---------------------------------------------------------------------------


@mock_aws
def test_second_execute_on_an_already_remediated_finding_reports_already_fixed(
    tmp_path,
):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    log_path = tmp_path / "audit.jsonl"

    first_plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )
    first_result = execute_remediation_plan(
        client,
        first_plan,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=log_path,
        now=NOW,
    )
    assert first_result.status == "executed"

    # Re-run the whole flow exactly as the CLI would on a second invocation:
    # a fresh plan, built from current live state.
    second_plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )
    assert second_plan.status == "already_fixed"

    second_result = execute_remediation_plan(
        client,
        second_plan,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=log_path,
        now=NOW,
    )

    assert second_result.status == "already_fixed"
    # Exactly one audit-log line -- the second run mutated nothing.
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1


@mock_aws
def test_execute_re_checks_state_even_from_a_stale_remediable_plan(tmp_path):
    """A plan built earlier can go stale if the resource changes before execute runs.

    execute_remediation_plan re-derives a fresh plan from live state right
    before mutating -- it never blindly trusts a plan object handed to it.
    """
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")
    stale_plan = build_remediation_plan(
        client, _finding(), allowlist=ALLOWLIST, remediation_config=REMEDIATION_CONFIG
    )
    assert stale_plan.status == "remediable"

    # Someone (or something) fixes it by hand between plan and execute.
    client.put_bucket_tagging(
        Bucket="demo-bucket",
        Tagging={
            "TagSet": [
                {"Key": "team_owner", "Value": "platform-unassigned"},
                {"Key": "environment", "Value": "unassigned"},
            ]
        },
    )

    result = execute_remediation_plan(
        client,
        stale_plan,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert result.status == "already_fixed"


# ---------------------------------------------------------------------------
# The allowlist gate is independent of --approve -- proven mechanically by
# handing execute_remediation_plan a hand-built "remediable" plan for a rule
# that is not on the allowlist. Even with approve=True, it must refuse.
# ---------------------------------------------------------------------------


@mock_aws
def test_execute_refuses_a_hand_built_remediable_plan_for_a_rule_not_on_the_allowlist(
    tmp_path,
):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="demo-bucket")

    # Bypass build_remediation_plan entirely -- simulate a caller that
    # somehow got hold of a "remediable" plan for a rule missing from the
    # allowlist actually in force.
    from platformops.cloudremediate import RemediationAction

    forged_plan = RemediationPlan(
        finding_id="demo-bucket:required-tags",
        resource_id="demo-bucket",
        resource_type="s3-bucket",
        rule_id="required-tags",
        status="remediable",
        action=RemediationAction(
            api_call="put_bucket_tagging",
            args={"Bucket": "demo-bucket", "Tagging": {"TagSet": []}},
        ),
        before_state={"tags": {}},
    )

    result = execute_remediation_plan(
        client,
        forged_plan,
        approve=True,
        allowlist=[],  # required-tags is NOT on the allowlist in force
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert result.status == "not_supported"
    with pytest.raises(client.exceptions.ClientError):
        client.get_bucket_tagging(Bucket="demo-bucket")


# ---------------------------------------------------------------------------
# Batch cap -- refuses (not silently truncates) an over-cap batch.
# ---------------------------------------------------------------------------


@mock_aws
def test_batch_refuses_when_over_the_default_cap(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    plans = []
    for i in range(DEFAULT_BATCH_CAP + 1):
        bucket = f"demo-bucket-{i}"
        client.create_bucket(Bucket=bucket)
        plan = build_remediation_plan(
            client,
            _finding(resource_id=bucket),
            allowlist=ALLOWLIST,
            remediation_config=REMEDIATION_CONFIG,
        )
        plans.append(plan)

    with pytest.raises(BatchTooLargeError):
        execute_remediation_batch(
            client,
            plans,
            approve=True,
            allowlist=ALLOWLIST,
            remediation_config=REMEDIATION_CONFIG,
            audit_log_path=tmp_path / "audit.jsonl",
        )

    # Nothing was mutated -- the batch was refused before touching anything.
    for i in range(DEFAULT_BATCH_CAP + 1):
        with pytest.raises(client.exceptions.ClientError):
            client.get_bucket_tagging(Bucket=f"demo-bucket-{i}")


@mock_aws
def test_batch_succeeds_over_the_default_cap_with_an_explicit_override(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    plans = []
    for i in range(DEFAULT_BATCH_CAP + 1):
        bucket = f"demo-bucket-{i}"
        client.create_bucket(Bucket=bucket)
        plan = build_remediation_plan(
            client,
            _finding(resource_id=bucket),
            allowlist=ALLOWLIST,
            remediation_config=REMEDIATION_CONFIG,
        )
        plans.append(plan)

    results = execute_remediation_batch(
        client,
        plans,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=tmp_path / "audit.jsonl",
        cap=DEFAULT_BATCH_CAP + 1,
        now=NOW,
    )

    assert len(results) == DEFAULT_BATCH_CAP + 1
    assert all(r.status == "executed" for r in results)


# ---------------------------------------------------------------------------
# Live tests against a real, running Floci -- prove the whole flow end to
# end: seed a real violation, plan it, execute it, and confirm a fresh
# cloud-audit no longer reports it. Idempotency proven with two real,
# consecutive executes.
# ---------------------------------------------------------------------------

FLOCI_ENDPOINT = "http://localhost:4566"
REGION = "us-east-1"


@pytest.fixture
def s3_client(require_floci):
    return get_aws_client("s3", region=REGION, endpoint_url=FLOCI_ENDPOINT)


@pytest.fixture
def bucket(s3_client):
    import uuid

    name = f"platformops-remediate-test-{uuid.uuid4().hex[:12]}"
    s3_client.create_bucket(Bucket=name)
    yield name
    try:
        s3_client.delete_bucket(Bucket=name)
    except s3_client.exceptions.ClientError:
        pass


def _audit_finding(
    policy_path: Path, region: str, endpoint_url: str, resource_id: str, rule_id: str
):
    report = cloudaudit.run_cloud_audit(
        policy_path=policy_path, region=region, endpoint_url=endpoint_url
    )
    matches = [
        f
        for f in report["findings"]
        if f["resource_id"] == resource_id
        and f["rule_id"] == rule_id
        and not f["suppressed"]
    ]
    return matches


def test_live_execute_with_approve_mutates_and_a_fresh_audit_no_longer_reports_it(
    s3_client, bucket, tmp_path
):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "rules:\n"
        "  - id: required-tags\n"
        "    check: required_tags\n"
        "    severity: high\n"
        "    required_tags: [team_owner, environment]\n"
        "exceptions: []\n"
    )

    before_findings = _audit_finding(
        policy_path, REGION, FLOCI_ENDPOINT, bucket, "required-tags"
    )
    assert len(before_findings) == 1

    plan = build_remediation_plan(
        s3_client,
        before_findings[0],
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
    )
    assert plan.status == "remediable"

    log_path = tmp_path / "audit.jsonl"
    result = execute_remediation_plan(
        s3_client,
        plan,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=log_path,
        actor="live-test",
    )
    assert result.status == "executed"

    after_findings = _audit_finding(
        policy_path, REGION, FLOCI_ENDPOINT, bucket, "required-tags"
    )
    assert after_findings == []


def test_live_idempotent_second_execute_reports_already_fixed_with_no_second_log_entry(
    s3_client, bucket, tmp_path
):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "rules:\n"
        "  - id: require-encryption\n"
        "    check: require_encryption\n"
        "    severity: high\n"
        "exceptions: []\n"
    )
    log_path = tmp_path / "audit.jsonl"

    findings = _audit_finding(
        policy_path, REGION, FLOCI_ENDPOINT, bucket, "require-encryption"
    )
    assert len(findings) == 1
    plan_one = build_remediation_plan(
        s3_client,
        findings[0],
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
    )
    result_one = execute_remediation_plan(
        s3_client,
        plan_one,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=log_path,
        actor="live-test",
    )
    assert result_one.status == "executed"

    # Second, real, consecutive run -- fresh plan built from live state again.
    findings_after = _audit_finding(
        policy_path, REGION, FLOCI_ENDPOINT, bucket, "require-encryption"
    )
    assert findings_after == []
    plan_two = build_remediation_plan(
        s3_client,
        {
            "resource_id": bucket,
            "resource_type": "s3-bucket",
            "rule_id": "require-encryption",
        },
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
    )
    assert plan_two.status == "already_fixed"
    result_two = execute_remediation_plan(
        s3_client,
        plan_two,
        approve=True,
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
        audit_log_path=log_path,
        actor="live-test",
    )

    assert result_two.status == "already_fixed"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_live_execute_without_approve_leaves_the_real_bucket_unchanged(
    s3_client, bucket, tmp_path
):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "rules:\n"
        "  - id: required-tags\n"
        "    check: required_tags\n"
        "    severity: high\n"
        "    required_tags: [team_owner]\n"
        "exceptions: []\n"
    )

    findings = _audit_finding(
        policy_path, REGION, FLOCI_ENDPOINT, bucket, "required-tags"
    )
    assert len(findings) == 1
    plan = build_remediation_plan(
        s3_client,
        findings[0],
        allowlist=ALLOWLIST,
        remediation_config=REMEDIATION_CONFIG,
    )

    with pytest.raises(RemediationNotApprovedError):
        execute_remediation_plan(
            s3_client,
            plan,
            approve=False,
            allowlist=ALLOWLIST,
            remediation_config=REMEDIATION_CONFIG,
            audit_log_path=tmp_path / "audit.jsonl",
        )

    findings_after = _audit_finding(
        policy_path, REGION, FLOCI_ENDPOINT, bucket, "required-tags"
    )
    assert len(findings_after) == 1
