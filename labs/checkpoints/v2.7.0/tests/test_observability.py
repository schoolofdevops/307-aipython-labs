import json
from pathlib import Path

import httpx
import respx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from platformops.observability import (
    evaluate_observability_snapshot,
    gather_alerts_evidence,
    gather_logs_evidence,
    gather_metrics_evidence,
    inspect_observability,
)

FIXTURES = Path(__file__).parent / "fixtures" / "observability"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


METRICS_URL = "http://localhost:9090"
LOGS_URL = "http://localhost:3100"
ALERTS_URL = "http://localhost:9093"


def _test_tracer():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


# ---------------------------------------------------------------------------
# gather_*_evidence -- respx-mocked, one per source.
# ---------------------------------------------------------------------------


@respx.mock
def test_gather_metrics_evidence_reports_samples_on_success():
    respx.get(f"{METRICS_URL}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_fixture("metrics_query_success.json"))
    )

    evidence = gather_metrics_evidence(
        METRICS_URL, 'http_requests_total{service="payments"}'
    )

    assert evidence["fetched"] is True
    assert evidence["sample_count"] == 2


@respx.mock
def test_gather_metrics_evidence_degrades_honestly_when_unreachable(monkeypatch):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get(f"{METRICS_URL}/api/v1/query").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    evidence = gather_metrics_evidence(METRICS_URL, "up")

    assert evidence["fetched"] is False
    assert "unreachable" in evidence["error"]


@respx.mock
def test_gather_logs_evidence_reports_entries_on_success():
    respx.get(f"{LOGS_URL}/api/v1/logs/search").mock(
        return_value=httpx.Response(200, json=_fixture("logs_search_success.json"))
    )

    evidence = gather_logs_evidence(LOGS_URL, "payments", "req-a1b2c3d4e5f6")

    assert evidence["fetched"] is True
    assert evidence["total"] == 2


@respx.mock
def test_gather_alerts_evidence_reports_active_alerts():
    respx.get(f"{ALERTS_URL}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=_fixture("alerts_active.json"))
    )

    evidence = gather_alerts_evidence(ALERTS_URL, "payments")

    assert evidence["fetched"] is True
    assert len(evidence["active"]) == 1


# ---------------------------------------------------------------------------
# evaluate_observability_snapshot -- pure, hand-built evidence, no network.
# ---------------------------------------------------------------------------

_METRICS_FOUND = {"fetched": True, "query": "up", "sample_count": 2, "samples": []}
_METRICS_EMPTY = {"fetched": True, "query": "up", "sample_count": 0, "samples": []}
_METRICS_UNREACHABLE = {"fetched": False, "error": "boom"}

_LOGS_FOUND = {"fetched": True, "query": "q", "total": 2, "logs": []}
_LOGS_EMPTY = {"fetched": True, "query": "q", "total": 0, "logs": []}

_ALERTS_NONE_ACTIVE = {"fetched": True, "total": 0, "active": []}
_ALERTS_FIRING = {
    "fetched": True,
    "total": 1,
    "active": [{"labels": {"alertname": "HighErrorRate"}}],
}


def test_evaluate_observability_snapshot_reports_ok_when_data_is_present():
    snapshot = evaluate_observability_snapshot(
        service="payments",
        correlation_id="req-abc123def456",
        metrics_evidence=_METRICS_FOUND,
        logs_evidence=_LOGS_FOUND,
        alerts_evidence=_ALERTS_NONE_ACTIVE,
    )

    assert snapshot.metrics["status"] == "OK"
    assert snapshot.logs["status"] == "OK"
    assert snapshot.alerts["status"] == "OK"
    assert snapshot.sources_failed == []


def test_evaluate_observability_snapshot_reports_empty_as_a_real_answer_not_unknown():
    snapshot = evaluate_observability_snapshot(
        service="payments",
        correlation_id="req-abc123def456",
        metrics_evidence=_METRICS_EMPTY,
        logs_evidence=_LOGS_EMPTY,
        alerts_evidence=_ALERTS_NONE_ACTIVE,
    )

    assert snapshot.metrics["status"] == "EMPTY"
    assert snapshot.logs["status"] == "EMPTY"
    assert (
        snapshot.sources_failed == []
    )  # both sources DID answer -- they just found nothing


def test_evaluate_observability_snapshot_reports_unknown_when_a_source_is_unreachable():
    snapshot = evaluate_observability_snapshot(
        service="payments",
        correlation_id="req-abc123def456",
        metrics_evidence=_METRICS_UNREACHABLE,
        logs_evidence=_LOGS_FOUND,
        alerts_evidence=_ALERTS_NONE_ACTIVE,
    )

    assert snapshot.metrics["status"] == "UNKNOWN"
    assert snapshot.sources_failed == ["metrics"]
    assert snapshot.sources_ok == ["logs", "alerts"]


def test_evaluate_observability_snapshot_reports_firing_when_alerts_are_active():
    snapshot = evaluate_observability_snapshot(
        service="payments",
        correlation_id="req-abc123def456",
        metrics_evidence=_METRICS_FOUND,
        logs_evidence=_LOGS_FOUND,
        alerts_evidence=_ALERTS_FIRING,
    )

    assert snapshot.alerts["status"] == "FIRING"
    assert "HighErrorRate" in snapshot.alerts["detail"]


# ---------------------------------------------------------------------------
# inspect_observability -- the orchestrator: fetch + evaluate + traced_operation.
# ---------------------------------------------------------------------------


@respx.mock
def test_inspect_observability_runs_end_to_end_and_tags_one_span_with_the_correlation_id():
    respx.get(f"{METRICS_URL}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_fixture("metrics_query_success.json"))
    )
    respx.get(f"{LOGS_URL}/api/v1/logs/search").mock(
        return_value=httpx.Response(200, json=_fixture("logs_search_success.json"))
    )
    respx.get(f"{ALERTS_URL}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=_fixture("alerts_active.json"))
    )
    tracer, exporter = _test_tracer()

    snapshot = inspect_observability(
        service="payments",
        metrics_base_url=METRICS_URL,
        metrics_query='http_requests_total{service="payments"}',
        logs_base_url=LOGS_URL,
        alerts_base_url=ALERTS_URL,
        correlation_id="req-a1b2c3d4e5f6",
        tracer=tracer,
    )

    assert snapshot.correlation_id == "req-a1b2c3d4e5f6"
    assert snapshot.metrics["status"] == "OK"
    assert snapshot.alerts["status"] == "FIRING"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["correlation_id"] == "req-a1b2c3d4e5f6"
    assert spans[0].attributes["service"] == "payments"


@respx.mock
def test_inspect_observability_degrades_honestly_when_logs_backend_is_unreachable(
    monkeypatch,
):
    monkeypatch.setattr("platformops.httpclient.time.sleep", lambda _: None)
    respx.get(f"{METRICS_URL}/api/v1/query").mock(
        return_value=httpx.Response(200, json=_fixture("metrics_query_success.json"))
    )
    respx.get(f"{LOGS_URL}/api/v1/logs/search").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    respx.get(f"{ALERTS_URL}/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=_fixture("alerts_none.json"))
    )
    tracer, _ = _test_tracer()

    snapshot = inspect_observability(
        service="payments",
        metrics_base_url=METRICS_URL,
        metrics_query="up",
        logs_base_url=LOGS_URL,
        alerts_base_url=ALERTS_URL,
        tracer=tracer,
    )

    assert snapshot.logs["status"] == "UNKNOWN"
    assert snapshot.sources_failed == ["logs"]
    assert snapshot.metrics["status"] == "OK"
    assert snapshot.alerts["status"] == "OK"


def test_inspect_observability_generates_a_correlation_id_when_none_is_given():
    with respx.mock:
        respx.get(f"{METRICS_URL}/api/v1/query").mock(
            return_value=httpx.Response(200, json=_fixture("metrics_query_empty.json"))
        )
        respx.get(f"{LOGS_URL}/api/v1/logs/search").mock(
            return_value=httpx.Response(200, json=_fixture("logs_search_empty.json"))
        )
        respx.get(f"{ALERTS_URL}/api/v2/alerts").mock(
            return_value=httpx.Response(200, json=_fixture("alerts_none.json"))
        )
        tracer, _ = _test_tracer()

        snapshot = inspect_observability(
            service="payments",
            metrics_base_url=METRICS_URL,
            metrics_query="up",
            logs_base_url=LOGS_URL,
            alerts_base_url=ALERTS_URL,
            tracer=tracer,
        )

    assert snapshot.correlation_id.startswith("req-")
