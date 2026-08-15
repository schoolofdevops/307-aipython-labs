import json
from pathlib import Path

import httpx
import respx
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from platformops.ai_release_readiness import (
    check_ai_release_readiness,
    evaluate_ai_release_readiness,
    gather_evaluation_evidence,
    gather_kubernetes_evidence,
)
from platformops.aiservice import GOOD_AI_EXAMPLE, validate_ai_service

FIXTURES = Path(__file__).parent / "fixtures" / "mlflow"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


MLFLOW_URL = "http://localhost:5000"
MODEL_NAME = "support-assistant-intent-classifier"
RUN_ID = "a1b2c3d4e5f647a8b9c0d1e2f3a4b5c6"


# ---------------------------------------------------------------------------
# gather_evaluation_evidence -- respx-mocked, reuses get_run() (M33).
# ---------------------------------------------------------------------------


@respx.mock
def test_gather_evaluation_evidence_reports_a_finished_run_and_its_metric():
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/runs/get").mock(
        return_value=httpx.Response(200, json=_fixture("run_finished.json"))
    )

    evidence = gather_evaluation_evidence(MLFLOW_URL, RUN_ID, metric_key="accuracy")

    assert evidence["fetched"] is True
    assert evidence["status"] == "FINISHED"
    assert evidence["metric_key"] == "accuracy"
    assert evidence["metric_value"] == 0.94


@respx.mock
def test_gather_evaluation_evidence_reports_a_failed_run_as_fetched_not_unreachable():
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/runs/get").mock(
        return_value=httpx.Response(200, json=_fixture("run_failed.json"))
    )

    evidence = gather_evaluation_evidence(
        MLFLOW_URL, "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7", metric_key="accuracy"
    )

    assert evidence["fetched"] is True
    assert evidence["status"] == "FAILED"
    assert evidence["metric_value"] is None


@respx.mock
def test_gather_evaluation_evidence_degrades_honestly_when_unreachable(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/runs/get").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    evidence = gather_evaluation_evidence(MLFLOW_URL, RUN_ID, metric_key="accuracy")

    assert evidence["fetched"] is False
    assert "unreachable" in evidence["error"]


# ---------------------------------------------------------------------------
# gather_kubernetes_evidence -- monkeypatched get_kubernetes_clients() and
# the two kubernetes_inspect reads, the same style
# incidentcontext.gather_kubernetes_evidence()'s own tests already use.
# ---------------------------------------------------------------------------


def test_gather_kubernetes_evidence_reports_unknown_when_cluster_unreachable(
    monkeypatch,
):
    import platformops.ai_release_readiness as ai_release_readiness

    def raise_config_exception(**kwargs):
        raise ConfigException("no kubeconfig found")

    monkeypatch.setattr(
        ai_release_readiness, "get_kubernetes_clients", raise_config_exception
    )

    evidence = gather_kubernetes_evidence(
        name="support-assistant", namespace="ml-serving"
    )

    assert evidence["fetched"] is False
    assert "kubeconfig" in evidence["error"]


def test_gather_kubernetes_evidence_reports_not_found_for_a_missing_deployment(
    monkeypatch,
):
    import platformops.ai_release_readiness as ai_release_readiness

    monkeypatch.setattr(
        ai_release_readiness,
        "get_kubernetes_clients",
        lambda **kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        ai_release_readiness,
        "get_deployment_status",
        lambda apps_client, name, namespace: (_ for _ in ()).throw(
            ApiException(status=404, reason="Not Found")
        ),
    )

    evidence = gather_kubernetes_evidence(
        name="support-assistant", namespace="ml-serving"
    )

    assert evidence == {"fetched": True, "found": False}


def test_gather_kubernetes_evidence_reports_a_healthy_deployment_with_labels_and_resources(
    monkeypatch,
):
    import platformops.ai_release_readiness as ai_release_readiness
    from platformops.kubernetes_inspect import (
        DeploymentResourceProfile,
        DeploymentStatus,
    )

    status = DeploymentStatus(
        name="support-assistant",
        namespace="ml-serving",
        desired_replicas=2,
        ready_replicas=2,
        available_replicas=2,
        updated_replicas=2,
        unavailable_replicas=None,
        conditions=[],
        selector={"app": "support-assistant"},
    )
    profile = DeploymentResourceProfile(
        name="support-assistant",
        namespace="ml-serving",
        labels={"app": "support-assistant", "model-version": "4"},
        container_resource_requests={
            "support-assistant": {"cpu": "2", "memory": "8Gi", "nvidia.com/gpu": "1"}
        },
    )
    monkeypatch.setattr(
        ai_release_readiness,
        "get_kubernetes_clients",
        lambda **kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        ai_release_readiness,
        "get_deployment_status",
        lambda apps_client, name, namespace: status,
    )
    monkeypatch.setattr(
        ai_release_readiness,
        "get_deployment_resource_profile",
        lambda apps_client, name, namespace: profile,
    )

    evidence = gather_kubernetes_evidence(
        name="support-assistant", namespace="ml-serving"
    )

    assert evidence["fetched"] is True
    assert evidence["found"] is True
    assert evidence["rollout_state"] == "healthy"
    assert evidence["labels"]["model-version"] == "4"
    assert evidence["resource_requests"] == {
        "support-assistant": {"cpu": "2", "memory": "8Gi", "nvidia.com/gpu": "1"}
    }


# ---------------------------------------------------------------------------
# evaluate_ai_release_readiness -- pure, hand-built evidence, no network.
# ---------------------------------------------------------------------------

_MODEL_READY = {
    "fetched": True,
    "status": "READY",
    "current_stage": "Production",
    "run_id": RUN_ID,
}
_MODEL_FAILED = {
    "fetched": True,
    "status": "FAILED_REGISTRATION",
    "current_stage": "None",
    "run_id": RUN_ID,
}
_MODEL_UNREACHABLE = {"fetched": False, "error": "boom"}

_EVAL_PASSING = {
    "fetched": True,
    "status": "FINISHED",
    "metric_key": "accuracy",
    "metric_value": 0.94,
}
_EVAL_BELOW_THRESHOLD = {
    "fetched": True,
    "status": "FINISHED",
    "metric_key": "accuracy",
    "metric_value": 0.5,
}
_EVAL_FAILED_RUN = {
    "fetched": True,
    "status": "FAILED",
    "metric_key": "accuracy",
    "metric_value": None,
}

_ENDPOINT_HEALTHY = {
    "checked": True,
    "ok": True,
    "status_code": 200,
    "latency_ms": 42.0,
    "error": None,
}
_ENDPOINT_NOT_APPLICABLE = {
    "checked": False,
    "reason": "batch inference has no standing endpoint to check",
}

_K8S_HEALTHY_MATCHING = {
    "fetched": True,
    "found": True,
    "rollout_state": "healthy",
    "desired_replicas": 2,
    "ready_replicas": 2,
    "labels": {"model-version": "4"},
    "resource_requests": {"support-assistant": {"nvidia.com/gpu": "1"}},
}
_K8S_HEALTHY_MISMATCHED_VERSION = {
    **_K8S_HEALTHY_MATCHING,
    "labels": {"model-version": "3"},
}
_K8S_NO_RESOURCE_REQUESTS = {
    **_K8S_HEALTHY_MATCHING,
    "resource_requests": {"support-assistant": {}},
}
_K8S_DEGRADED = {**_K8S_HEALTHY_MATCHING, "rollout_state": "degraded"}
_K8S_UNREACHABLE = {"fetched": False, "error": "cluster unreachable"}


def test_evaluate_ai_release_readiness_reports_ready_when_everything_checks_out():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_HEALTHY_MATCHING,
    )

    assert report.model["status"] == "PASS"
    assert report.evaluation["status"] == "PASS"
    assert report.endpoint["status"] == "PASS"
    assert report.deployment["status"] == "PASS"
    assert report.version_match["status"] == "PASS"
    assert report.model_quality_verdict == "ready"
    assert report.infrastructure_verdict == "ready"
    assert report.verdict == "ready"
    assert report.deployed_version == "4"


def test_evaluate_ai_release_readiness_flags_low_evaluation_score_as_a_quality_problem():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_BELOW_THRESHOLD,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_HEALTHY_MATCHING,
    )

    assert report.evaluation["status"] == "FAIL"
    assert report.model_quality_verdict == "not_ready"
    # infra is fine -- this is a model-quality problem, not an infra one.
    assert report.infrastructure_verdict == "ready"
    assert report.verdict == "not_ready"


def test_evaluate_ai_release_readiness_flags_an_unhealthy_rollout_as_an_infra_problem():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_DEGRADED,
    )

    assert report.deployment["status"] == "FAIL"
    # quality is fine -- this is an infra problem, not a model-quality one.
    assert report.model_quality_verdict == "ready"
    assert report.infrastructure_verdict == "not_ready"
    assert report.verdict == "not_ready"


def test_evaluate_ai_release_readiness_flags_a_deployment_with_no_resource_requests():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_NO_RESOURCE_REQUESTS,
    )

    assert report.deployment["status"] == "FAIL"
    assert "resource" in report.deployment["detail"]
    assert report.infrastructure_verdict == "not_ready"


def test_evaluate_ai_release_readiness_flags_a_deployed_version_mismatch():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_HEALTHY_MISMATCHED_VERSION,
    )

    assert report.version_match["status"] == "FAIL"
    assert "3" in report.version_match["detail"]
    assert "4" in report.version_match["detail"]
    # the deployed (rollback-relevant) version is still surfaced, even on a mismatch.
    assert report.deployed_version == "3"
    assert report.infrastructure_verdict == "not_ready"


def test_evaluate_ai_release_readiness_marks_endpoint_not_applicable_for_batch_inference():
    report = evaluate_ai_release_readiness(
        service="nightly-fraud-scorer",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_NOT_APPLICABLE,
        kubernetes_evidence=_K8S_HEALTHY_MATCHING,
    )

    assert report.endpoint["status"] == "NOT_APPLICABLE"
    assert report.infrastructure_verdict == "ready"
    assert report.verdict == "ready"
    assert "endpoint" not in report.sources_ok
    assert "endpoint" not in report.sources_failed


def test_evaluate_ai_release_readiness_reports_a_source_it_could_not_fetch_at_all():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_UNREACHABLE,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_HEALTHY_MATCHING,
    )

    assert report.model["status"] == "UNKNOWN"
    assert "model" in report.sources_failed
    assert "model" not in report.sources_ok
    assert report.model_quality_verdict == "not_ready"
    assert report.verdict == "not_ready"


def test_evaluate_ai_release_readiness_reports_unknown_when_kubernetes_is_unreachable():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_UNREACHABLE,
    )

    assert report.deployment["status"] == "UNKNOWN"
    assert report.version_match["status"] == "UNKNOWN"
    assert report.deployed_version is None
    assert "kubernetes" in report.sources_failed
    assert report.infrastructure_verdict == "not_ready"
    assert report.verdict == "not_ready"


def test_evaluate_ai_release_readiness_fails_evaluation_when_the_run_itself_failed():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_READY,
        evaluation_evidence=_EVAL_FAILED_RUN,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_HEALTHY_MATCHING,
    )

    assert report.evaluation["status"] == "FAIL"
    assert report.model_quality_verdict == "not_ready"


def test_evaluate_ai_release_readiness_reports_failed_model_registration():
    report = evaluate_ai_release_readiness(
        service="support-assistant",
        model_version="4",
        eval_threshold=0.9,
        model_evidence=_MODEL_FAILED,
        evaluation_evidence=_EVAL_PASSING,
        endpoint_evidence=_ENDPOINT_HEALTHY,
        kubernetes_evidence=_K8S_HEALTHY_MATCHING,
    )

    assert report.model["status"] == "FAIL"
    assert report.model_quality_verdict == "not_ready"


# ---------------------------------------------------------------------------
# check_ai_release_readiness -- the thin orchestrator, fetch chained through
# evaluate, the same shape inspect_ai_workload() (M33) already established.
# ---------------------------------------------------------------------------


@respx.mock
def test_check_ai_release_readiness_chains_the_models_run_id_into_the_evaluation_lookup(
    monkeypatch,
):
    import platformops.ai_release_readiness as ai_release_readiness
    from platformops.kubernetes_inspect import (
        DeploymentResourceProfile,
        DeploymentStatus,
    )

    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/get").mock(
        return_value=httpx.Response(200, json=_fixture("model_version_ready.json"))
    )
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/runs/get").mock(
        return_value=httpx.Response(200, json=_fixture("run_finished.json"))
    )
    status = DeploymentStatus(
        name="support-assistant",
        namespace="ml-serving",
        desired_replicas=1,
        ready_replicas=1,
        available_replicas=1,
        updated_replicas=1,
        unavailable_replicas=None,
        conditions=[],
        selector={"app": "support-assistant"},
    )
    profile = DeploymentResourceProfile(
        name="support-assistant",
        namespace="ml-serving",
        labels={"model-version": "4"},
        container_resource_requests={"support-assistant": {"nvidia.com/gpu": "1"}},
    )
    monkeypatch.setattr(
        ai_release_readiness,
        "get_kubernetes_clients",
        lambda **kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        ai_release_readiness,
        "get_deployment_status",
        lambda apps_client, name, namespace: status,
    )
    monkeypatch.setattr(
        ai_release_readiness,
        "get_deployment_resource_profile",
        lambda apps_client, name, namespace: profile,
    )
    service = validate_ai_service(GOOD_AI_EXAMPLE)

    report = check_ai_release_readiness(
        service,
        mlflow_base_url=MLFLOW_URL,
        namespace="ml-serving",
        deployment_name="support-assistant",
        eval_threshold=0.9,
        endpoint_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    assert report.model["status"] == "PASS"
    assert report.evaluation["status"] == "PASS"
    assert report.version_match["status"] == "PASS"
    assert report.verdict == "ready"
