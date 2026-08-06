"""platformops.telemetry -- structured logs and OpenTelemetry traces for this project's own automation.

Every module up to this one produces evidence *about* something else -- a
service definition, a pull request, a Kubernetes workload. None of them
answer a different question: when this toolkit itself misbehaves in
production -- a `release-check` run that took nine minutes instead of nine
seconds, a `restart-execute` run that a teammate swears they never
triggered -- how do you reconstruct what actually happened, across every log
line and every network call that one run made? "Add a print statement and
run it again" does not work once the run already happened once, unfinished,
somewhere you cannot reproduce on demand.

Two signals, one identifier, ties them together. `configure_json_logging()`
makes every log line in this process one JSON object a log-search backend
can index. `configure_tracing()` makes every instrumented operation one
OpenTelemetry span a trace backend can visualize. `traced_operation()` is
the one function that touches both at once: it opens a span, and for the
span's whole duration it makes the same correlation ID appear on every log
line written underneath it, via a `contextvars.ContextVar` a plain function
argument could not reach across nested calls without being threaded through
every signature. Grep one correlation ID in this project's own log output
and every line in the story shows up together -- an agent or a script doing
that grep, not a human paging through a dashboard, is the actual point of
this module: correlated telemetry is data automation can act on, not just
data a person can look at.

This module never sends a span or a log line anywhere but the exporter it
is explicitly given. The CLI defaults to `ConsoleSpanExporter` (prints
spans to stdout) precisely because this course's lab environment has no
OpenTelemetry Collector, no Jaeger, no Tempo -- see `course.config.json`'s
`lab.tools` list. Tests hand `traced_operation()` a tracer built from an
`InMemorySpanExporter` instead, the same swap-the-transport-not-the-logic
pattern `platformops.httpclient.check_health()`'s `transport` parameter
already uses for HTTP.
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, Tracer

logger = logging.getLogger("platformops.telemetry")

SERVICE_NAME = "platformops"

# The one place this run's correlation ID actually lives. A ContextVar,
# not a plain module-level variable, because it has to survive nested
# function calls (gather_metrics_evidence() called from
# inspect_observability(), several stack frames deep) without every one of
# those functions taking a correlation_id parameter just to pass it along --
# and because a ContextVar, unlike a plain global, keeps concurrent asyncio
# tasks or threads from bleeding their correlation IDs into each other.
_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def new_correlation_id() -> str:
    """A short, greppable run identifier -- `req-` plus 12 hex characters.

    Twelve hex characters (48 bits of randomness from `uuid4`) is
    deliberately shorter than a full UUID -- long enough that two runs
    colliding by chance never happens in this project's lifetime, short
    enough that a person can actually read one aloud or paste it into a
    search box without it wrapping across two lines of a terminal.
    """
    return f"req-{uuid.uuid4().hex[:12]}"


class _CorrelationIdFilter(logging.Filter):
    """Attaches the current correlation ID (or None) to every log record it sees.

    A `logging.Filter` runs on every record a handler processes, whether or
    not that record's code even knows this module exists -- which is the
    point: a log line written deep inside `platformops.httpclient` picks up
    the correlation ID of whichever `traced_operation()` block is currently
    open, with no import and no `extra=` argument required at that call
    site.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    """Renders one log record as one line of JSON -- the shape a log-search backend can index.

    `platformops.observability.search_logs()`'s fixtures use exactly this
    shape (`timestamp`/`level`/`message`/`correlation_id`) for the same
    reason: a real log-search backend built on structured JSON lines reads
    fields, not a human-formatted sentence it would have to re-parse.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_json_logging(level: int = logging.INFO) -> logging.StreamHandler[Any]:
    """Route every log record in this process through one JSON-line formatter.

    Replaces the root logger's handlers outright rather than adding to
    them -- safe to call more than once in the same process (the test
    suite, and a CLI command that runs after `platformops`'s own
    `logging.basicConfig(force=True)` in `cli.py`'s callback, both call
    this), the same call-more-than-once safety that `force=True` already
    gives the plain-text logging path.
    """
    handler: logging.StreamHandler[Any] = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_CorrelationIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    return handler


def configure_tracing(
    service_name: str = SERVICE_NAME, *, exporter: SpanExporter | None = None
) -> TracerProvider:
    """Install a process-wide `TracerProvider`, exporting to `exporter` (a `ConsoleSpanExporter` by default).

    OpenTelemetry's global tracer provider can only be installed once per
    process -- a second `trace.set_tracer_provider()` call is silently
    ignored (it only logs a warning), which is easy to trip over the first
    time a test suite invokes this project's CLI more than once in the same
    pytest session. This function checks whether a real `TracerProvider` is
    already installed and, if so, returns the existing one instead of
    trying (and quietly failing) to replace it -- see the Deep Dive for
    this working, and failing, both proven live. Tests that need an
    isolated exporter per test should build their own `TracerProvider` and
    pass a tracer from it straight to `traced_operation()` instead of
    calling this function at all -- exactly what `tests/test_telemetry.py`
    and `tests/test_observability.py` do.
    """
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        logger.debug(
            "tracing already configured for this process -- reusing the existing TracerProvider"
        )
        return current

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(exporter or ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    logger.debug("tracing configured for service %s", service_name)
    return provider


@contextmanager
def traced_operation(
    name: str,
    *,
    correlation_id: str,
    tracer: Tracer | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    """Open one span named `name`, and make `correlation_id` appear on both that span and every log line inside it.

    This is the module's central mechanism, and the reason it exists at
    all: for the duration of this `with` block, `correlation_id` becomes a
    span attribute a trace backend can filter on, AND
    `_correlation_id_var` makes that exact same value appear on every
    structured log line `_JsonFormatter` renders anywhere in the call
    stack underneath it -- one identifier, greppable across both signals,
    without threading it through every function's argument list by hand.

    `tracer` defaults to the process-wide tracer `configure_tracing()`
    installs; pass an explicit one (from a `TracerProvider` built just for
    a test) to keep that test's spans out of the global registry entirely.
    """
    resolved_tracer = tracer or trace.get_tracer(SERVICE_NAME)
    token = _correlation_id_var.set(correlation_id)
    try:
        with resolved_tracer.start_as_current_span(name) as span:
            span.set_attribute("correlation_id", correlation_id)
            for key, value in (attributes or {}).items():
                span.set_attribute(key, value)
            yield span
    finally:
        _correlation_id_var.reset(token)
