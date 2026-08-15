"""platformops.ai_release_readiness -- is the model any good, and is the infrastructure under it healthy? Two separate questions, never one.

`ai_inspect.py` (Module 33) answers "is this AI workload up right now":
is the registered model version `READY`, did its training run finish, is
its endpoint answering. `releasecheck.py` (Module 26) answers "is this
release ready to ship" for a normal service, from GitHub's own CI/CD
evidence. Neither one answers the question a release engineer actually
asks before promoting a new model version: is *this* version, the one
about to go live, good enough to ship, AND is the infrastructure under it
ready to carry it. A model that scored badly on evaluation is a quality
problem no amount of healthy infrastructure fixes; a perfectly-scored
model behind a Deployment with no GPU request configured is an
infrastructure problem no amount of model accuracy fixes. Collapsing both
into one verdict would hide which team needs to act.

This module composes two prior modules instead of re-implementing either.
`gather_model_evidence()` and `gather_endpoint_evidence()` are imported
directly from `ai_inspect.py` -- the registry-status and endpoint-health
questions do not change just because this module also cares about
evaluation and Kubernetes. `get_deployment_status()` and
`get_deployment_resource_profile()` are imported from
`kubernetes_inspect.py` (Module 28) -- rollout health and resource
requests are still that module's job; this file never touches the
Kubernetes API directly. What is new here: `gather_evaluation_evidence()`
(the training run's logged metric, read against a threshold -- not just
whether the run finished) and `gather_kubernetes_evidence()` (this
module's own fetch wrapper around the two `kubernetes_inspect` reads,
following the exact honest-degradation pattern
`incidentcontext.gather_kubernetes_evidence()` already established).

`evaluate_ai_release_readiness()` is the pure aggregation half -- no
network call anywhere in its body -- and it keeps two sub-verdicts
alongside the combined one: `model_quality_verdict` (model registry state
+ evaluation score) and `infrastructure_verdict` (deployment rollout,
resource requests, deployed-version match, and endpoint health). A caller
that only wants to know "is this a data-science problem or an ops
problem" reads those two fields directly, instead of guessing from a
single collapsed status. `deployed_version` -- read from the Deployment's
own `model-version` label -- is carried on the report unconditionally,
matched or not: it is the rollback target, the last version that was
actually running, whether or not the new one passes.

This module makes no write call of any kind, the same discipline every
inspector in this project already follows -- it never registers a model
version, never edits a Deployment, and never restarts anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import MaxRetryError

from platformops.ai_inspect import gather_endpoint_evidence, gather_model_evidence
from platformops.aiservice import AIServiceDefinition
from platformops.httpclient import (
    DEFAULT_TIMEOUT,
    EndpointStatusError,
    EndpointUnreachableError,
    HttpCheckError,
    ResponseFormatError,
    get_run,
)
from platformops.k8sclient import get_kubernetes_clients
from platformops.kubernetes_inspect import (
    get_deployment_resource_profile,
    get_deployment_status,
    rollout_state,
)

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
# every connection attempt to the API server itself is refused. Same set
# `incidentcontext.py` already uses for the same reason.
_K8S_UNREACHABLE_ERRORS = (ConfigException, MaxRetryError, OSError)

# MLflow's own run lifecycle (M33). `RUNNING` and `SCHEDULED` are real,
# known, in-progress states -- not a failure, and not proof of a passing
# evaluation either -- so they fall through to an honest `UNKNOWN`.
_SUCCESSFUL_RUN_STATUSES = {"FINISHED"}
_FAILED_RUN_STATUSES = {"FAILED", "KILLED"}

_READY_MODEL_STATUSES = {"READY"}
_FAILED_MODEL_STATUSES = {"FAILED_REGISTRATION"}

# The Deployment label this module reads to learn which model version is
# actually running -- set by the manifest that deploys a model-serving
# workload, the same way `app` is set by convention for every Deployment
# `kubernetes_inspect.py`'s label-selector logic already relies on.
MODEL_VERSION_LABEL = "model-version"


# ---------------------------------------------------------------------------
# Fetch -- one function per evidence source. `gather_model_evidence()` and
# `gather_endpoint_evidence()` are ai_inspect.py's own functions, imported
# directly rather than re-implemented. `gather_evaluation_evidence()` and
# `gather_kubernetes_evidence()` are new here.
# ---------------------------------------------------------------------------


def gather_evaluation_evidence(
    mlflow_base_url: str,
    run_id: str,
    *,
    metric_key: str = "accuracy",
    token: str | None = None,
) -> dict[str, Any]:
    """Fetch the run behind a model version and one logged metric from it -- evaluation, not just "did it finish".

    This reuses `get_run()` (M33) exactly the way `ai_inspect.gather_run_evidence()`
    does -- the difference is what this function reports back: not only the
    run's own status, but the one metric `evaluate_ai_release_readiness()`
    will judge against a threshold. A `FAILED` run is a fetched, honest
    answer with no metric to trust, not a failure to fetch.
    """
    try:
        run = get_run(mlflow_base_url, run_id, token=token)
    except _FETCHABLE_HTTP_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    metrics = {metric["key"]: metric["value"] for metric in run["metrics"]}
    return {
        "fetched": True,
        "status": run["status"],
        "metric_key": metric_key,
        "metric_value": metrics.get(metric_key),
    }


def gather_kubernetes_evidence(
    *,
    name: str,
    namespace: str,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    in_cluster: bool = False,
) -> dict[str, Any]:
    """Fetch one Deployment's rollout health, labels and resource requests -- honestly unreachable if the cluster is not up.

    Two separate `kubernetes_inspect` reads compose here:
    `get_deployment_status()` for rollout health, `get_deployment_resource_profile()`
    for the `model-version` label and each container's resource requests.
    A missing Deployment (a 404) is a real, known answer -- `"found": False`
    -- not an unreachable cluster.
    """
    try:
        apps_client, _core_client = get_kubernetes_clients(
            kubeconfig_path=kubeconfig_path, context=context, in_cluster=in_cluster
        )
        status = get_deployment_status(apps_client, name=name, namespace=namespace)
        resources = get_deployment_resource_profile(
            apps_client, name=name, namespace=namespace
        )
    except _K8S_UNREACHABLE_ERRORS as exc:
        return {"fetched": False, "error": str(exc)}
    except ApiException as exc:
        if exc.status == 404:
            return {"fetched": True, "found": False}
        return {"fetched": False, "error": str(exc)}

    return {
        "fetched": True,
        "found": True,
        "rollout_state": rollout_state(status),
        "desired_replicas": status.desired_replicas,
        "ready_replicas": status.ready_replicas,
        "labels": resources.labels,
        "resource_requests": resources.container_resource_requests,
    }


# ---------------------------------------------------------------------------
# Aggregate -- pure functions. Nothing below this line calls httpclient,
# get_kubernetes_clients, or a gather_* function.
# ---------------------------------------------------------------------------


@dataclass
class AIReleaseReadinessReport:
    """One model version's combined release-readiness picture, kept in two halves plus a rollback fact.

    `model_quality_verdict` is `"ready"` only when `model` and `evaluation`
    are both `PASS` -- this is the data-science half: is this the version
    the registry says it is, and did it score well enough. `infrastructure_verdict`
    is `"ready"` only when `deployment`, `version_match` and (for an
    `online` workload) `endpoint` are all `PASS` -- this is the ops half: is
    the right version actually running, and is it healthy. `verdict` is
    `"ready"` only when both halves are. `deployed_version` is read from the
    Deployment's own label whenever Kubernetes evidence was reachable and
    found one, matched or not -- it names the rollback target: the version
    that was actually serving before this check ran.
    """

    service: str
    model_version: str
    deployed_version: str | None
    model: dict[str, Any]
    evaluation: dict[str, Any]
    endpoint: dict[str, Any]
    deployment: dict[str, Any]
    version_match: dict[str, Any]
    model_quality_verdict: str
    infrastructure_verdict: str
    verdict: str
    sources_ok: list[str]
    sources_failed: list[str]


def _evaluate_model_registry(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "model registry evidence unavailable"),
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


def _evaluate_evaluation(
    evidence: dict[str, Any], *, threshold: float
) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "evaluation evidence unavailable"),
        }
    status = evidence["status"]
    if status in _FAILED_RUN_STATUSES:
        return {
            "status": "FAIL",
            "detail": f"evaluation run {status} -- no trustworthy metric to judge",
        }
    if status not in _SUCCESSFUL_RUN_STATUSES:
        return {"status": "UNKNOWN", "detail": f"evaluation run status is {status}"}

    metric_key = evidence["metric_key"]
    metric_value = evidence.get("metric_value")
    if metric_value is None:
        return {
            "status": "UNKNOWN",
            "detail": f"no '{metric_key}' metric logged for this run",
        }
    if metric_value < threshold:
        return {
            "status": "FAIL",
            "detail": f"{metric_key}={metric_value} is below the required threshold {threshold}",
        }
    return {
        "status": "PASS",
        "detail": f"{metric_key}={metric_value} meets the required threshold {threshold}",
    }


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
        return {
            "status": "UNKNOWN",
            "detail": f"endpoint unreachable -- {evidence['error']}",
        }
    return {
        "status": "FAIL",
        "detail": f"endpoint answered {evidence['status_code']} -- not healthy",
    }


def _evaluate_deployment(evidence: dict[str, Any]) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": evidence.get("error", "kubernetes evidence unavailable"),
        }
    if not evidence["found"]:
        return {"status": "FAIL", "detail": "no deployment found for this service"}

    state = evidence["rollout_state"]
    if state != "healthy":
        return {"status": "FAIL", "detail": f"deployment rollout is {state}"}

    resource_requests = evidence["resource_requests"]
    if not any(requests for requests in resource_requests.values()):
        return {
            "status": "FAIL",
            "detail": "deployment declares no resource requests -- CPU/memory/GPU unbounded",
        }
    summary = ", ".join(
        f"{container}: {requests}" for container, requests in resource_requests.items()
    )
    return {
        "status": "PASS",
        "detail": f"rollout healthy, resource requests set ({summary})",
    }


def _evaluate_version_match(
    model_version: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    if not evidence.get("fetched"):
        return {
            "status": "UNKNOWN",
            "detail": "kubernetes evidence unavailable -- cannot confirm the deployed version",
        }
    if not evidence["found"]:
        return {
            "status": "UNKNOWN",
            "detail": "no deployment found -- cannot confirm the deployed version",
        }
    deployed_version = evidence["labels"].get(MODEL_VERSION_LABEL)
    if deployed_version is None:
        return {
            "status": "UNKNOWN",
            "detail": f"deployment carries no '{MODEL_VERSION_LABEL}' label",
        }
    if deployed_version != model_version:
        return {
            "status": "FAIL",
            "detail": (
                f"deployment is running model version {deployed_version}, "
                f"service definition claims version {model_version}"
            ),
        }
    return {
        "status": "PASS",
        "detail": f"deployment is running the claimed model version ({model_version})",
    }


def evaluate_ai_release_readiness(
    *,
    service: str,
    model_version: str,
    eval_threshold: float,
    model_evidence: dict[str, Any],
    evaluation_evidence: dict[str, Any],
    endpoint_evidence: dict[str, Any],
    kubernetes_evidence: dict[str, Any],
) -> AIReleaseReadinessReport:
    """Combine four already-gathered evidence dicts into one report. Pure -- no network call, ever.

    Model quality (`model`, `evaluation`) and infrastructure health
    (`deployment`, `version_match`, and `endpoint` for an `online` workload)
    are judged, and rolled up, separately -- `model_quality_verdict` and
    `infrastructure_verdict` never blend into each other. The combined
    `verdict` is `"ready"` only when both are. An `UNKNOWN` gating section
    holds its half at `"not_ready"`, the same honest-degradation rule every
    inspector in this project already follows -- incomplete evidence is
    never treated as good evidence.
    """
    model_section = _evaluate_model_registry(model_evidence)
    evaluation_section = _evaluate_evaluation(
        evaluation_evidence, threshold=eval_threshold
    )
    endpoint_section = _evaluate_endpoint(endpoint_evidence)
    deployment_section = _evaluate_deployment(kubernetes_evidence)
    version_match_section = _evaluate_version_match(model_version, kubernetes_evidence)

    sources = {
        "model": model_evidence,
        "evaluation": evaluation_evidence,
        "kubernetes": kubernetes_evidence,
    }
    sources_ok = [name for name, ev in sources.items() if ev.get("fetched")]
    sources_failed = [name for name, ev in sources.items() if not ev.get("fetched")]
    if endpoint_evidence.get("checked"):
        sources_ok.append("endpoint")

    def _verdict(sections: list[dict[str, Any]]) -> str:
        has_fail = any(section["status"] == "FAIL" for section in sections)
        has_unknown = any(section["status"] == "UNKNOWN" for section in sections)
        return "ready" if not has_fail and not has_unknown else "not_ready"

    model_quality_verdict = _verdict([model_section, evaluation_section])

    infra_sections = [deployment_section, version_match_section]
    if endpoint_section["status"] != "NOT_APPLICABLE":
        infra_sections.append(endpoint_section)
    infrastructure_verdict = _verdict(infra_sections)

    verdict = (
        "ready"
        if model_quality_verdict == "ready" and infrastructure_verdict == "ready"
        else "not_ready"
    )

    deployed_version = None
    if kubernetes_evidence.get("fetched") and kubernetes_evidence.get("found"):
        deployed_version = kubernetes_evidence["labels"].get(MODEL_VERSION_LABEL)

    return AIReleaseReadinessReport(
        service=service,
        model_version=model_version,
        deployed_version=deployed_version,
        model=model_section,
        evaluation=evaluation_section,
        endpoint=endpoint_section,
        deployment=deployment_section,
        version_match=version_match_section,
        model_quality_verdict=model_quality_verdict,
        infrastructure_verdict=infrastructure_verdict,
        verdict=verdict,
        sources_ok=sources_ok,
        sources_failed=sources_failed,
    )


def check_ai_release_readiness(
    service: AIServiceDefinition,
    *,
    mlflow_base_url: str,
    namespace: str,
    deployment_name: str,
    eval_metric: str = "accuracy",
    eval_threshold: float = 0.9,
    token: str | None = None,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    in_cluster: bool = False,
    endpoint_timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    endpoint_transport: httpx.BaseTransport | None = None,
) -> AIReleaseReadinessReport:
    """Gather every evidence source for `service` and evaluate it -- the one call a CLI command or script needs.

    The model version's evidence names the run that trained it (`run_id`);
    this function chains that id straight into `gather_evaluation_evidence()`,
    the same chaining `ai_inspect.inspect_ai_workload()` (M33) already does
    for the plain run-status check. When the model version could not be
    fetched at all, there is no `run_id` to chain from -- the evaluation
    section reports its own honest `"fetched": False` rather than guessing
    which run to ask about.
    """
    model_evidence = gather_model_evidence(
        mlflow_base_url,
        service.registered_model_name,
        service.model_version,
        token=token,
    )

    run_id = model_evidence.get("run_id") if model_evidence.get("fetched") else None
    if run_id is not None:
        evaluation_evidence = gather_evaluation_evidence(
            mlflow_base_url, run_id, metric_key=eval_metric, token=token
        )
    else:
        evaluation_evidence = {
            "fetched": False,
            "error": "no run id available -- model version evidence unavailable",
        }

    endpoint_evidence = gather_endpoint_evidence(
        service.endpoint,
        service.inference_mode,
        timeout=endpoint_timeout,
        transport=endpoint_transport,
    )

    kubernetes_evidence = gather_kubernetes_evidence(
        name=deployment_name,
        namespace=namespace,
        kubeconfig_path=kubeconfig_path,
        context=context,
        in_cluster=in_cluster,
    )

    return evaluate_ai_release_readiness(
        service=service.name,
        model_version=service.model_version,
        eval_threshold=eval_threshold,
        model_evidence=model_evidence,
        evaluation_evidence=evaluation_evidence,
        endpoint_evidence=endpoint_evidence,
        kubernetes_evidence=kubernetes_evidence,
    )
