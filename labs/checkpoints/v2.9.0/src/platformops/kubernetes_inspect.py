"""platformops.kubernetes_inspect -- read a Deployment's real health from the Kubernetes API, never change it.

`cloudaudit.py` (Module 24) proved a pattern this project now repeats for a
second, unrelated system: separate the calls that touch a live API from the
logic that decides what the evidence means, so the decision logic is
unit-testable without the live system at all. This module applies that same
split to Kubernetes. `list_pod_statuses()` and `list_warning_events()` are
the only functions here that call the Kubernetes API -- each takes a client
object as its first argument, the same dependency-injection convention
`cloudaudit.gather_bucket_evidence()` and every AWS-backed module in this
project already use (a client is a parameter, never a hidden module-level
global), so a test can hand either function a fake client with no real
cluster running at all. `rollout_state()` is the pure half: it takes an
already-built `DeploymentStatus` and returns one word describing the
rollout, with zero network calls and zero imports from `kubernetes.client`
in its body.

This module makes **no create, patch, delete or replace call of any kind**.
It is a read-only inspector, the same discipline `cloudaudit.py` applies to
AWS resources -- looking, not changing, is Module 28's whole job; the
governed, approval-gated version that IS allowed to act on a cluster is
Module 29's `Safe Kubernetes Operations`, not this one. The Deep Dive proves
this mechanically, the same way `cloudaudit.py`'s Deep Dive greps for
write-mode AWS calls: grepping this file for `create_`, `patch_`, `delete_`
and `replace_` finds nothing.

A `Deployment`'s Pods are never looked up by name -- they are discovered
through the Deployment's own `spec.selector.matchLabels`, turned into a
label selector for `list_namespaced_pod()`. That is the same relationship
`kubectl get pods -l app=payments` relies on: a Deployment does not "own" a
fixed list of Pod names, it owns a ReplicaSet that creates and destroys
Pods with generated names as it scales and rolls out, and the label
selector is the only stable way to find "the Pods that belong to this
Deployment" at any one moment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from kubernetes.client.exceptions import ApiException


@dataclass
class ContainerState:
    """One container inside one Pod, reduced to what an operator checks first during triage.

    `state` is always one of `"running"`, `"waiting"` or `"terminated"` --
    the three states the Kubernetes API itself uses on
    `container_status.state`. `reason` is `None` for a healthy `"running"`
    container and a short code (`CrashLoopBackOff`, `ImagePullBackOff`,
    `ErrImagePull`, `Completed`, `Error`, `ContainerCreating`) for the other
    two -- exactly the value `kubectl describe pod` prints on its own
    `State:`/`Reason:` lines.
    """

    name: str
    image: str
    ready: bool
    restart_count: int
    state: str
    reason: str | None
    message: str | None


@dataclass
class PodStatus:
    """One Pod belonging to the inspected Deployment.

    `containers` is an empty list for a Pod stuck in `Pending` -- the
    scheduler has not placed it on a node yet, so kubelet has never
    reported a container status for it at all. That is a real, honest
    state, not a bug: `list_pod_statuses()` never invents a placeholder
    container to fill the gap.
    """

    name: str
    phase: str
    containers: list[ContainerState]


@dataclass
class WarningEvent:
    """One Warning-type Event tied to this Deployment, one of its Pods, or both."""

    reason: str
    message: str
    involved_object: str
    count: int
    last_seen: str | None


@dataclass
class DeploymentStatus:
    """A Deployment's replica counts and conditions, plus its pod-selector -- everything `rollout_state()` needs.

    `ready_replicas`, `available_replicas` and `unavailable_replicas` are
    `None` -- not `0` -- straight from the Kubernetes API whenever a
    Deployment has never had any replicas in that state; `rollout_state()`
    treats a `None` exactly like `0` rather than requiring every caller to
    normalize it first.
    """

    name: str
    namespace: str
    desired_replicas: int
    ready_replicas: int | None
    available_replicas: int | None
    updated_replicas: int | None
    unavailable_replicas: int | None
    conditions: list[dict[str, str | None]]
    selector: dict[str, str]


def _container_state(raw_state: Any) -> tuple[str, str | None, str | None]:
    """Turn a `V1ContainerState`'s three mutually-exclusive fields into one `(state, reason, message)` triple."""
    if raw_state.running is not None:
        return "running", None, None
    if raw_state.waiting is not None:
        return "waiting", raw_state.waiting.reason, raw_state.waiting.message
    if raw_state.terminated is not None:
        return "terminated", raw_state.terminated.reason, raw_state.terminated.message
    return "unknown", None, None


def get_deployment_status(
    apps_client: Any, *, name: str, namespace: str
) -> DeploymentStatus:
    """Read one Deployment's replica counts, conditions and pod-selector. Raises `ApiException` if it does not exist."""
    deployment = apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    status = deployment.status
    conditions = [
        {
            "type": c.type,
            "status": c.status,
            "reason": c.reason,
            "message": c.message,
        }
        for c in (status.conditions or [])
    ]
    return DeploymentStatus(
        name=name,
        namespace=namespace,
        desired_replicas=deployment.spec.replicas or 0,
        ready_replicas=status.ready_replicas,
        available_replicas=status.available_replicas,
        updated_replicas=status.updated_replicas,
        unavailable_replicas=status.unavailable_replicas,
        conditions=conditions,
        selector=dict(deployment.spec.selector.match_labels or {}),
    )


def list_pod_statuses(
    core_client: Any, *, namespace: str, label_selector: str
) -> list[PodStatus]:
    """List every Pod matching `label_selector`, with each container's state, restart count and image.

    `label_selector` is built from the Deployment's own `spec.selector`,
    never a guessed or hardcoded name pattern -- the same relationship
    `kubectl get pods -l ...` relies on to find a Deployment's Pods.
    """
    pods = core_client.list_namespaced_pod(namespace, label_selector=label_selector)
    result: list[PodStatus] = []
    for pod in pods.items:
        containers = []
        for cs in pod.status.container_statuses or []:
            state, reason, message = _container_state(cs.state)
            containers.append(
                ContainerState(
                    name=cs.name,
                    image=cs.image,
                    ready=cs.ready,
                    restart_count=cs.restart_count,
                    state=state,
                    reason=reason,
                    message=message,
                )
            )
        result.append(
            PodStatus(
                name=pod.metadata.name, phase=pod.status.phase, containers=containers
            )
        )
    return result


def list_warning_events(
    core_client: Any, *, namespace: str, related_names: set[str]
) -> list[WarningEvent]:
    """List every Warning-type Event whose `involvedObject.name` is in `related_names`.

    `related_names` is the Deployment's own name plus its currently-listed
    Pods' names -- objects that exist right now. A Pod from a Deployment's
    *previous* rollout can still have Warning events sitting in the
    cluster's event history (Kubernetes keeps events for about an hour by
    default) even after that Pod is gone; matching against the current Pod
    list, not a name prefix, keeps a stale rollout's events out of a fresh
    inspection instead of resurfacing a problem that has already rolled
    forward.
    """
    events = core_client.list_namespaced_event(namespace)
    result: list[WarningEvent] = []
    for event in events.items:
        if event.type != "Warning":
            continue
        involved = event.involved_object
        if involved is None or involved.name not in related_names:
            continue
        result.append(
            WarningEvent(
                reason=event.reason,
                message=event.message,
                involved_object=involved.name,
                count=event.count or 1,
                last_seen=event.last_timestamp.isoformat()
                if event.last_timestamp
                else None,
            )
        )
    return result


def rollout_state(deployment: DeploymentStatus) -> str:
    """Summarize a Deployment's rollout in one word -- pure, no client, no network call.

    - `"scaled-to-zero"` -- `desired_replicas` is `0`; nothing is expected
      to be running, so `0` ready is not a problem.
    - `"healthy"` -- `ready_replicas` and `available_replicas` both equal
      `desired_replicas`. Every replica the Deployment wants is up and has
      stayed up past its `minReadySeconds` window.
    - `"unavailable"` -- no replica is ready at all (`ready_replicas` is
      `None` or `0`) while replicas are desired -- the CrashLoopBackOff,
      ImagePullBackOff and Pending scenarios this module's lab builds all
      land here.
    - `"degraded"` -- somewhere in between: at least one replica is ready,
      but fewer than desired -- a partial rollout or a partial failure,
      worth a closer look but not a total outage.
    """
    if deployment.desired_replicas == 0:
        return "scaled-to-zero"
    ready = deployment.ready_replicas or 0
    available = deployment.available_replicas or 0
    if (
        ready == deployment.desired_replicas
        and available == deployment.desired_replicas
    ):
        return "healthy"
    if ready == 0:
        return "unavailable"
    return "degraded"


@dataclass
class DeploymentResourceProfile:
    """One Deployment's labels and each container's requested resources -- a second, deliberately separate read.

    `DeploymentStatus` (above) answers "is this Deployment healthy" and
    never carries labels or resource requests -- adding them there would
    mean every caller that only wants rollout health starts paying for two
    concerns it never asked about. `platformops.ai_release_readiness`
    (Module 34) is the first caller that needs both: a `model-version`
    label is how it confirms the Deployment actually running is the model
    version the registry says it should be, and `container_resource_requests`
    is the GPU/CPU/memory metadata it checks was declared at all. This
    module never judges whether a requested amount is *enough* -- only
    whether one was set.
    """

    name: str
    namespace: str
    labels: dict[str, str]
    container_resource_requests: dict[str, dict[str, str]]


def get_deployment_resource_profile(
    apps_client: Any, *, name: str, namespace: str
) -> DeploymentResourceProfile:
    """Read one Deployment's labels and its containers' resource requests. Raises `ApiException` if it does not exist."""
    deployment = apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    labels = dict(deployment.metadata.labels or {})
    containers = deployment.spec.template.spec.containers or []
    container_resource_requests = {
        container.name: dict(container.resources.requests or {})
        if container.resources is not None
        else {}
        for container in containers
    }
    return DeploymentResourceProfile(
        name=name,
        namespace=namespace,
        labels=labels,
        container_resource_requests=container_resource_requests,
    )


def _error_code(exc: ApiException) -> str:
    """The HTTP status code an `ApiException` carries, as a string -- this module's equivalent of an AWS `Error.Code`."""
    return str(exc.status)


def inspect_workload(
    apps_client: Any, core_client: Any, *, name: str, namespace: str
) -> dict[str, Any]:
    """The one call a CLI command needs: gather a Deployment's status, its Pods, and its warning events.

    Only `ApiException` is caught by name here -- the same "catch the one
    expected failure type, let a real bug stay loud" discipline
    `cloudinventory.scan_inventory()` applies to `botocore.exceptions`. A
    Deployment that does not exist, or a namespace this client's RBAC
    cannot read, both come back as `ApiException` and both degrade to a
    structured `{"status": "error", ...}` report instead of a traceback --
    no Pod or Event call is ever attempted once the Deployment lookup
    itself fails.
    """
    try:
        deployment = get_deployment_status(apps_client, name=name, namespace=namespace)
    except ApiException as exc:
        return {"status": "error", "error": _error_code(exc), "message": str(exc)}

    label_selector = ",".join(f"{k}={v}" for k, v in deployment.selector.items())

    try:
        pods = list_pod_statuses(
            core_client, namespace=namespace, label_selector=label_selector
        )
        related_names = {deployment.name} | {pod.name for pod in pods}
        events = list_warning_events(
            core_client, namespace=namespace, related_names=related_names
        )
    except ApiException as exc:
        return {"status": "error", "error": _error_code(exc), "message": str(exc)}

    return {
        "status": "ok",
        "deployment": asdict(deployment),
        "rollout_state": rollout_state(deployment),
        "pods": [asdict(pod) for pod in pods],
        "warning_events": [asdict(event) for event in events],
    }


__all__ = [
    "ContainerState",
    "PodStatus",
    "WarningEvent",
    "DeploymentStatus",
    "DeploymentResourceProfile",
    "get_deployment_status",
    "get_deployment_resource_profile",
    "list_pod_statuses",
    "list_warning_events",
    "rollout_state",
    "inspect_workload",
]
