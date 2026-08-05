from datetime import UTC, datetime

import pytest

from platformops import cloudaudit
from platformops.awsclient import get_aws_client
from platformops.cloudaudit import (
    Finding,
    ResourceEvidence,
    build_report,
    evaluate_policy,
)
from platformops.multiregion import list_s3_buckets

# ---------------------------------------------------------------------------
# evaluate_policy -- pure, no network calls of any kind. Every test here
# hand-builds evidence and a policy dict directly; none of them needs Floci
# running.
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _policy(**overrides):
    policy = {"rules": [], "exceptions": []}
    policy.update(overrides)
    return policy


def test_compliant_resource_produces_no_finding():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="good-bucket",
            region="us-east-1",
            tags={"team_owner": "platform", "environment": "prod"},
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "required-tags",
                "check": "required_tags",
                "severity": "high",
                "required_tags": ["team_owner", "environment"],
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert findings == []


def test_required_tags_reports_only_the_missing_tags():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="bad-bucket",
            region="us-east-1",
            tags={"team_owner": "platform"},
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "required-tags",
                "check": "required_tags",
                "severity": "high",
                "required_tags": ["team_owner", "environment"],
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert findings[0].rule_id == "required-tags"
    assert findings[0].severity == "high"
    assert findings[0].evidence == "missing tags: environment"
    assert findings[0].suppressed is False


def test_approved_regions_rejects_a_region_not_in_the_list():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="wrong-region-bucket",
            region="ap-south-1",
            tags={},
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "approved-regions",
                "check": "approved_regions",
                "severity": "high",
                "approved_regions": ["us-east-1", "us-west-2"],
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert "ap-south-1" in findings[0].evidence


def test_require_encryption_flags_a_disabled_bucket():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="unencrypted-bucket",
            region="us-east-1",
            tags={},
            encrypted=False,
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "require-encryption",
                "check": "require_encryption",
                "severity": "high",
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert "not using a customer-managed KMS key" in findings[0].evidence


def test_require_encryption_is_skipped_when_evidence_was_never_gathered():
    evidence = [
        ResourceEvidence(
            resource_type="ec2-instance",
            resource_id="i-0123456789",
            region="us-east-1",
            tags={},
            encrypted=None,
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "require-encryption",
                "check": "require_encryption",
                "severity": "high",
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert findings == []


def test_no_public_exposure_flags_a_public_bucket():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="public-bucket",
            region="us-east-1",
            tags={},
            public=True,
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "no-public-exposure",
                "check": "no_public_exposure",
                "severity": "critical",
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert findings[0].severity == "critical"


def test_applies_to_skips_a_rule_for_a_resource_type_not_listed():
    evidence = [
        ResourceEvidence(
            resource_type="ec2-instance",
            resource_id="i-0123456789",
            region="us-east-1",
            tags={},
            public=True,
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "no-public-exposure",
                "check": "no_public_exposure",
                "severity": "critical",
                "applies_to": ["s3-bucket"],
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert findings == []


# ---------------------------------------------------------------------------
# max_age_days -- proven with a controlled `now` and a controlled
# `created_at`, never against Floci's real clock (Floci cannot backdate a
# resource's actual creation time).
# ---------------------------------------------------------------------------


def test_max_age_days_flags_a_resource_older_than_the_limit():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="old-bucket",
            region="us-east-1",
            tags={},
            created_at="2026-01-01T00:00:00+00:00",
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "max-age-days",
                "check": "max_age_days",
                "severity": "medium",
                "max_age_days": 90,
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert "exceeds max_age_days 90" in findings[0].evidence


def test_max_age_days_is_silent_for_a_resource_within_the_limit():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="new-bucket",
            region="us-east-1",
            tags={},
            created_at="2026-08-01T00:00:00+00:00",
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "max-age-days",
                "check": "max_age_days",
                "severity": "medium",
                "max_age_days": 90,
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert findings == []


def test_max_age_days_is_skipped_without_created_at_evidence():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="unknown-age-bucket",
            region="us-east-1",
            tags={},
            created_at=None,
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "max-age-days",
                "check": "max_age_days",
                "severity": "medium",
                "max_age_days": 90,
            }
        ]
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert findings == []


# ---------------------------------------------------------------------------
# Exceptions and suppression -- a suppressed finding stays in the list with
# suppressed=True, it is never dropped. An expired exception must NOT
# suppress.
# ---------------------------------------------------------------------------


def test_unexpired_exception_suppresses_but_does_not_drop_the_finding():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="legacy-bucket",
            region="us-east-1",
            tags={},
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "required-tags",
                "check": "required_tags",
                "severity": "high",
                "required_tags": ["team_owner"],
            }
        ],
        exceptions=[
            {
                "resource_id": "legacy-bucket",
                "rule_id": "required-tags",
                "reason": "cleanup tracked in OPS-4021",
                "expires": "2026-12-31",
            }
        ],
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert findings[0].suppression_reason == "cleanup tracked in OPS-4021"


def test_expired_exception_does_not_suppress():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="legacy-bucket",
            region="us-east-1",
            tags={},
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "required-tags",
                "check": "required_tags",
                "severity": "high",
                "required_tags": ["team_owner"],
            }
        ],
        exceptions=[
            {
                "resource_id": "legacy-bucket",
                "rule_id": "required-tags",
                "reason": "cleanup tracked in OPS-4021",
                "expires": "2026-01-01",
            }
        ],
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert findings[0].suppressed is False
    assert findings[0].suppression_reason is None


def test_exception_only_matches_its_own_resource_and_rule():
    evidence = [
        ResourceEvidence(
            resource_type="s3-bucket",
            resource_id="other-bucket",
            region="us-east-1",
            tags={},
        )
    ]
    policy = _policy(
        rules=[
            {
                "id": "required-tags",
                "check": "required_tags",
                "severity": "high",
                "required_tags": ["team_owner"],
            }
        ],
        exceptions=[
            {
                "resource_id": "legacy-bucket",
                "rule_id": "required-tags",
                "reason": "not this bucket",
                "expires": "2026-12-31",
            }
        ],
    )

    findings = evaluate_policy(evidence, policy, now=NOW)

    assert len(findings) == 1
    assert findings[0].suppressed is False


# ---------------------------------------------------------------------------
# build_report -- summary counts by severity, active vs suppressed.
# ---------------------------------------------------------------------------


def test_build_report_counts_active_and_suppressed_separately():
    findings = [
        Finding(
            resource_id="a",
            resource_type="s3-bucket",
            rule_id="required-tags",
            severity="high",
            evidence="missing tags: team_owner",
        ),
        Finding(
            resource_id="b",
            resource_type="s3-bucket",
            rule_id="no-public-exposure",
            severity="critical",
            evidence="public exposure",
        ),
        Finding(
            resource_id="c",
            resource_type="s3-bucket",
            rule_id="required-tags",
            severity="high",
            evidence="missing tags: environment",
            suppressed=True,
            suppression_reason="excepted",
        ),
    ]

    report = build_report(findings)

    assert report["status"] == "violations"
    assert report["summary"]["total_findings"] == 3
    assert report["summary"]["active"] == 2
    assert report["summary"]["suppressed"] == 1
    assert report["summary"]["by_severity"] == {"high": 1, "critical": 1}


def test_build_report_status_is_ok_when_every_finding_is_suppressed():
    findings = [
        Finding(
            resource_id="a",
            resource_type="s3-bucket",
            rule_id="required-tags",
            severity="high",
            evidence="missing tags: team_owner",
            suppressed=True,
            suppression_reason="excepted",
        )
    ]

    report = build_report(findings)

    assert report["status"] == "ok"
    assert report["summary"]["active"] == 0
    assert report["summary"]["suppressed"] == 1


def test_build_report_on_no_findings_is_ok_and_empty():
    report = build_report([])

    assert report["status"] == "ok"
    assert report["summary"] == {
        "total_findings": 0,
        "active": 0,
        "suppressed": 0,
        "by_severity": {},
    }
    assert report["findings"] == []


# ---------------------------------------------------------------------------
# Live evidence-gathering against a real, running Floci -- proves
# gather_bucket_evidence() actually detects real seeded violations, not
# just that the pure detection logic above is correct.
# ---------------------------------------------------------------------------

FLOCI_ENDPOINT = "http://localhost:4566"
REGION = "us-east-1"


@pytest.fixture
def s3_client(require_floci):
    return get_aws_client("s3", region=REGION, endpoint_url=FLOCI_ENDPOINT)


@pytest.fixture
def bucket(s3_client):
    import uuid

    name = f"platformops-audit-test-{uuid.uuid4().hex[:12]}"
    s3_client.create_bucket(Bucket=name)
    yield name
    s3_client.delete_bucket(Bucket=name)


def test_bucket_uses_kms_encryption_is_false_for_the_real_sse_s3_default(
    s3_client, bucket
):
    # Floci turns on SSE-S3 (AES256) default encryption the moment a bucket
    # is created, matching real AWS behavior since January 2023 -- so a
    # freshly created bucket really is "encrypted", just not with a
    # customer-managed KMS key. Confirm that default is exactly what the
    # bucket already has before asserting the KMS-specific check reads it
    # correctly.
    existing = s3_client.get_bucket_encryption(Bucket=bucket)
    algorithm = existing["ServerSideEncryptionConfiguration"]["Rules"][0][
        "ApplyServerSideEncryptionByDefault"
    ]["SSEAlgorithm"]
    assert algorithm == "AES256"

    assert cloudaudit.bucket_uses_kms_encryption(s3_client, bucket) is False


def test_bucket_uses_kms_encryption_is_true_once_a_kms_key_is_set(s3_client, bucket):
    s3_client.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "aws:kms",
                        "KMSMasterKeyID": "alias/platformops-audit-demo",
                    }
                }
            ]
        },
    )

    assert cloudaudit.bucket_uses_kms_encryption(s3_client, bucket) is True


def test_bucket_is_public_is_false_by_default(s3_client, bucket):
    assert cloudaudit.bucket_is_public(s3_client, bucket) is False


def test_gather_bucket_evidence_reads_real_tags_and_region(s3_client, bucket):
    s3_client.put_bucket_tagging(
        Bucket=bucket, Tagging={"TagSet": [{"Key": "environment", "Value": "prod"}]}
    )
    records = list_s3_buckets(s3_client)
    matching = [r for r in records if r.resource_id == bucket]

    evidence = cloudaudit.gather_bucket_evidence(s3_client, matching)

    assert len(evidence) == 1
    assert evidence[0].tags == {"environment": "prod"}
    assert evidence[0].region == REGION
    assert evidence[0].encrypted is False
    assert evidence[0].public is False


def test_run_cloud_audit_detects_a_real_seeded_violation(s3_client, bucket, tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "rules:\n"
        "  - id: require-encryption\n"
        "    check: require_encryption\n"
        "    severity: high\n"
        "exceptions: []\n"
    )

    report = cloudaudit.run_cloud_audit(
        policy_path=policy_path, region=REGION, endpoint_url=FLOCI_ENDPOINT
    )

    assert report["status"] == "violations"
    matching = [f for f in report["findings"] if f["resource_id"] == bucket]
    assert len(matching) == 1
    assert matching[0]["rule_id"] == "require-encryption"
    assert "not using a customer-managed KMS key" in matching[0]["evidence"]
