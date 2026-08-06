import json
import logging

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from platformops.telemetry import (
    _JsonFormatter,
    configure_json_logging,
    configure_tracing,
    new_correlation_id,
    traced_operation,
)


def _test_tracer():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_new_correlation_id_has_a_stable_greppable_shape():
    cid = new_correlation_id()
    assert cid.startswith("req-")
    assert len(cid) == len("req-") + 12


def test_new_correlation_id_is_unique_each_call():
    assert new_correlation_id() != new_correlation_id()


def test_traced_operation_tags_the_span_with_the_correlation_id():
    tracer, exporter = _test_tracer()
    cid = "req-abc123def456"

    with traced_operation("demo-op", correlation_id=cid, tracer=tracer):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "demo-op"
    assert spans[0].attributes["correlation_id"] == cid


def test_traced_operation_passes_through_extra_attributes():
    tracer, exporter = _test_tracer()

    with traced_operation(
        "demo-op",
        correlation_id="req-abc123def456",
        tracer=tracer,
        attributes={"service": "payments"},
    ):
        pass

    spans = exporter.get_finished_spans()
    assert spans[0].attributes["service"] == "payments"


def test_traced_operation_makes_the_correlation_id_available_to_log_records(caplog):
    tracer, _ = _test_tracer()
    cid = "req-abc123def456"
    handler = configure_json_logging()
    logger = logging.getLogger("platformops.telemetry.test")

    captured: list[str] = []
    handler.stream.write = captured.append  # type: ignore[method-assign]

    with traced_operation("demo-op", correlation_id=cid, tracer=tracer):
        logger.info("hello from inside the span")

    line = "".join(captured).strip().splitlines()[0]
    record = json.loads(line)
    assert record["correlation_id"] == cid
    assert record["message"] == "hello from inside the span"


def test_correlation_id_is_absent_outside_any_traced_operation():
    handler = configure_json_logging()
    logger = logging.getLogger("platformops.telemetry.test2")

    captured: list[str] = []
    handler.stream.write = captured.append  # type: ignore[method-assign]

    logger.info("no span here")

    line = "".join(captured).strip().splitlines()[0]
    record = json.loads(line)
    assert record["correlation_id"] is None


def test_json_formatter_renders_one_json_object_per_record():
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="platformops.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="a structured message",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "req-abc123def456"

    rendered = formatter.format(record)
    parsed = json.loads(rendered)

    assert parsed["level"] == "WARNING"
    assert parsed["message"] == "a structured message"
    assert parsed["correlation_id"] == "req-abc123def456"


def test_configure_tracing_is_idempotent_within_one_process():
    provider_a = configure_tracing("platformops-test-idempotent")
    provider_b = configure_tracing("platformops-test-idempotent")
    assert provider_a is provider_b
