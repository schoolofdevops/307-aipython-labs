"""platformops.api -- the same domain functions the CLI calls, behind a small internal HTTP API.

A script only one person can run from their own terminal is not yet a
platform capability. A **golden path** is the opposite: a stable, self-service
way for anyone on the org to get the same answer, without learning this
project's Python internals or having `platformops` installed locally at all.
This module is that golden path for five of this project's read paths --
`POST /services/validate`, `GET /health`, `GET /release-readiness`,
`GET /incident-context`, `POST /remediations/plan` -- reachable over plain
HTTP on `localhost:8080`.

Every route below is a thin wrapper. It builds a Pydantic request model out
of the HTTP request, calls the EXACT SAME function `cli.py` already calls
for the matching command, and shapes that function's return value into a
Pydantic response model. `services_validate()` calls `load_yaml_dict()` and
`validate_service()` -- the same two calls `cli.validate()` makes.
`release_readiness()` calls `releasecheck.run_release_check()` -- the same
call `cli.release_check()` makes. `incident_context()` calls
`incidentcontext.collect_incident_context()` -- the same call
`cli.incident_collect()` makes. `remediations_plan()` calls
`cloudremediate.build_remediation_plan()` -- the same call
`cli.remediate_plan()` makes. No route handler in this file re-implements
any validation, evidence-gathering or planning logic -- see the Deep Dive
for a mechanical proof that this module imports no adapter (`httpx`,
`boto3`, `kubernetes.client`, `subprocess`) directly, only the same
already-tested functions the CLI imports.

`POST /remediations/plan` is deliberately **plan-only**. This module never
imports `cloudremediate.execute_remediation_plan()` or
`execute_remediation_batch()` -- there is no `/remediations/execute` route,
and no code path here that can mutate a cloud resource. Module 25 already
established what executing a remediation safely needs: `--approve`, an
allowlist, and an audit log written by the same process making the call.
Exposing that over an open HTTP endpoint on localhost, with no equivalent
per-caller authorization, would be a real safety regression -- see the
lesson for what a production version of an execute endpoint would need
that this lab does not build.

Two things this module adds that no earlier CLI command needed: an
`X-API-Key` gate (`require_api_key()`), because a command anyone on the
network can now reach needs to know who is calling it, and a JSON-lines
audit log (`_audit_middleware()`, reusing `cloudremediate.append_audit_log()`
-- the same append-only audit-log function Module 25's remediation writes
and Module 29's governed restarts already write) recording every request
this server receives, successful or not. `GET /health` is the one route
that skips the API-key gate on purpose -- a liveness probe a load balancer
or an orchestrator calls needs to succeed before it can prove it even knows
who it is; every other route requires a valid key. Every request, gated or
not, is still written to the audit log -- an unauthenticated attempt is
exactly the kind of event an audit trail exists to record.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from platformops import __version__
from platformops.awsclient import get_aws_client
from platformops.cloudremediate import (
    FindingNotFoundError,
    append_audit_log,
    build_remediation_plan,
    find_finding,
    load_remediation_config,
    plan_to_dict,
)
from platformops.config import ConfigError, ConfigNotFoundError, load_yaml_dict
from platformops.incidentcontext import collect_incident_context
from platformops.releasecheck import run_release_check
from platformops.servicedef import ServiceDefinition, validate_service

ANONYMOUS_CALLER = "anonymous"
"""The audit log's caller value for any request that never established an identity --
a public route like `/health`, or a route an invalid/missing API key was rejected from."""


# ---------------------------------------------------------------------------
# API-key config -- {api_key: caller_name}, the same load_yaml_dict()-backed
# pattern remediation.example.yaml and k8s-ops.example.yaml already use.
# ---------------------------------------------------------------------------


def load_api_key_config(path: Path) -> dict[str, str]:
    """Load `{api_key: caller_name}` from an `api-keys.example.yaml`-shaped file."""
    data = load_yaml_dict(path)
    return dict(data.get("keys", {}))


# ---------------------------------------------------------------------------
# Request/response models -- one pair per route, mirroring the exact shape
# the matching CLI command's --json output already uses.
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str


class ServiceValidateRequest(BaseModel):
    path: str = Field(
        description="path to a service definition YAML file on the API host"
    )


class ServiceValidateResponse(BaseModel):
    status: str
    service: dict[str, Any] | None = None
    errors: list[dict[str, Any]] | None = None


class ReleaseReadinessResponse(BaseModel):
    status: str
    service: str
    branch: str
    verdict: str
    pr: dict[str, Any]
    ci: dict[str, Any]
    checks: dict[str, Any]
    artifacts: dict[str, Any]
    release: dict[str, Any]
    deployment: dict[str, Any]
    sources_ok: list[str]
    sources_failed: list[str]


class IncidentContextResponse(BaseModel):
    status: str
    service: str
    ownership: dict[str, Any]
    source_changes: dict[str, Any]
    ci: dict[str, Any]
    kubernetes: dict[str, Any]
    cloud: dict[str, Any]
    observability: dict[str, Any]
    runbook: dict[str, Any]
    timeline: list[dict[str, Any]]
    sources_ok: list[str]
    sources_failed: list[str]


class RemediationPlanRequest(BaseModel):
    report_path: str = Field(
        description="path to a JSON report written by cloud-audit --output"
    )
    finding_id: str = Field(description="a finding's id, '<resource_id>:<rule_id>'")
    region: str = Field(description="AWS region, e.g. us-east-1")
    remediation_config: str = "remediation.example.yaml"
    profile: str | None = None
    endpoint_url: str | None = None


class RemediationPlanResponse(BaseModel):
    status: str
    plan: dict[str, Any]


# ---------------------------------------------------------------------------
# Auth -- one dependency every route except /health declares with Depends().
# ---------------------------------------------------------------------------


def require_api_key(
    request: Request, x_api_key: Annotated[str | None, Header()] = None
) -> str:
    """Resolve the caller's identity from `X-API-Key`, or refuse with 401.

    A valid key's caller name is stashed on `request.state.caller` so
    `_audit_middleware()` -- which runs for every request, gated or not --
    can record who actually made this call, not just that some key was
    presented.
    """
    api_keys: dict[str, str] = request.app.state.api_keys
    if x_api_key is None or x_api_key not in api_keys:
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")
    caller = api_keys[x_api_key]
    request.state.caller = caller
    return caller


RequireApiKey = Annotated[str, Depends(require_api_key)]


# ---------------------------------------------------------------------------
# Audit log -- every request, whatever its outcome, becomes one JSON-lines
# record: who (if known), what, when, and what it got back.
# ---------------------------------------------------------------------------


async def _audit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Any]]
) -> Any:
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000, 1)
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "method": request.method,
        "path": request.url.path,
        "caller": getattr(request.state, "caller", ANONYMOUS_CALLER),
        "status_code": response.status_code,
        "duration_ms": duration_ms,
    }
    append_audit_log(request.app.state.audit_log_path, record)
    return response


@asynccontextmanager
async def _lifespan(app: FastAPI) -> Any:
    """Startup/shutdown events -- the current replacement for the deprecated `@app.on_event()`."""
    append_audit_log(
        app.state.audit_log_path,
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": "api-server-started",
            "version": __version__,
        },
    )
    yield
    append_audit_log(
        app.state.audit_log_path,
        {"timestamp": datetime.now(UTC).isoformat(), "event": "api-server-stopped"},
    )


# ---------------------------------------------------------------------------
# App factory -- `api-serve` (cli.py) is the only caller in this project;
# tests build their own app with a temp audit log and a throwaway key.
# ---------------------------------------------------------------------------


def create_app(*, api_keys: dict[str, str], audit_log_path: Path) -> FastAPI:
    app = FastAPI(
        title="PlatformOps Internal API",
        description="Self-service access to PlatformOps read paths and dry-run remediation planning.",
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.api_keys = api_keys
    app.state.audit_log_path = audit_log_path
    app.middleware("http")(_audit_middleware)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Liveness probe -- no API key required, so an orchestrator can always reach it."""
        return HealthResponse(
            status="ok", version=__version__, timestamp=datetime.now(UTC).isoformat()
        )

    @app.post("/services/validate", response_model=ServiceValidateResponse)
    def services_validate(
        payload: ServiceValidateRequest, caller: RequireApiKey
    ) -> ServiceValidateResponse:
        """Validate a service definition file -- calls the same two functions `platformops validate` does."""
        try:
            data = load_yaml_dict(Path(payload.path))
        except ConfigNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        result = validate_service(data)
        if isinstance(result, ServiceDefinition):
            return ServiceValidateResponse(
                status="ok", service=result.model_dump(mode="json")
            )
        return ServiceValidateResponse(status="error", errors=result)

    @app.get("/release-readiness", response_model=ReleaseReadinessResponse)
    def release_readiness(
        caller: RequireApiKey,
        service: str,
        owner: str,
        pr: int,
        repo: str | None = None,
        branch: str = "main",
        environment: str = "production",
        artifact_name: str | None = None,
    ) -> ReleaseReadinessResponse:
        """Combine PR/CI/checks/artifact/release/deployment evidence -- calls the same
        `releasecheck.run_release_check()` the CLI's `release-check` command calls.

        Always HTTP 200 when the report was produced: `verdict` (`ready` /
        `not_ready`) is business data in the body, not a protocol-level
        error -- an HTTP client should branch on `verdict`, the same way a
        script branches on the CLI's exit code, not on the status code
        alone.
        """
        report = run_release_check(
            owner=owner,
            repo=repo or service,
            branch=branch,
            pr=pr,
            service=service,
            environment=environment,
            artifact_name=artifact_name,
            token=os.environ.get("GITHUB_TOKEN"),
        )
        return ReleaseReadinessResponse(
            status="ok",
            service=report.service,
            branch=report.branch,
            verdict=report.verdict,
            pr=report.pr,
            ci=report.ci,
            checks=report.checks,
            artifacts=report.artifacts,
            release=report.release,
            deployment=report.deployment,
            sources_ok=report.sources_ok,
            sources_failed=report.sources_failed,
        )

    @app.get("/incident-context", response_model=IncidentContextResponse)
    def incident_context(
        caller: RequireApiKey,
        service: str,
        owner: str,
        service_path: str = "service.yaml",
        repo_path: str = ".",
        repo: str | None = None,
        branch: str | None = None,
        namespace: str = "default",
        deployment_name: str | None = None,
        kubeconfig: str | None = None,
        context: str | None = None,
        in_cluster: bool = False,
        policy: str = "policy.example.yaml",
        region: str = "us-east-1",
        profile: str | None = None,
        endpoint_url: str | None = None,
        metrics_url: str = "http://localhost:9090",
        logs_url: str = "http://localhost:3100",
        alerts_url: str = "http://localhost:9093",
        metrics_query: str | None = None,
        correlation_id: str | None = None,
        registry: str = "incident.example.yaml",
    ) -> IncidentContextResponse:
        """Gather one incident's context -- calls the same
        `incidentcontext.collect_incident_context()` the CLI's `incident-collect`
        command calls. Always HTTP 200 when the report was produced; `status`
        (`ok` / `partial`) mirrors `sources_failed`, exactly like the CLI's
        JSON output.
        """
        resolved_repo = repo or service
        resolved_deployment_name = deployment_name or service
        resolved_metrics_query = (
            metrics_query or f'http_requests_total{{service="{service}"}}'
        )

        report = collect_incident_context(
            service=service,
            service_path=Path(service_path),
            repo_path=repo_path,
            owner=owner,
            repo=resolved_repo,
            branch=branch,
            namespace=namespace,
            deployment_name=resolved_deployment_name,
            kubeconfig_path=kubeconfig,
            context=context,
            in_cluster=in_cluster,
            policy_path=Path(policy),
            region=region,
            profile=profile,
            endpoint_url=endpoint_url,
            metrics_base_url=metrics_url,
            metrics_query=resolved_metrics_query,
            logs_base_url=logs_url,
            alerts_base_url=alerts_url,
            correlation_id=correlation_id,
            registry_path=Path(registry),
            github_token=os.environ.get("GITHUB_TOKEN"),
            observability_token=os.environ.get("OBSERVABILITY_TOKEN"),
        )

        return IncidentContextResponse(
            status="ok" if not report.sources_failed else "partial",
            service=report.service,
            ownership=report.ownership,
            source_changes=report.source_changes,
            ci=report.ci,
            kubernetes=report.kubernetes,
            cloud=report.cloud,
            observability=report.observability,
            runbook=report.runbook,
            timeline=report.timeline,
            sources_ok=report.sources_ok,
            sources_failed=report.sources_failed,
        )

    @app.post("/remediations/plan", response_model=RemediationPlanResponse)
    def remediations_plan(
        payload: RemediationPlanRequest, caller: RequireApiKey
    ) -> RemediationPlanResponse:
        """Show what remediating one finding would do -- always a dry run, never a mutation.

        Calls the same `cloudremediate.build_remediation_plan()` the CLI's
        `remediate-plan` command calls. There is no `/remediations/execute`
        route -- this module never imports
        `cloudremediate.execute_remediation_plan()` at all.
        """
        report_path = Path(payload.report_path)
        try:
            report_text = report_path.read_text()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"{report_path}: no such file"
            ) from exc

        report = json.loads(report_text)
        try:
            finding = find_finding(report, payload.finding_id)
        except FindingNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        config = load_remediation_config(Path(payload.remediation_config))
        client = get_aws_client(
            "s3",
            region=payload.region,
            profile=payload.profile,
            endpoint_url=payload.endpoint_url,
        )
        plan = build_remediation_plan(
            client, finding, allowlist=config["allowlist"], remediation_config=config
        )
        return RemediationPlanResponse(status="ok", plan=plan_to_dict(plan))

    return app


__all__ = [
    "ANONYMOUS_CALLER",
    "HealthResponse",
    "IncidentContextResponse",
    "RemediationPlanRequest",
    "RemediationPlanResponse",
    "ReleaseReadinessResponse",
    "ServiceValidateRequest",
    "ServiceValidateResponse",
    "create_app",
    "load_api_key_config",
    "require_api_key",
]
