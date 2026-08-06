from pathlib import Path

import botocore.exceptions
from kubernetes.config.config_exception import ConfigException

from platformops.incidentcontext import (
    build_timeline,
    evaluate_incident_context,
    gather_ci_evidence,
    gather_cloud_evidence,
    gather_kubernetes_evidence,
    gather_observability_evidence,
    gather_runbook_evidence,
    gather_service_evidence,
    gather_source_evidence,
)
from platformops.local_ops import GitStatusResult
from platformops.observability import ObservabilitySnapshot
from platformops.servicedef import GOOD_EXAMPLE

# ---------------------------------------------------------------------------
# gather_service_evidence
# ---------------------------------------------------------------------------


def test_gather_service_evidence_reports_valid_service(tmp_path, monkeypatch):
    import platformops.incidentcontext as incidentcontext
    from platformops.servicedef import validate_service

    monkeypatch.setattr(
        incidentcontext,
        "load_service_yaml",
        lambda path: validate_service(GOOD_EXAMPLE),
    )

    evidence = gather_service_evidence(tmp_path / "service.yaml")

    assert evidence == {
        "fetched": True,
        "valid": True,
        "service": validate_service(GOOD_EXAMPLE),
    }


def test_gather_service_evidence_reports_config_error(monkeypatch):
    import platformops.incidentcontext as incidentcontext
    from platformops.config import ConfigNotFoundError

    def raise_not_found(path):
        raise ConfigNotFoundError(f"config file not found: {path}")

    monkeypatch.setattr(incidentcontext, "load_service_yaml", raise_not_found)

    evidence = gather_service_evidence(Path("nope.yaml"))

    assert evidence["fetched"] is False
    assert "not found" in evidence["error"]


def test_gather_service_evidence_reports_validation_errors(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    monkeypatch.setattr(
        incidentcontext,
        "load_service_yaml",
        lambda path: [{"loc": ("name",), "msg": "field required"}],
    )

    evidence = gather_service_evidence(Path("bad.yaml"))

    assert evidence == {
        "fetched": True,
        "valid": False,
        "errors": [{"loc": ("name",), "msg": "field required"}],
    }


# ---------------------------------------------------------------------------
# gather_source_evidence
# ---------------------------------------------------------------------------


def test_gather_source_evidence_reports_clean_tree_with_commits(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    monkeypatch.setattr(
        incidentcontext,
        "git_status",
        lambda repo_path, timeout: GitStatusResult(
            repo_path=repo_path, clean=True, changed_files=[]
        ),
    )
    monkeypatch.setattr(
        incidentcontext,
        "git_log_detailed",
        lambda repo_path, n, timeout: [
            {
                "sha": "abc1234",
                "authored_at": "2026-08-01T10:00:00+00:00",
                "subject": "fix",
            }
        ],
    )

    evidence = gather_source_evidence(".")

    assert evidence["fetched"] is True
    assert evidence["clean"] is True
    assert evidence["commits"][0]["sha"] == "abc1234"


def test_gather_source_evidence_reports_unknown_on_git_error(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    monkeypatch.setattr(
        incidentcontext,
        "git_status",
        lambda repo_path, timeout: GitStatusResult(
            repo_path=repo_path,
            clean=False,
            changed_files=[],
            error="not a git repository",
        ),
    )

    evidence = gather_source_evidence("/tmp")

    assert evidence == {"fetched": False, "error": "not a git repository"}


# ---------------------------------------------------------------------------
# gather_ci_evidence
# ---------------------------------------------------------------------------


def test_gather_ci_evidence_filters_by_branch(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    monkeypatch.setattr(
        incidentcontext,
        "list_workflow_runs",
        lambda owner, repo, token, max_pages: [
            {"id": 1, "head_branch": "main", "created_at": "2026-08-01T10:00:00Z"},
            {"id": 2, "head_branch": "feature", "created_at": "2026-08-01T09:00:00Z"},
        ],
    )

    evidence = gather_ci_evidence("owner", "repo", branch="main")

    assert evidence["fetched"] is True
    assert [run["id"] for run in evidence["runs"]] == [1]


def test_gather_ci_evidence_reports_unknown_on_http_error(monkeypatch):
    import platformops.incidentcontext as incidentcontext
    from platformops.httpclient import EndpointUnreachableError

    def raise_unreachable(owner, repo, token, max_pages):
        raise EndpointUnreachableError("connection refused")

    monkeypatch.setattr(incidentcontext, "list_workflow_runs", raise_unreachable)

    evidence = gather_ci_evidence("owner", "repo")

    assert evidence["fetched"] is False
    assert "connection refused" in evidence["error"]


# ---------------------------------------------------------------------------
# gather_kubernetes_evidence
# ---------------------------------------------------------------------------


def test_gather_kubernetes_evidence_reports_unknown_when_cluster_unreachable(
    monkeypatch,
):
    import platformops.incidentcontext as incidentcontext

    def raise_config_exception(**kwargs):
        raise ConfigException("no kubeconfig found")

    monkeypatch.setattr(
        incidentcontext, "get_kubernetes_clients", raise_config_exception
    )

    evidence = gather_kubernetes_evidence(name="payments", namespace="default")

    assert evidence["fetched"] is False
    assert "kubeconfig" in evidence["error"]


def test_gather_kubernetes_evidence_reports_not_found_when_deployment_missing(
    monkeypatch,
):
    import platformops.incidentcontext as incidentcontext

    monkeypatch.setattr(
        incidentcontext, "get_kubernetes_clients", lambda **kwargs: (object(), object())
    )
    monkeypatch.setattr(
        incidentcontext,
        "inspect_workload",
        lambda apps_client, core_client, name, namespace: {
            "status": "error",
            "error": "404",
            "message": "deployment not found",
        },
    )

    evidence = gather_kubernetes_evidence(name="payments", namespace="default")

    assert evidence == {
        "fetched": True,
        "found": False,
        "error": "deployment not found",
    }


def test_gather_kubernetes_evidence_reports_healthy_workload(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    report = {
        "status": "ok",
        "deployment": {
            "name": "payments",
            "namespace": "default",
            "ready_replicas": 2,
            "desired_replicas": 2,
        },
        "rollout_state": "healthy",
        "pods": [],
        "warning_events": [],
    }
    monkeypatch.setattr(
        incidentcontext, "get_kubernetes_clients", lambda **kwargs: (object(), object())
    )
    monkeypatch.setattr(
        incidentcontext,
        "inspect_workload",
        lambda apps_client, core_client, name, namespace: report,
    )

    evidence = gather_kubernetes_evidence(name="payments", namespace="default")

    assert evidence == {"fetched": True, "found": True, "report": report}


# ---------------------------------------------------------------------------
# gather_cloud_evidence
# ---------------------------------------------------------------------------


def test_gather_cloud_evidence_reports_ok_report(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    ok_report = {
        "status": "ok",
        "summary": {"total_findings": 1, "active": 1, "suppressed": 0},
        "findings": [{"resource_id": "bucket-a"}],
    }
    monkeypatch.setattr(incidentcontext, "run_cloud_audit", lambda **kwargs: ok_report)

    evidence = gather_cloud_evidence(
        policy_path=Path("policy.yaml"), region="us-east-1"
    )

    assert evidence == {"fetched": True, "report": ok_report}


def test_gather_cloud_evidence_reports_unknown_on_internal_error_status(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    monkeypatch.setattr(
        incidentcontext,
        "run_cloud_audit",
        lambda **kwargs: {
            "status": "error",
            "error": "no-credentials",
            "message": "no creds",
        },
    )

    evidence = gather_cloud_evidence(
        policy_path=Path("policy.yaml"), region="us-east-1"
    )

    assert evidence == {"fetched": False, "error": "no creds"}


def test_gather_cloud_evidence_reports_unknown_when_endpoint_unreachable(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    def raise_connection_error(**kwargs):
        raise botocore.exceptions.EndpointConnectionError(
            endpoint_url="http://localhost:4566"
        )

    monkeypatch.setattr(incidentcontext, "run_cloud_audit", raise_connection_error)

    evidence = gather_cloud_evidence(
        policy_path=Path("policy.yaml"), region="us-east-1"
    )

    assert evidence["fetched"] is False
    assert "localhost:4566" in evidence["error"]


# ---------------------------------------------------------------------------
# gather_observability_evidence
# ---------------------------------------------------------------------------


def test_gather_observability_evidence_always_fetched(monkeypatch):
    import platformops.incidentcontext as incidentcontext

    snapshot = ObservabilitySnapshot(
        service="payments",
        correlation_id="req-1",
        metrics={"status": "OK", "detail": "1 sample"},
        logs={"status": "UNKNOWN", "detail": "unreachable"},
        alerts={"status": "OK", "detail": "no active alerts"},
        sources_ok=["metrics", "alerts"],
        sources_failed=["logs"],
    )
    monkeypatch.setattr(
        incidentcontext, "inspect_observability", lambda **kwargs: snapshot
    )

    evidence = gather_observability_evidence(
        service="payments",
        metrics_base_url="http://localhost:9090",
        metrics_query="q",
        logs_base_url="http://localhost:3100",
        alerts_base_url="http://localhost:9093",
    )

    assert evidence == {"fetched": True, "snapshot": snapshot}


# ---------------------------------------------------------------------------
# gather_runbook_evidence
# ---------------------------------------------------------------------------


def test_gather_runbook_evidence_finds_registered_service(tmp_path):
    registry = tmp_path / "incident.yaml"
    registry.write_text(
        "services:\n"
        "  payments:\n"
        "    runbook_url: https://runbooks.example/payments\n"
        "    slo:\n"
        "      target_percent: 99.9\n"
        "      window_days: 30\n"
    )

    evidence = gather_runbook_evidence(registry, "payments")

    assert evidence["fetched"] is True
    assert evidence["found"] is True
    assert evidence["runbook_url"] == "https://runbooks.example/payments"
    assert evidence["slo"]["target_percent"] == 99.9


def test_gather_runbook_evidence_reports_not_found_for_unregistered_service(tmp_path):
    registry = tmp_path / "incident.yaml"
    registry.write_text("services:\n  other-service:\n    runbook_url: https://x\n")

    evidence = gather_runbook_evidence(registry, "payments")

    assert evidence == {"fetched": True, "found": False}


def test_gather_runbook_evidence_reports_unknown_when_registry_missing(tmp_path):
    evidence = gather_runbook_evidence(tmp_path / "nope.yaml", "payments")

    assert evidence["fetched"] is False


# ---------------------------------------------------------------------------
# build_timeline
# ---------------------------------------------------------------------------


def test_build_timeline_merges_and_sorts_descending():
    source_evidence = {
        "fetched": True,
        "commits": [
            {
                "sha": "abc1234",
                "authored_at": "2026-08-01T09:00:00+00:00",
                "subject": "fix bug",
            }
        ],
    }
    ci_evidence = {
        "fetched": True,
        "runs": [
            {
                "id": 42,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-08-01T11:00:00Z",
            }
        ],
    }
    kubernetes_evidence = {
        "fetched": True,
        "found": True,
        "report": {
            "warning_events": [
                {
                    "reason": "BackOff",
                    "message": "restarting failed container",
                    "involved_object": "payments-abc",
                    "last_seen": "2026-08-01T10:00:00+00:00",
                }
            ]
        },
    }

    timeline = build_timeline(
        source_evidence=source_evidence,
        ci_evidence=ci_evidence,
        kubernetes_evidence=kubernetes_evidence,
    )

    assert [entry["source"] for entry in timeline] == ["ci", "kubernetes", "git"]


def test_build_timeline_skips_sources_that_were_not_fetched():
    timeline = build_timeline(
        source_evidence={"fetched": False, "error": "no git"},
        ci_evidence={"fetched": False, "error": "unreachable"},
        kubernetes_evidence={"fetched": False, "error": "unreachable"},
    )

    assert timeline == []


# ---------------------------------------------------------------------------
# evaluate_incident_context -- the honest-degradation contract
# ---------------------------------------------------------------------------


def _full_success_evidences():
    from platformops.servicedef import validate_service

    return {
        "service_evidence": {
            "fetched": True,
            "valid": True,
            "service": validate_service(GOOD_EXAMPLE),
        },
        "source_evidence": {
            "fetched": True,
            "clean": True,
            "changed_files": [],
            "commits": [
                {
                    "sha": "abc1234",
                    "authored_at": "2026-08-01T09:00:00+00:00",
                    "subject": "fix",
                }
            ],
        },
        "ci_evidence": {
            "fetched": True,
            "runs": [
                {
                    "id": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": "2026-08-01T09:30:00Z",
                }
            ],
        },
        "kubernetes_evidence": {
            "fetched": True,
            "found": True,
            "report": {
                "status": "ok",
                "deployment": {
                    "name": "checkout-api",
                    "namespace": "checkout",
                    "ready_replicas": 2,
                    "desired_replicas": 2,
                },
                "rollout_state": "healthy",
                "warning_events": [],
            },
        },
        "cloud_evidence": {
            "fetched": True,
            "report": {
                "status": "ok",
                "summary": {"total_findings": 0, "active": 0, "suppressed": 0},
                "findings": [],
            },
        },
        "observability_evidence": {
            "fetched": True,
            "snapshot": ObservabilitySnapshot(
                service="checkout-api",
                correlation_id="req-1",
                metrics={"status": "OK", "detail": "1 sample"},
                logs={"status": "OK", "detail": "2 log lines"},
                alerts={"status": "OK", "detail": "no active alerts"},
                sources_ok=["metrics", "logs", "alerts"],
                sources_failed=[],
            ),
        },
        "runbook_evidence": {
            "fetched": True,
            "found": True,
            "runbook_url": "https://runbooks.example/checkout-api",
            "slo": {"target_percent": 99.9, "window_days": 30},
        },
    }


def test_evaluate_incident_context_full_success_has_no_failed_sources():
    report = evaluate_incident_context(
        service="checkout-api", **_full_success_evidences()
    )

    assert report.sources_failed == []
    assert set(report.sources_ok) == {
        "ownership",
        "source_changes",
        "ci",
        "kubernetes",
        "cloud",
        "observability",
        "runbook",
    }
    assert report.kubernetes["status"] == "healthy"
    assert report.ownership["status"] == "OK"


def test_evaluate_incident_context_reports_partial_outage_honestly():
    evidences = _full_success_evidences()
    evidences["kubernetes_evidence"] = {
        "fetched": False,
        "error": "Unable to connect to the server",
    }
    evidences["cloud_evidence"] = {"fetched": False, "error": "connection refused"}

    report = evaluate_incident_context(service="checkout-api", **evidences)

    assert set(report.sources_failed) == {"kubernetes", "cloud"}
    assert report.kubernetes["status"] == "UNKNOWN"
    assert report.cloud["status"] == "UNKNOWN"
    # everything else still answered
    assert report.ownership["status"] == "OK"
    assert report.source_changes["status"] == "CLEAN"
    assert report.ci["status"] == "OK"
    assert report.observability["status"] == "OK"
    assert report.runbook["status"] == "INFO"


def test_evaluate_incident_context_never_calls_a_mutating_function():
    import inspect

    import platformops.incidentcontext as incidentcontext

    source = inspect.getsource(incidentcontext)
    for forbidden in (
        "restart_execute",
        "execute_remediation",
        "patch_namespaced",
        "delete_",
    ):
        assert forbidden not in source
