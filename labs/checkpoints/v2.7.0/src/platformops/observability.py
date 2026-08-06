"""platformops.observability -- read metrics, logs and alerts for one service, honestly.

Every operational question this course's tools have answered so far is
about state something else already computed: GitHub's CI state (M26), AWS's
resource state (M24), a Kubernetes workload's spec (M28). Observability
data is different in one specific way that matters here: a query can
succeed and still come back with nothing in it. A metrics backend that
answers instantly with zero data points is telling you something real --
this service emits no metric under that name, or nothing happened in the
queried window -- and that is a completely different fact from "the metrics
backend could not be reached at all." Confusing the two is the single
easiest mistake to make in an automated observability check, and this
module exists mainly to keep them apart everywhere it reports anything.

The fetch/aggregate split every prior read-only inspector in this project
uses (`cloudaudit.py`, M24; `releasecheck.py`, M26) carries over exactly.
Three `gather_*_evidence()` functions are the only functions here that call
into `platformops.httpclient` -- each one talks to exactly one backend,
catches every way that call can fail, and hands back the raw facts it
found (or an honest "could not fetch this" result). `evaluate_observability_snapshot()`
is the other half: a pure function that takes three already-gathered
evidence dicts and turns them into one `ObservabilitySnapshot`. It never
imports `httpx` and never calls a `gather_*` function.

`inspect_observability()` is the one function that ties this module to
`platformops.telemetry`: it opens one `traced_operation()` span for the
whole inspection, tagged with a correlation ID, so every log line this
run's own fetch calls write (and the span itself) share one identifier a
person or an agent can grep. This module never writes to a metrics, log or
alert backend -- every function here is a GET, and this module reports what
it found, it never acknowledges an alert or deletes a log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import Tracer

from platformops.httpclient import (
    EndpointStatusError,
    EndpointUnreachableError,
    HttpCheckError,
    ResponseFormatError,
    list_alerts,
    query_metrics,
    search_logs,
)
from platformops.telemetry import new_correlation_id, traced_operation

_FETCHABLE_HTTP_ERRORS = (
    EndpointStatusError,
    EndpointUnreachableError,
    ResponseFormatError,
    HttpCheckError,
)


# ---------------------------------------------------------------------------
# Fetch -- one function per evidence source. These, and only these, call
# into httpclient.py. Every one of them catches its own network failures and
# hands back a plain dict of raw facts (or "fetched": False on failure) --
# never a judgment about whether that data is good or bad news.
# ---------------------------------------------------------------------------


def gather_metrics_evidence(
    base_url: str, query: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch metric samples for `query`. An empty result is reported honestly, not as a failure."""
    try:
        samples = query_metrics(base_url, query, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    return {
        "fetched": True,
        "query": query,
        "sample_count": len(samples),
        "samples": samples,
    }


def gather_logs_evidence(
    base_url: str,
    service: str,
    correlation_id: str | None,
    *,
    limit: int = 100,
    token: str | None = None,
) -> dict[str, Any]:
    """Fetch log lines for `service`, narrowed to `correlation_id` when one is given.

    Searching by `correlation_id` alone, once a run's ID is known, is the
    whole point of tagging log lines with it in the first place -- see
    `platformops.telemetry.traced_operation()`.
    """
    query = (
        f"service:{service} AND correlation_id:{correlation_id}"
        if correlation_id
        else f"service:{service}"
    )
    try:
        logs = search_logs(base_url, query, limit=limit, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    return {"fetched": True, "query": query, "total": len(logs), "logs": logs}


def gather_alerts_evidence(
    base_url: str, service: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch alerts for `service`, split into every alert found and the ones currently active."""
    try:
        alerts = list_alerts(base_url, service=service, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    active = [alert for alert in alerts if alert["status"]["state"] == "active"]
    return {"fetched": True, "total": len(alerts), "active": active}


# ---------------------------------------------------------------------------
# Aggregate -- pure functions. Nothing below this line imports httpx, calls
# a gather_* function, or makes a network call of any kind.
# ---------------------------------------------------------------------------


@dataclass
class ObservabilitySnapshot:
    """One service's combined metrics/logs/alerts picture, tagged with the run's correlation ID.

    Each section's `status` is one of `"OK"` (data found), `"EMPTY"` (the
    source answered but found nothing -- a real, known answer), `"FIRING"`
    (alerts only: at least one active alert), or `"UNKNOWN"` (the source
    could not be reached at all). `sources_failed` lists which sources this
    run genuinely could not fetch -- `"EMPTY"` sections are never counted as
    failed, because a source that answered "nothing here" did its job.
    """

    service: str
    correlation_id: str
    metrics: dict[str, Any]
    logs: dict[str, Any]
    alerts: dict[str, Any]
    sources_ok: list[str]
    sources_failed: list[str]


def _evaluate_metrics(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "metrics evidence unavailable"),
        }
    if evidence["sample_count"] == 0:
        return {
            "status": "EMPTY",
            "detail": f"no data points found for query '{evidence['query']}'",
        }
    return {
        "status": "OK",
        "detail": f"{evidence['sample_count']} sample(s) found for query '{evidence['query']}'",
    }


def _evaluate_logs(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "log evidence unavailable"),
        }
    if evidence["total"] == 0:
        return {
            "status": "EMPTY",
            "detail": f"no log line(s) matched '{evidence['query']}'",
        }
    return {
        "status": "OK",
        "detail": f"{evidence['total']} log line(s) matched '{evidence['query']}'",
    }


def _evaluate_alerts(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "alert evidence unavailable"),
        }
    active = evidence["active"]
    if active:
        names = ", ".join(
            alert["labels"].get("alertname", "unknown") for alert in active
        )
        return {"status": "FIRING", "detail": f"{len(active)} active alert(s): {names}"}
    return {"status": "OK", "detail": "no active alerts"}


def evaluate_observability_snapshot(
    *,
    service: str,
    correlation_id: str,
    metrics_evidence: dict[str, Any],
    logs_evidence: dict[str, Any],
    alerts_evidence: dict[str, Any],
) -> ObservabilitySnapshot:
    """Combine three already-gathered evidence dicts into one snapshot. Pure -- no network call, ever.

    A source that failed to fetch (`"fetched": False`) never becomes a
    silent `"EMPTY"` here -- it becomes an honest `"UNKNOWN"` section and a
    name in `sources_failed`. This is the rule this whole module exists to
    enforce: "no data" and "could not check" are never the same report.
    """
    sources = {
        "metrics": metrics_evidence,
        "logs": logs_evidence,
        "alerts": alerts_evidence,
    }
    sources_ok = [name for name, ev in sources.items() if ev.get("fetched")]
    sources_failed = [name for name, ev in sources.items() if not ev.get("fetched")]

    return ObservabilitySnapshot(
        service=service,
        correlation_id=correlation_id,
        metrics=_evaluate_metrics(metrics_evidence),
        logs=_evaluate_logs(logs_evidence),
        alerts=_evaluate_alerts(alerts_evidence),
        sources_ok=sources_ok,
        sources_failed=sources_failed,
    )


def inspect_observability(
    *,
    service: str,
    metrics_base_url: str,
    metrics_query: str,
    logs_base_url: str,
    alerts_base_url: str,
    correlation_id: str | None = None,
    token: str | None = None,
    tracer: Tracer | None = None,
) -> ObservabilitySnapshot:
    """Gather every evidence source under one traced, correlated run, and evaluate it.

    Opens exactly one `traced_operation()` span for the whole inspection --
    every fetch below happens inside it, so every log line those fetch
    calls write carries this run's correlation ID, and the span itself
    records `service` as an attribute a trace backend can filter on. This
    is the only function in this module that both fetches evidence and
    calls the aggregation function; it is intentionally a thin
    orchestrator, the same shape `platformops.releasecheck.run_release_check()`
    already established.
    """
    resolved_correlation_id = correlation_id or new_correlation_id()

    with traced_operation(
        "observability-inspect",
        correlation_id=resolved_correlation_id,
        tracer=tracer,
        attributes={"service": service},
    ):
        metrics_evidence = gather_metrics_evidence(
            metrics_base_url, metrics_query, token=token
        )
        logs_evidence = gather_logs_evidence(
            logs_base_url, service, resolved_correlation_id, token=token
        )
        alerts_evidence = gather_alerts_evidence(alerts_base_url, service, token=token)

    return evaluate_observability_snapshot(
        service=service,
        correlation_id=resolved_correlation_id,
        metrics_evidence=metrics_evidence,
        logs_evidence=logs_evidence,
        alerts_evidence=alerts_evidence,
    )
