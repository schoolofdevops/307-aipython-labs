import json
from pathlib import Path

import httpx
import respx

from platformops.ai_inspect import (
    evaluate_ai_workload,
    gather_endpoint_evidence,
    gather_model_evidence,
    gather_run_evidence,
    inspect_ai_workload,
)
from platformops.aiservice import GOOD_AI_EXAMPLE, validate_ai_service

FIXTURES = Path(__file__).parent / "fixtures" / "mlflow"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


MLFLOW_URL = "http://localhost:5000"
MODEL_NAME = "support-assistant-intent-classifier"
RUN_ID = "a1b2c3d4e5f647a8b9c0d1e2f3a4b5c6"


# ---------------------------------------------------------------------------
# gather_*_evidence -- respx/MockTransport-mocked, one per source.
# ---------------------------------------------------------------------------


@respx.mock
def test_gather_model_evidence_reports_a_ready_version():
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/get").mock(
        return_value=httpx.Response(200, json=_fixture("model_version_ready.json"))
    )

    evidence = gather_model_evidence(MLFLOW_URL, MODEL_NAME, "4")

    assert evidence["fetched"] is True
    assert evidence["status"] == "READY"
    assert evidence["run_id"] == RUN_ID


@respx.mock
def test_gather_model_evidence_degrades_honestly_when_unreachable(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/get").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    evidence = gather_model_evidence(MLFLOW_URL, MODEL_NAME, "4")

    assert evidence["fetched"] is False
    assert "unreachable" in evidence["error"]


@respx.mock
def test_gather_run_evidence_reports_a_finished_run():
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/runs/get").mock(
        return_value=httpx.Response(200, json=_fixture("run_finished.json"))
    )

    evidence = gather_run_evidence(MLFLOW_URL, RUN_ID)

    assert evidence["fetched"] is True
    assert evidence["status"] == "FINISHED"
    assert evidence["metrics"]["accuracy"] == 0.94


@respx.mock
def test_gather_run_evidence_reports_a_failed_run_as_fetched_not_unreachable():
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/runs/get").mock(
        return_value=httpx.Response(200, json=_fixture("run_failed.json"))
    )

    evidence = gather_run_evidence(MLFLOW_URL, "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7")

    assert evidence["fetched"] is True
    assert evidence["status"] == "FAILED"


def test_gather_endpoint_evidence_checks_a_reachable_online_endpoint():
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    evidence = gather_endpoint_evidence(
        "https://support-assistant.internal.example.com/v1/predict",
        "online",
        transport=transport,
    )

    assert evidence["checked"] is True
    assert evidence["ok"] is True
    assert evidence["status_code"] == 200


def test_gather_endpoint_evidence_skips_a_batch_workload_with_no_standing_endpoint():
    evidence = gather_endpoint_evidence(
        "https://support-assistant.internal.example.com/v1/predict", "batch"
    )

    assert evidence["checked"] is False
    assert "batch" in evidence["reason"]


# ---------------------------------------------------------------------------
# evaluate_ai_workload -- pure, hand-built evidence, no network.
# ---------------------------------------------------------------------------

_MODEL_READY = {
    "fetched": True,
    "status": "READY",
    "current_stage": "Production",
    "run_id": RUN_ID,
}
_MODEL_PENDING = {
    "fetched": True,
    "status": "PENDING_REGISTRATION",
    "current_stage": "None",
    "run_id": RUN_ID,
}
_MODEL_UNREACHABLE = {"fetched": False, "error": "boom"}

_RUN_FINISHED = {"fetched": True, "status": "FINISHED", "metrics": {"accuracy": 0.94}}
_RUN_FAILED = {"fetched": True, "status": "FAILED", "metrics": {}}

_ENDPOINT_HEALTHY = {
    "checked": True,
    "ok": True,
    "status_code": 200,
    "latency_ms": 42.0,
    "error": None,
}
_ENDPOINT_UNHEALTHY = {
    "checked": True,
    "ok": False,
    "status_code": 503,
    "latency_ms": 10.0,
    "error": None,
}
_ENDPOINT_UNREACHABLE = {
    "checked": True,
    "ok": False,
    "status_code": None,
    "latency_ms": None,
    "error": "connection failed: refused",
}
_ENDPOINT_NOT_APPLICABLE = {
    "checked": False,
    "reason": "batch inference has no standing endpoint to check",
}


def test_evaluate_ai_workload_reports_healthy_when_everything_checks_out():
    report = evaluate_ai_workload(
        service="support-assistant",
        inference_mode="online",
        registered_model_name=MODEL_NAME,
        model_version="4",
        serving_runtime="vllm",
        model_evidence=_MODEL_READY,
        run_evidence=_RUN_FINISHED,
        endpoint_evidence=_ENDPOINT_HEALTHY,
    )

    assert report.model["status"] == "PASS"
    assert report.run["status"] == "PASS"
    assert report.endpoint["status"] == "PASS"
    assert report.verdict == "healthy"
    assert report.sources_failed == []


def test_evaluate_ai_workload_reports_unhealthy_when_the_run_failed():
    report = evaluate_ai_workload(
        service="support-assistant",
        inference_mode="online",
        registered_model_name=MODEL_NAME,
        model_version="4",
        serving_runtime="vllm",
        model_evidence=_MODEL_READY,
        run_evidence=_RUN_FAILED,
        endpoint_evidence=_ENDPOINT_HEALTHY,
    )

    assert report.run["status"] == "FAIL"
    assert report.verdict == "unhealthy"


def test_evaluate_ai_workload_marks_endpoint_not_applicable_for_batch_inference():
    report = evaluate_ai_workload(
        service="nightly-fraud-scorer",
        inference_mode="batch",
        registered_model_name=MODEL_NAME,
        model_version="4",
        serving_runtime="spark-batch",
        model_evidence=_MODEL_READY,
        run_evidence=_RUN_FINISHED,
        endpoint_evidence=_ENDPOINT_NOT_APPLICABLE,
    )

    assert report.endpoint["status"] == "NOT_APPLICABLE"
    # a batch workload with no endpoint to check is still healthy overall --
    # NOT_APPLICABLE is never treated as a failed gating section.
    assert report.verdict == "healthy"
    assert "endpoint" not in report.sources_ok
    assert "endpoint" not in report.sources_failed


def test_evaluate_ai_workload_reports_unknown_model_status_honestly():
    report = evaluate_ai_workload(
        service="support-assistant",
        inference_mode="online",
        registered_model_name=MODEL_NAME,
        model_version="5",
        serving_runtime="vllm",
        model_evidence=_MODEL_PENDING,
        run_evidence=_RUN_FINISHED,
        endpoint_evidence=_ENDPOINT_HEALTHY,
    )

    assert report.model["status"] == "UNKNOWN"
    assert report.verdict == "unhealthy"


def test_evaluate_ai_workload_reports_a_source_it_could_not_fetch_at_all():
    report = evaluate_ai_workload(
        service="support-assistant",
        inference_mode="online",
        registered_model_name=MODEL_NAME,
        model_version="4",
        serving_runtime="vllm",
        model_evidence=_MODEL_UNREACHABLE,
        run_evidence=_RUN_FINISHED,
        endpoint_evidence=_ENDPOINT_HEALTHY,
    )

    assert report.model["status"] == "UNKNOWN"
    assert "model" in report.sources_failed
    assert "model" not in report.sources_ok


def test_evaluate_ai_workload_distinguishes_unreachable_endpoint_from_unhealthy_endpoint():
    unreachable_report = evaluate_ai_workload(
        service="support-assistant",
        inference_mode="online",
        registered_model_name=MODEL_NAME,
        model_version="4",
        serving_runtime="vllm",
        model_evidence=_MODEL_READY,
        run_evidence=_RUN_FINISHED,
        endpoint_evidence=_ENDPOINT_UNREACHABLE,
    )
    unhealthy_report = evaluate_ai_workload(
        service="support-assistant",
        inference_mode="online",
        registered_model_name=MODEL_NAME,
        model_version="4",
        serving_runtime="vllm",
        model_evidence=_MODEL_READY,
        run_evidence=_RUN_FINISHED,
        endpoint_evidence=_ENDPOINT_UNHEALTHY,
    )

    assert unreachable_report.endpoint["status"] == "UNKNOWN"
    assert unhealthy_report.endpoint["status"] == "FAIL"


# ---------------------------------------------------------------------------
# inspect_ai_workload -- the thin orchestrator, fetch chained through evaluate.
# ---------------------------------------------------------------------------


@respx.mock
def test_inspect_ai_workload_chains_the_models_run_id_into_the_run_lookup():
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/model-versions/get").mock(
        return_value=httpx.Response(200, json=_fixture("model_version_ready.json"))
    )
    respx.get(f"{MLFLOW_URL}/api/2.0/mlflow/runs/get").mock(
        return_value=httpx.Response(200, json=_fixture("run_finished.json"))
    )
    service = validate_ai_service(GOOD_AI_EXAMPLE)

    report = inspect_ai_workload(
        service,
        mlflow_base_url=MLFLOW_URL,
        endpoint_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    assert report.model["status"] == "PASS"
    assert report.run["status"] == "PASS"
    assert report.verdict == "healthy"
