"""platformops.incidentcontext -- pull one incident's first-response context from six sources into one report.

Every module up to this point in the course answers one question about one
system: is the working tree clean (`local_ops`, M11), did the last CI run
pass (`httpclient`, M9/M26), is this Deployment's rollout healthy
(`kubernetes_inspect`, M28), does this S3 bucket violate policy
(`cloudaudit`, M24), is a service emitting the metrics, logs and alerts it
should (`observability`, M30). During a real incident, an on-call engineer
does not ask those questions one at a time -- they ask all of them at once,
in the first few minutes, before they have decided what is even wrong. This
module is that first few minutes, automated: one command that gathers all
six answers and hands back one report.

It adds no new way of talking to git, GitHub, Kubernetes, AWS or a metrics
backend. Every network call, every subprocess call and every YAML parse in
this file happens through a function some earlier module already built and
already tested -- `local_ops.git_status()`/`git_log_detailed()` (M11),
`httpclient.list_workflow_runs()` (M9/M26), `kubernetes_inspect.inspect_workload()`
(M28), `cloudaudit.run_cloud_audit()` (M24), `observability.inspect_observability()`
(M30), and `config.load_service_yaml()`/`load_yaml_dict()` (M6) for the
service's own ownership record and this module's runbook/SLO registry. This
file imports no `subprocess`, no `httpx`, no `boto3`, no `kubernetes.client`
API call, and no raw `yaml` module -- only the adapters those modules
already export. The Deep Dive proves this mechanically the same way
`cloudaudit.py`'s Deep Dive greps for write-mode AWS calls.

The fetch/aggregate split every prior read-only inspector in this project
uses (`cloudaudit.py`, M24; `releasecheck.py`, M26; `observability.py`, M30)
carries over exactly, at a larger scale: seven `gather_*_evidence()`
functions are the only functions here that call into another module's
adapter. Each one catches every way its one call can fail and hands back a
plain dict of raw facts, or an honest `"fetched": False`.
`evaluate_incident_context()` is the other half -- a pure function with no
network call, no subprocess call, and no file read anywhere in its body --
that turns seven already-gathered evidence dicts into one
`IncidentContextReport`.

This module makes **no write call of any kind** -- no restart, no
remediation, no mutation to any of the six systems it reads from. It is a
read-only inspector, the same discipline `kubernetes_inspect.py` (M28) and
`cloudaudit.py` (M24) already apply to their one system each, applied here
across all six at once. Diagnosing an incident and fixing it are two
different jobs done by two different tools, on purpose -- the Deep Dive
proves this file never imports a mutating function from any module it
composes.

A source this module cannot reach -- kind is not running, Floci is down,
GitHub is unreachable, a metrics fixture is missing -- is reported as
`UNKNOWN` for that one section, and named in `sources_failed`. It never
becomes a silent gap and it never aborts the run: every other section that
*did* answer still appears in the report. An incident report that goes
blank the moment one of six sources is unreachable is worse than useless
during an outage -- the whole point of gathering six sources instead of one
is that they rarely all fail at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import botocore.exceptions
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import MaxRetryError

from platformops.cloudaudit import run_cloud_audit
from platformops.config import ConfigError, load_service_yaml, load_yaml_dict
from platformops.httpclient import (
    EndpointStatusError,
    EndpointUnreachableError,
    HttpCheckError,
    ResponseFormatError,
    list_workflow_runs,
)
from platformops.k8sclient import get_kubernetes_clients
from platformops.kubernetes_inspect import inspect_workload
from platformops.local_ops import git_log_detailed, git_status
from platformops.observability import inspect_observability
from platformops.servicedef import ServiceDefinition

_FETCHABLE_HTTP_ERRORS = (
    EndpointStatusError,
    EndpointUnreachableError,
    ResponseFormatError,
    HttpCheckError,
)

# A cluster that is unreachable outright (kind stopped, wrong context) never
# reaches `ApiException` -- that class is for a server that answered with an
# HTTP error. `ConfigException` covers a kubeconfig that cannot even be
# loaded; `MaxRetryError` is what the underlying `urllib3` pool raises when
# every connection attempt to the API server itself is refused.
_K8S_UNREACHABLE_ERRORS = (ConfigException, MaxRetryError, OSError)


# ---------------------------------------------------------------------------
# Fetch -- one function per evidence source. These, and only these, call
# into another module's adapter. Every one of them catches its own failures
# and hands back a plain dict of raw facts -- never a judgment about what
# the incident is, or whether anything here is "the problem".
# ---------------------------------------------------------------------------


def gather_service_evidence(service_path: Path) -> dict[str, Any]:
    """Fetch the service's own ownership record -- name, repo, namespace, region, who owns it."""
    try:
        result = load_service_yaml(service_path)
    except ConfigError as exc:
        return {"fetched": False, "error": str(exc)}
    if isinstance(result, list):
        return {"fetched": True, "valid": False, "errors": result}
    return {"fetched": True, "valid": True, "service": result}


def gather_source_evidence(
    repo_path: str, *, commit_count: int = 5, timeout: float = 10.0
) -> dict[str, Any]:
    """Fetch the local working tree's clean/dirty state plus its recent commit history."""
    status_result = git_status(repo_path, timeout=timeout)
    if status_result.error:
        return {"fetched": False, "error": status_result.error}
    commits = git_log_detailed(repo_path, n=commit_count, timeout=timeout)
    return {
        "fetched": True,
        "clean": status_result.clean,
        "changed_files": status_result.changed_files,
        "commits": commits,
    }


def gather_ci_evidence(
    owner: str,
    repo: str,
    *,
    branch: str | None = None,
    token: str | None = None,
    max_pages: int = 1,
) -> dict[str, Any]:
    """Fetch the repository's most recent workflow runs, optionally narrowed to one branch."""
    try:
        runs = list_workflow_runs(owner, repo, token=token, max_pages=max_pages)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    if branch is not None:
        runs = [run for run in runs if run.get("head_branch") == branch]
    return {"fetched": True, "runs": runs[:5]}


def gather_kubernetes_evidence(
    *,
    name: str,
    namespace: str,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    in_cluster: bool = False,
) -> dict[str, Any]:
    """Fetch one Deployment's rollout health -- honestly unreachable if the cluster is not up."""
    try:
        apps_client, core_client = get_kubernetes_clients(
            kubeconfig_path=kubeconfig_path, context=context, in_cluster=in_cluster
        )
        report = inspect_workload(
            apps_client, core_client, name=name, namespace=namespace
        )
    except _K8S_UNREACHABLE_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    except ApiException as exc:
        return {"fetched": False, "error": str(exc)}

    if report["status"] == "error":
        return {"fetched": True, "found": False, "error": report["message"]}
    return {"fetched": True, "found": True, "report": report}


def gather_cloud_evidence(
    *,
    policy_path: Path,
    region: str,
    profile: str | None = None,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """Fetch cloud-policy findings -- honestly unreachable if Floci (or AWS) cannot be reached."""
    try:
        report = run_cloud_audit(
            policy_path=policy_path,
            region=region,
            profile=profile,
            endpoint_url=endpoint_url,
        )
    except botocore.exceptions.BotoCoreError as exc:
        return {"fetched": False, "error": str(exc)}

    if report["status"] == "error":
        return {"fetched": False, "error": report["message"]}
    return {"fetched": True, "report": report}


def gather_observability_evidence(
    *,
    service: str,
    metrics_base_url: str,
    metrics_query: str,
    logs_base_url: str,
    alerts_base_url: str,
    correlation_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Fetch metrics/logs/alerts for the service under one correlation ID -- see `observability.py`.

    `inspect_observability()` never raises: each of its own three sources
    already degrades to an honest per-source `UNKNOWN` internally. This
    function is always `"fetched": True` -- what varies is how many of the
    snapshot's own three sources actually answered, which `_evaluate_observability()`
    reads back out of the snapshot itself.
    """
    snapshot = inspect_observability(
        service=service,
        metrics_base_url=metrics_base_url,
        metrics_query=metrics_query,
        logs_base_url=logs_base_url,
        alerts_base_url=alerts_base_url,
        correlation_id=correlation_id,
        token=token,
    )
    return {"fetched": True, "snapshot": snapshot}


def gather_runbook_evidence(registry_path: Path, service: str) -> dict[str, Any]:
    """Fetch this service's runbook URL and documented SLO target from the incident registry.

    This registry is the one new piece of state this module owns -- no
    earlier module records a runbook URL or an SLO commitment. It is read
    through `config.load_yaml_dict()` (M6), the same loader every other YAML
    config file in this project already goes through; this function never
    parses YAML itself.
    """
    try:
        data = load_yaml_dict(registry_path)
    except ConfigError as exc:
        return {"fetched": False, "error": str(exc)}

    entry = (data.get("services") or {}).get(service)
    if entry is None:
        return {"fetched": True, "found": False}
    return {
        "fetched": True,
        "found": True,
        "runbook_url": entry.get("runbook_url"),
        "slo": entry.get("slo"),
    }


# ---------------------------------------------------------------------------
# Aggregate -- pure functions. Nothing below this line calls a gather_*
# function, makes a network call, runs a subprocess, or reads a file.
# ---------------------------------------------------------------------------


@dataclass
class IncidentContextReport:
    """One incident's combined context, with every section's status and which sources actually answered.

    Every section's `status` is either a real, known answer in that
    section's own vocabulary (`CLEAN`/`DIRTY` for source control,
    `healthy`/`degraded`/`unavailable`/`scaled-to-zero` for Kubernetes, and
    so on) or the one shared value every section can carry no matter its
    domain: `"UNKNOWN"`, meaning this run could not reach that source at
    all. `sources_failed` collects exactly the section names whose status
    is `"UNKNOWN"` -- never a section that answered with bad news (`DIRTY`,
    `unavailable`, an active alert), because answering with bad news is a
    source doing its job, not failing to do it.
    """

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


def _evaluate_ownership(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "service definition unavailable"),
        }
    if not evidence["valid"]:
        return {
            "status": "UNKNOWN",
            "detail": f"service definition invalid: {len(evidence['errors'])} field error(s)",
        }
    service: ServiceDefinition = evidence["service"]
    return {
        "status": "OK",
        "detail": service.to_summary(),
        "owner": service.team_owner,
        "repository": service.repository,
        "namespace": service.kubernetes_namespace,
        "deployment_name": service.deployment_name,
        "region": service.region,
        "dashboard_url": service.observability.dashboard_url,
        "alert_channel": service.observability.alert_channel,
    }


def _evaluate_source(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "source-control evidence unavailable"),
        }
    if evidence["clean"]:
        return {
            "status": "CLEAN",
            "detail": "working tree clean",
            "changed_files": [],
            "commits": evidence["commits"],
        }
    return {
        "status": "DIRTY",
        "detail": f"{len(evidence['changed_files'])} changed file(s)",
        "changed_files": evidence["changed_files"],
        "commits": evidence["commits"],
    }


def _evaluate_ci(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "CI evidence unavailable"),
        }
    runs = evidence["runs"]
    if not runs:
        return {
            "status": "EMPTY",
            "detail": "no recent workflow run(s) found",
            "runs": [],
        }
    latest = runs[0]
    return {
        "status": "OK",
        "detail": f"latest run #{latest['id']}: {latest.get('status')}/{latest.get('conclusion')}",
        "runs": runs,
    }


def _evaluate_kubernetes(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "kubernetes evidence unavailable"),
        }
    if not evidence["found"]:
        return {
            "status": "not_found",
            "detail": evidence.get("error", "deployment not found"),
        }
    report = evidence["report"]
    deployment = report["deployment"]
    return {
        "status": report["rollout_state"],
        "detail": (
            f"{deployment['name']} ({deployment['namespace']}): "
            f"ready={deployment['ready_replicas']}/{deployment['desired_replicas']}"
        ),
        "warning_events": report["warning_events"],
    }


def _evaluate_cloud(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "cloud evidence unavailable"),
        }
    summary = evidence["report"]["summary"]
    return {
        "status": "OK",
        "detail": (
            f"{summary['total_findings']} finding(s) -- {summary['active']} active, "
            f"{summary['suppressed']} suppressed"
        ),
        "findings": evidence["report"]["findings"],
    }


def _evaluate_observability(evidence: dict[str, Any]) -> dict[str, Any]:
    snapshot = evidence["snapshot"]
    sections = {
        "metrics": snapshot.metrics,
        "logs": snapshot.logs,
        "alerts": snapshot.alerts,
    }
    if not snapshot.sources_ok:
        status = "UNKNOWN"
        detail = f"unreachable: {', '.join(snapshot.sources_failed)}"
    elif snapshot.sources_failed:
        status = "DEGRADED"
        detail = (
            f"reachable: {', '.join(snapshot.sources_ok)}; "
            f"unreachable: {', '.join(snapshot.sources_failed)}"
        )
    else:
        status = "OK"
        detail = "metrics, logs and alerts all reachable"
    return {
        "status": status,
        "detail": detail,
        "correlation_id": snapshot.correlation_id,
        "sections": sections,
        "sources_ok": snapshot.sources_ok,
        "sources_failed": snapshot.sources_failed,
    }


def _evaluate_runbook(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "runbook registry unavailable"),
        }
    if not evidence["found"]:
        return {
            "status": "INFO",
            "detail": "no runbook entry recorded for this service",
        }

    slo = evidence.get("slo") or {}
    target = slo.get("target_percent")
    window = slo.get("window_days")
    detail = f"runbook: {evidence['runbook_url']}"
    if target is not None and window is not None:
        detail += (
            f" -- SLO target {target}% over {window}d "
            "(documented target, not calculated from live data)"
        )
    return {
        "status": "INFO",
        "detail": detail,
        "runbook_url": evidence.get("runbook_url"),
        "slo": slo,
    }


def build_timeline(
    *,
    source_evidence: dict[str, Any],
    ci_evidence: dict[str, Any],
    kubernetes_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge real timestamps from whichever sources answered into one time-ordered list.

    Only evidence that actually carries a real timestamp goes in -- a commit
    with an author date, a workflow run with a `created_at`, a Kubernetes
    warning event with a `last_seen`. A source this run could not reach
    contributes nothing here; this function never invents a timestamp to
    fill a gap.
    """
    entries: list[dict[str, Any]] = []

    if source_evidence.get("fetched"):
        for commit in source_evidence.get("commits", []):
            entries.append(
                {
                    "timestamp": commit["authored_at"],
                    "source": "git",
                    "summary": f"{commit['sha'][:7]} {commit['subject']}",
                }
            )

    if ci_evidence.get("fetched"):
        for run in ci_evidence.get("runs", []):
            if run.get("created_at"):
                entries.append(
                    {
                        "timestamp": run["created_at"],
                        "source": "ci",
                        "summary": (
                            f"workflow run #{run['id']}: "
                            f"{run.get('status')}/{run.get('conclusion')}"
                        ),
                    }
                )

    if kubernetes_evidence.get("fetched") and kubernetes_evidence.get("found"):
        for event in kubernetes_evidence["report"]["warning_events"]:
            if event.get("last_seen"):
                entries.append(
                    {
                        "timestamp": event["last_seen"],
                        "source": "kubernetes",
                        "summary": f"{event['reason']} on {event['involved_object']}: {event['message']}",
                    }
                )

    entries.sort(key=lambda entry: entry["timestamp"], reverse=True)
    return entries


def evaluate_incident_context(
    *,
    service: str,
    service_evidence: dict[str, Any],
    source_evidence: dict[str, Any],
    ci_evidence: dict[str, Any],
    kubernetes_evidence: dict[str, Any],
    cloud_evidence: dict[str, Any],
    observability_evidence: dict[str, Any],
    runbook_evidence: dict[str, Any],
) -> IncidentContextReport:
    """Combine seven already-gathered evidence dicts into one report. Pure -- no I/O of any kind, ever.

    A source that failed to fetch never becomes a silent `OK` here -- it
    becomes an honest `UNKNOWN` section and a name in `sources_failed`. This
    is the rule this whole module exists to enforce, the same one
    `releasecheck.py` and `observability.py` already enforce for their own,
    smaller slice of this project's evidence.
    """
    sections = {
        "ownership": _evaluate_ownership(service_evidence),
        "source_changes": _evaluate_source(source_evidence),
        "ci": _evaluate_ci(ci_evidence),
        "kubernetes": _evaluate_kubernetes(kubernetes_evidence),
        "cloud": _evaluate_cloud(cloud_evidence),
        "observability": _evaluate_observability(observability_evidence),
        "runbook": _evaluate_runbook(runbook_evidence),
    }
    sources_ok = [
        name for name, section in sections.items() if section["status"] != "UNKNOWN"
    ]
    sources_failed = [
        name for name, section in sections.items() if section["status"] == "UNKNOWN"
    ]

    timeline = build_timeline(
        source_evidence=source_evidence,
        ci_evidence=ci_evidence,
        kubernetes_evidence=kubernetes_evidence,
    )

    return IncidentContextReport(
        service=service,
        ownership=sections["ownership"],
        source_changes=sections["source_changes"],
        ci=sections["ci"],
        kubernetes=sections["kubernetes"],
        cloud=sections["cloud"],
        observability=sections["observability"],
        runbook=sections["runbook"],
        timeline=timeline,
        sources_ok=sources_ok,
        sources_failed=sources_failed,
    )


def collect_incident_context(
    *,
    service: str,
    service_path: Path,
    repo_path: str,
    owner: str,
    repo: str,
    branch: str | None,
    namespace: str,
    deployment_name: str,
    policy_path: Path,
    region: str,
    metrics_base_url: str,
    metrics_query: str,
    logs_base_url: str,
    alerts_base_url: str,
    registry_path: Path,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    in_cluster: bool = False,
    profile: str | None = None,
    endpoint_url: str | None = None,
    correlation_id: str | None = None,
    github_token: str | None = None,
    observability_token: str | None = None,
) -> IncidentContextReport:
    """Gather every evidence source and evaluate it -- the one call a CLI command needs.

    This is the only function in this module that both fetches evidence and
    calls the aggregation function -- it is intentionally a thin
    orchestrator, the same shape `releasecheck.run_release_check()` and
    `observability.inspect_observability()` already established.
    """
    service_evidence = gather_service_evidence(service_path)
    source_evidence = gather_source_evidence(repo_path)
    ci_evidence = gather_ci_evidence(owner, repo, branch=branch, token=github_token)
    kubernetes_evidence = gather_kubernetes_evidence(
        name=deployment_name,
        namespace=namespace,
        kubeconfig_path=kubeconfig_path,
        context=context,
        in_cluster=in_cluster,
    )
    cloud_evidence = gather_cloud_evidence(
        policy_path=policy_path,
        region=region,
        profile=profile,
        endpoint_url=endpoint_url,
    )
    observability_evidence = gather_observability_evidence(
        service=service,
        metrics_base_url=metrics_base_url,
        metrics_query=metrics_query,
        logs_base_url=logs_base_url,
        alerts_base_url=alerts_base_url,
        correlation_id=correlation_id,
        token=observability_token,
    )
    runbook_evidence = gather_runbook_evidence(registry_path, service)

    return evaluate_incident_context(
        service=service,
        service_evidence=service_evidence,
        source_evidence=source_evidence,
        ci_evidence=ci_evidence,
        kubernetes_evidence=kubernetes_evidence,
        cloud_evidence=cloud_evidence,
        observability_evidence=observability_evidence,
        runbook_evidence=runbook_evidence,
    )


__all__ = [
    "IncidentContextReport",
    "gather_service_evidence",
    "gather_source_evidence",
    "gather_ci_evidence",
    "gather_kubernetes_evidence",
    "gather_cloud_evidence",
    "gather_observability_evidence",
    "gather_runbook_evidence",
    "build_timeline",
    "evaluate_incident_context",
    "collect_incident_context",
]
