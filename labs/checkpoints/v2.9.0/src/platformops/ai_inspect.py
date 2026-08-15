"""platformops.ai_inspect -- read a model version's registry state, its training run, and its serving endpoint, honestly.

`observability.py` (Module 30) already drew the line between "no data" and
"could not check" for metrics, logs and alerts. An AI-serving workload needs
that same discipline applied to three different questions: is the exact
model version this deployment claims to run actually registered and ready
(`get_model_version()`), did the training run behind it finish cleanly
(`get_run()`), and -- only for a workload with a live endpoint -- does that
endpoint answer requests (`check_health()`, Module 9). None of these three
questions is "is the model any good." Judging accuracy, drift or bias is
data-science work this toolkit deliberately never does; this module answers
the operational questions an SRE or platform engineer is actually paged
for: is the thing we deployed the thing we meant to deploy, and is it up.

The fetch/aggregate split every prior read-only inspector in this project
uses (`cloudaudit.py`, M24; `releasecheck.py`, M26; `observability.py`, M30)
carries over exactly. `gather_model_evidence()`, `gather_run_evidence()`
and `gather_endpoint_evidence()` are the only functions here that reach
outside this module -- into `platformops.httpclient`'s MLflow-shaped
functions or `check_health()`. Each one catches every way its call can
fail and hands back a plain dict of raw facts, or an honest
`"fetched": False`. `evaluate_ai_workload()` is the other half: a pure
function with no network call anywhere in its body, that turns three
already-gathered evidence dicts into one `AIWorkloadReport`.

One judgment call is specific to this module: batch inference has no
standing endpoint to poll. A nightly scoring job that reads from a queue
and writes results to a table is not "down" just because nothing answers
on port 443 right now -- there was never anything meant to answer there.
`gather_endpoint_evidence()` reports that case as `"checked": False`, and
`evaluate_ai_workload()` turns it into a `NOT_APPLICABLE` section, never a
`FAIL` -- the same discipline `cloudremediate.py` (Module 25) already
applies to a rule type that is not remediable by design: a question this
module was never meant to answer honestly says so, instead of guessing.

This module makes no write call of any kind. It never registers a model
version, never transitions a stage, and never restarts a serving process --
see the Deep Dive for the mechanical proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from platformops.aiservice import AIServiceDefinition
from platformops.httpclient import (
    DEFAULT_TIMEOUT,
    EndpointStatusError,
    EndpointUnreachableError,
    HttpCheckError,
    ResponseFormatError,
    check_health,
    get_model_version,
    get_run,
)

_FETCHABLE_HTTP_ERRORS = (
    EndpointStatusError,
    EndpointUnreachableError,
    ResponseFormatError,
    HttpCheckError,
)

# MLflow's own model version lifecycle. A version can sit in
# `PENDING_REGISTRATION` for a moment after being pushed -- a real, known
# state, not a failure -- which is why it is not in either set below and
# falls through to an honest `UNKNOWN` in `_evaluate_model()`.
_READY_MODEL_STATUSES = {"READY"}
_FAILED_MODEL_STATUSES = {"FAILED_REGISTRATION"}

# MLflow's own run lifecycle. `RUNNING` and `SCHEDULED` are real, known,
# in-progress states -- not a failure, and not proof of success either --
# so they also fall through to `UNKNOWN`.
_SUCCESSFUL_RUN_STATUSES = {"FINISHED"}
_FAILED_RUN_STATUSES = {"FAILED", "KILLED"}


# ---------------------------------------------------------------------------
# Fetch -- one function per evidence source. These, and only these, call
# into platformops.httpclient. Every one of them catches its own failures
# and hands back a plain dict of raw facts -- never a verdict.
# ---------------------------------------------------------------------------


def gather_model_evidence(
    mlflow_base_url: str,
    registered_model_name: str,
    model_version: str,
    *,
    token: str | None = None,
) -> dict[str, Any]:
    """Fetch one model version's registry status and originating run id."""
    try:
        version = get_model_version(
            mlflow_base_url, registered_model_name, model_version, token=token
        )
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    return {
        "fetched": True,
        "status": version["status"],
        "current_stage": version["current_stage"],
        "run_id": version["run_id"],
    }


def gather_run_evidence(
    mlflow_base_url: str, run_id: str, *, token: str | None = None
) -> dict[str, Any]:
    """Fetch one training run's status and logged metrics. A `FAILED` run is a fetched, honest answer, not a failure to fetch."""
    try:
        run = get_run(mlflow_base_url, run_id, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    return {
        "fetched": True,
        "status": run["status"],
        "metrics": {metric["key"]: metric["value"] for metric in run["metrics"]},
    }


def gather_endpoint_evidence(
    endpoint: str,
    inference_mode: str,
    *,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Health-check a live serving endpoint -- skipped, honestly, for a batch workload with no endpoint to poll."""
    if inference_mode == "batch":
        return {
            "checked": False,
            "reason": "batch inference has no standing endpoint to check",
        }
    result = check_health(endpoint, timeout=timeout, transport=transport)
    return {
        "checked": True,
        "ok": result.ok,
        "status_code": result.status_code,
        "latency_ms": result.latency_ms,
        "error": result.error,
    }


# ---------------------------------------------------------------------------
# Aggregate -- pure functions. Nothing below this line calls httpclient,
# check_health, or a gather_* function.
# ---------------------------------------------------------------------------


@dataclass
class AIWorkloadReport:
    """One AI workload's combined model/run/endpoint picture.

    `endpoint` carries `status="NOT_APPLICABLE"` for a batch workload --
    it is never folded into `sources_failed`, and it never drags the
    overall `verdict` to `"unhealthy"` on its own. `sources_ok` and
    `sources_failed` only ever name `"model"` and `"run"`, plus
    `"endpoint"` when this workload actually has one to check.
    """

    service: str
    inference_mode: str
    registered_model_name: str
    model_version: str
    serving_runtime: str
    model: dict[str, Any]
    run: dict[str, Any]
    endpoint: dict[str, Any]
    verdict: str
    sources_ok: list[str]
    sources_failed: list[str]


def _evaluate_model(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "model version evidence unavailable"),
        }
    status = evidence["status"]
    if status in _READY_MODEL_STATUSES:
        return {
            "status": "PASS",
            "detail": f"model version is READY (stage={evidence['current_stage']})",
        }
    if status in _FAILED_MODEL_STATUSES:
        return {"status": "FAIL", "detail": f"model version registration {status}"}
    return {"status": "UNKNOWN", "detail": f"model version status is {status}"}


def _evaluate_run(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "run evidence unavailable"),
        }
    status = evidence["status"]
    if status in _SUCCESSFUL_RUN_STATUSES:
        return {"status": "PASS", "detail": "training run FINISHED"}
    if status in _FAILED_RUN_STATUSES:
        return {"status": "FAIL", "detail": f"training run {status}"}
    return {"status": "UNKNOWN", "detail": f"training run status is {status}"}


def _evaluate_endpoint(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("checked"):
        return {
            "status": "NOT_APPLICABLE",
            "detail": evidence.get("reason", "endpoint not checked"),
        }
    if evidence["ok"]:
        return {
            "status": "PASS",
            "detail": f"endpoint healthy -- {evidence['status_code']} in {evidence['latency_ms']}ms",
        }
    if evidence["error"]:
        # No response came back at all -- a DNS failure, a refused
        # connection, a timeout. This is "could not check", not "checked
        # and it is down": the same UNKNOWN/FAIL split observability.py
        # draws between an unreachable backend and one that answered.
        return {
            "status": "UNKNOWN",
            "detail": f"endpoint unreachable -- {evidence['error']}",
        }
    return {
        "status": "FAIL",
        "detail": f"endpoint answered {evidence['status_code']} -- not healthy",
    }


def evaluate_ai_workload(
    *,
    service: str,
    inference_mode: str,
    registered_model_name: str,
    model_version: str,
    serving_runtime: str,
    model_evidence: dict[str, Any],
    run_evidence: dict[str, Any],
    endpoint_evidence: dict[str, Any],
) -> AIWorkloadReport:
    """Combine three already-gathered evidence dicts into one report. Pure -- no network call, ever.

    `verdict` is `"healthy"` only when every gating section is `PASS`, or
    -- for `endpoint` on a batch workload -- `NOT_APPLICABLE`. A source
    that failed to fetch, or a gating section this run could not judge
    (`UNKNOWN`), holds `verdict` at `"unhealthy"` the same way an `UNKNOWN`
    section holds `releasecheck.py`'s verdict at `"not_ready"` -- incomplete
    evidence is never treated as good evidence.
    """
    model_section = _evaluate_model(model_evidence)
    run_section = _evaluate_run(run_evidence)
    endpoint_section = _evaluate_endpoint(endpoint_evidence)

    sources = {"model": model_evidence, "run": run_evidence}
    sources_ok = [name for name, ev in sources.items() if ev.get("fetched")]
    sources_failed = [name for name, ev in sources.items() if not ev.get("fetched")]
    if endpoint_evidence.get("checked"):
        sources_ok.append("endpoint")

    gating_sections = [model_section, run_section]
    if endpoint_section["status"] != "NOT_APPLICABLE":
        gating_sections.append(endpoint_section)
    has_fail = any(section["status"] == "FAIL" for section in gating_sections)
    has_unknown = any(section["status"] == "UNKNOWN" for section in gating_sections)
    verdict = "healthy" if not has_fail and not has_unknown else "unhealthy"

    return AIWorkloadReport(
        service=service,
        inference_mode=inference_mode,
        registered_model_name=registered_model_name,
        model_version=model_version,
        serving_runtime=serving_runtime,
        model=model_section,
        run=run_section,
        endpoint=endpoint_section,
        verdict=verdict,
        sources_ok=sources_ok,
        sources_failed=sources_failed,
    )


def inspect_ai_workload(
    service: AIServiceDefinition,
    *,
    mlflow_base_url: str,
    token: str | None = None,
    endpoint_timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    endpoint_transport: httpx.BaseTransport | None = None,
) -> AIWorkloadReport:
    """Gather every evidence source for `service` and evaluate it -- the one call a CLI command or script needs.

    The model version's evidence names the run that trained it
    (`run_id`); this function reads that id and chains it straight into
    `gather_run_evidence()` -- the same real MLflow workflow a person
    follows by hand: look up the version, follow it to the run, check the
    run's own outcome. When the model version could not be fetched at all,
    there is no `run_id` to chain from -- the run section reports its own
    honest `"fetched": False` rather than guessing which run to ask about.
    This is intentionally a thin orchestrator, the same shape
    `observability.inspect_observability()` and
    `releasecheck.run_release_check()` already established.
    """
    model_evidence = gather_model_evidence(
        mlflow_base_url,
        service.registered_model_name,
        service.model_version,
        token=token,
    )

    run_id = model_evidence.get("run_id") if model_evidence.get("fetched") else None
    if run_id is not None:
        run_evidence = gather_run_evidence(mlflow_base_url, run_id, token=token)
    else:
        run_evidence = {
            "fetched": False,
            "error": "no run id available -- model version evidence unavailable",
        }

    endpoint_evidence = gather_endpoint_evidence(
        service.endpoint,
        service.inference_mode,
        timeout=endpoint_timeout,
        transport=endpoint_transport,
    )

    return evaluate_ai_workload(
        service=service.name,
        inference_mode=service.inference_mode,
        registered_model_name=service.registered_model_name,
        model_version=service.model_version,
        serving_runtime=service.serving_runtime,
        model_evidence=model_evidence,
        run_evidence=run_evidence,
        endpoint_evidence=endpoint_evidence,
    )
