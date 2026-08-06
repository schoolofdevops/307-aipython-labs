"""Tests for platformops.api -- the FastAPI wrapper around this project's existing domain functions.

Every route handler under test here is a thin wrapper: the test proves the
route calls the SAME function the CLI already calls (by monkeypatching that
function on `platformops.api`, exactly the way `test_cli.py` monkeypatches
`platformops.cli`), not a reimplementation of any domain logic. Domain logic
itself (service validation, release-readiness, incident context,
remediation planning) is already covered by test_servicedef.py,
test_releasecheck.py, test_incidentcontext.py and test_cloudremediate.py --
this file is only responsible for the HTTP layer: request/response shape,
the X-API-Key gate, and the audit log.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import platformops.api as api_module
from platformops.cloudremediate import RemediationAction, RemediationPlan
from platformops.incidentcontext import IncidentContextReport
from platformops.releasecheck import ReleaseReadinessReport

API_KEY = "test-suite-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}

GOOD_YAML_TEXT = (
    "name: checkout-api\n"
    "repository: github.com/example/checkout-api\n"
    "environment: prod\n"
    "team_owner: payments-team\n"
    "kubernetes_namespace: checkout\n"
    "deployment_name: checkout-api\n"
    'aws_account: "111122223333"\n'
    "region: us-east-1\n"
    "observability:\n"
    "  dashboard_url: https://grafana.example.com/d/checkout-api\n"
    "  alert_channel: '#checkout-alerts'\n"
)

BAD_YAML_TEXT = (
    "name: checkout-api\n"
    "repository: github.com/example/checkout-api\n"
    "environment: prod\n"
    "team_owner: payments-team\n"
    "kubernetes_namespace: checkout\n"
    'aws_account: "111122223333"\n'
    "region: us-east-1\n"
    "observability:\n"
    "  dashboard_url: https://grafana.example.com/d/checkout-api\n"
    "  alert_channel: '#checkout-alerts'\n"
)


@pytest.fixture
def audit_log_path(tmp_path):
    return tmp_path / "api-audit.jsonl"


@pytest.fixture
def client(audit_log_path):
    app = api_module.create_app(
        api_keys={API_KEY: "test-caller"}, audit_log_path=audit_log_path
    )
    with TestClient(app) as test_client:
        yield test_client


def _audit_records(audit_log_path):
    if not audit_log_path.exists():
        return []
    return [json.loads(line) for line in audit_log_path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# GET /health -- no API key required
# ---------------------------------------------------------------------------


def test_health_returns_ok_without_an_api_key(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["timestamp"]


def test_health_request_is_still_audited(client, audit_log_path):
    client.get("/health")

    records = [r for r in _audit_records(audit_log_path) if r.get("path") == "/health"]
    assert records
    assert records[-1]["status_code"] == 200
    assert records[-1]["method"] == "GET"


# ---------------------------------------------------------------------------
# The X-API-Key gate -- every other route requires it
# ---------------------------------------------------------------------------


def test_protected_route_without_api_key_is_401(client):
    response = client.get(
        "/release-readiness",
        params={"service": "payments", "owner": "example", "pr": 1},
    )

    assert response.status_code == 401


def test_protected_route_with_wrong_api_key_is_401(client):
    response = client.get(
        "/release-readiness",
        params={"service": "payments", "owner": "example", "pr": 1},
        headers={"X-API-Key": "not-the-right-key"},
    )

    assert response.status_code == 401


def test_a_rejected_request_is_still_audited_as_anonymous(client, audit_log_path):
    client.get(
        "/release-readiness",
        params={"service": "payments", "owner": "example", "pr": 1},
    )

    records = [
        r
        for r in _audit_records(audit_log_path)
        if r.get("path") == "/release-readiness"
    ]
    assert records
    assert records[-1]["status_code"] == 401
    assert records[-1]["caller"] == "anonymous"


# ---------------------------------------------------------------------------
# POST /services/validate -- wraps config.load_yaml_dict() + servicedef.validate_service(),
# the exact same two calls the CLI's `validate` command makes.
# ---------------------------------------------------------------------------


def test_services_validate_ok(client, tmp_path):
    service_path = tmp_path / "service.yaml"
    service_path.write_text(GOOD_YAML_TEXT)

    response = client.post(
        "/services/validate", json={"path": str(service_path)}, headers=AUTH_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]["name"] == "checkout-api"


def test_services_validate_reports_field_errors(client, tmp_path):
    service_path = tmp_path / "bad.yaml"
    service_path.write_text(BAD_YAML_TEXT)

    response = client.post(
        "/services/validate", json={"path": str(service_path)}, headers=AUTH_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert any(err["loc"] == ["deployment_name"] for err in body["errors"])


def test_services_validate_missing_file_is_404(client, tmp_path):
    response = client.post(
        "/services/validate",
        json={"path": str(tmp_path / "no-such-file.yaml")},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404


def test_services_validate_requires_api_key(client, tmp_path):
    service_path = tmp_path / "service.yaml"
    service_path.write_text(GOOD_YAML_TEXT)

    response = client.post("/services/validate", json={"path": str(service_path)})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /release-readiness -- a thin wrapper around releasecheck.run_release_check(),
# the exact function the CLI's `release-check` command calls.
# ---------------------------------------------------------------------------


def _fake_release_report(verdict="ready", sources_failed=None):
    return ReleaseReadinessReport(
        service="payments",
        branch="main",
        verdict=verdict,
        pr={"status": "PASS", "detail": "PR #42 merged"},
        ci={"status": "PASS", "detail": "latest run succeeded"},
        checks={"status": "PASS", "detail": "all checks green"},
        artifacts={"status": "PASS", "detail": "payments-build present"},
        release={"status": "PASS", "detail": "v1.2.3 published"},
        deployment={"status": "PASS", "detail": "production deployment recorded"},
        sources_ok=["pr", "ci", "checks", "artifacts", "release", "deployment"],
        sources_failed=sources_failed or [],
    )


def test_release_readiness_calls_run_release_check_and_shapes_the_response(
    client, monkeypatch
):
    captured = {}

    def fake_run_release_check(**kwargs):
        captured.update(kwargs)
        return _fake_release_report()

    monkeypatch.setattr(api_module, "run_release_check", fake_run_release_check)

    response = client.get(
        "/release-readiness",
        params={"service": "payments", "owner": "example", "pr": 42},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "ready"
    assert body["service"] == "payments"
    assert captured["service"] == "payments"
    assert captured["owner"] == "example"
    assert captured["pr"] == 42
    assert captured["repo"] == "payments"
    assert captured["branch"] == "main"


def test_release_readiness_reports_not_ready_with_http_200(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "run_release_check",
        lambda **kwargs: _fake_release_report(
            verdict="not_ready", sources_failed=["checks"]
        ),
    )

    response = client.get(
        "/release-readiness",
        params={"service": "payments", "owner": "example", "pr": 42},
        headers=AUTH_HEADERS,
    )

    # HTTP 200: the server successfully produced a report. "not_ready" is
    # business data carried in the body's verdict field, not a protocol
    # error -- see the lesson/deep-dive for why this endpoint never maps a
    # bad verdict onto a 4xx/5xx status code.
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "not_ready"
    assert body["sources_failed"] == ["checks"]


# ---------------------------------------------------------------------------
# GET /incident-context -- a thin wrapper around
# incidentcontext.collect_incident_context(), the exact function the CLI's
# `incident-collect` command calls.
# ---------------------------------------------------------------------------


def _fake_incident_report(sources_failed=None):
    sources_failed = sources_failed or []
    all_sections = [
        "ownership",
        "source_changes",
        "ci",
        "kubernetes",
        "cloud",
        "observability",
        "runbook",
    ]
    return IncidentContextReport(
        service="payments",
        ownership={"status": "OK", "detail": "payments [prod/us-east-1]"},
        source_changes={
            "status": "CLEAN",
            "detail": "working tree clean",
            "commits": [],
        },
        ci={"status": "OK", "detail": "latest run #1: completed/success", "runs": []},
        kubernetes={"status": "healthy", "detail": "payments (payments): ready=2/2"},
        cloud={"status": "OK", "detail": "0 finding(s)", "findings": []},
        observability={
            "status": "OK",
            "detail": "metrics, logs and alerts all reachable",
        },
        runbook={
            "status": "INFO",
            "detail": "runbook: https://runbooks.example/payments",
        },
        timeline=[],
        sources_ok=[s for s in all_sections if s not in sources_failed],
        sources_failed=sources_failed,
    )


def test_incident_context_calls_collect_incident_context_and_shapes_the_response(
    client, monkeypatch
):
    captured = {}

    def fake_collect_incident_context(**kwargs):
        captured.update(kwargs)
        return _fake_incident_report()

    monkeypatch.setattr(
        api_module, "collect_incident_context", fake_collect_incident_context
    )

    response = client.get(
        "/incident-context",
        params={"service": "payments", "owner": "example"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["kubernetes"]["status"] == "healthy"
    assert captured["service"] == "payments"
    assert captured["owner"] == "example"
    assert captured["repo"] == "payments"
    assert captured["deployment_name"] == "payments"


def test_incident_context_reports_partial_status_with_http_200(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "collect_incident_context",
        lambda **kwargs: _fake_incident_report(sources_failed=["kubernetes"]),
    )

    response = client.get(
        "/incident-context",
        params={"service": "payments", "owner": "example"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["sources_failed"] == ["kubernetes"]


# ---------------------------------------------------------------------------
# POST /remediations/plan -- ALWAYS a dry run. This module never wires an
# execute endpoint -- cloudremediate.execute_remediation_plan() is never
# imported into platformops.api at all (see the module-level import list and
# the Deep Dive's grep proof).
# ---------------------------------------------------------------------------


def _fake_report_file(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "resource_id": "demo-bucket",
                        "resource_type": "s3-bucket",
                        "rule_id": "required-tags",
                        "severity": "high",
                        "evidence": "missing tags: team_owner",
                        "suppressed": False,
                        "suppression_reason": None,
                    }
                ]
            }
        )
    )
    return report_path


def _fake_remediable_plan():
    return RemediationPlan(
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


def test_remediations_plan_calls_build_remediation_plan_never_execute(
    client, monkeypatch, tmp_path
):
    report_path = _fake_report_file(tmp_path)
    monkeypatch.setattr(api_module, "get_aws_client", lambda *a, **k: object())
    monkeypatch.setattr(
        api_module, "build_remediation_plan", lambda *a, **k: _fake_remediable_plan()
    )

    response = client.post(
        "/remediations/plan",
        json={
            "report_path": str(report_path),
            "finding_id": "demo-bucket:required-tags",
            "region": "us-east-1",
        },
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["plan"]["status"] == "remediable"
    assert body["plan"]["action"]["api_call"] == "put_bucket_tagging"


def test_remediations_plan_unknown_finding_is_404(client, monkeypatch, tmp_path):
    report_path = _fake_report_file(tmp_path)
    monkeypatch.setattr(api_module, "get_aws_client", lambda *a, **k: object())

    response = client.post(
        "/remediations/plan",
        json={
            "report_path": str(report_path),
            "finding_id": "no-such:required-tags",
            "region": "us-east-1",
        },
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404


def test_remediations_plan_missing_report_file_is_404(client, tmp_path):
    response = client.post(
        "/remediations/plan",
        json={
            "report_path": str(tmp_path / "no-such-report.json"),
            "finding_id": "demo-bucket:required-tags",
            "region": "us-east-1",
        },
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404


def test_api_module_never_imports_execute_remediation(tmp_path):
    """`POST /remediations/plan` is plan-only by construction -- this module
    never even imports the execute-side function, so there is no code path
    inside `platformops.api` that could ever mutate a cloud resource."""
    assert not hasattr(api_module, "execute_remediation_plan")
    assert not hasattr(api_module, "execute_remediation_batch")


# ---------------------------------------------------------------------------
# The audit log records a real, successful call too.
# ---------------------------------------------------------------------------


def test_a_successful_authenticated_call_is_audited_with_the_caller_identity(
    client, monkeypatch, audit_log_path
):
    monkeypatch.setattr(
        api_module, "run_release_check", lambda **kwargs: _fake_release_report()
    )

    client.get(
        "/release-readiness",
        params={"service": "payments", "owner": "example", "pr": 42},
        headers=AUTH_HEADERS,
    )

    records = [
        r
        for r in _audit_records(audit_log_path)
        if r.get("path") == "/release-readiness"
    ]
    assert records
    assert records[-1]["caller"] == "test-caller"
    assert records[-1]["status_code"] == 200


# ---------------------------------------------------------------------------
# load_api_key_config() -- the YAML loader for api-keys.example.yaml
# ---------------------------------------------------------------------------


def test_load_api_key_config_reads_keys_mapping(tmp_path):
    path = tmp_path / "api-keys.yaml"
    path.write_text("keys:\n  abc123: ci-bot\n  def456: sre-oncall\n")

    config = api_module.load_api_key_config(path)

    assert config == {"abc123": "ci-bot", "def456": "sre-oncall"}


def test_load_api_key_config_defaults_to_empty_mapping(tmp_path):
    path = tmp_path / "api-keys.yaml"
    path.write_text("keys: {}\n")

    config = api_module.load_api_key_config(path)

    assert config == {}
