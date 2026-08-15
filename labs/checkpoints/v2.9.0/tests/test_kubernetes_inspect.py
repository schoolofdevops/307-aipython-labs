from datetime import UTC, datetime

import pytest
from kubernetes.client.exceptions import ApiException

from platformops import kubernetes_inspect
from platformops.kubernetes_inspect import (
    ContainerState,
    DeploymentResourceProfile,
    DeploymentStatus,
    PodStatus,
    WarningEvent,
    get_deployment_resource_profile,
    inspect_workload,
    list_pod_statuses,
    list_warning_events,
    rollout_state,
)

# ---------------------------------------------------------------------------
# rollout_state -- pure, no client of any kind. Every test hand-builds a
# DeploymentStatus directly; none of them needs a real or fake Kubernetes
# client, the same discipline cloudaudit.evaluate_policy()'s tests use for
# hand-built ResourceEvidence.
# ---------------------------------------------------------------------------


def _deployment_status(**overrides):
    defaults = dict(
        name="payments",
        namespace="payments",
        desired_replicas=2,
        ready_replicas=2,
        available_replicas=2,
        updated_replicas=2,
        unavailable_replicas=None,
        conditions=[],
        selector={"app": "payments"},
    )
    defaults.update(overrides)
    return DeploymentStatus(**defaults)


def test_rollout_state_is_healthy_when_ready_and_available_match_desired():
    status = _deployment_status(
        desired_replicas=2, ready_replicas=2, available_replicas=2
    )

    assert rollout_state(status) == "healthy"


def test_rollout_state_is_unavailable_when_nothing_is_ready():
    status = _deployment_status(
        desired_replicas=2, ready_replicas=None, available_replicas=None
    )

    assert rollout_state(status) == "unavailable"


def test_rollout_state_is_degraded_when_partially_ready():
    status = _deployment_status(
        desired_replicas=2, ready_replicas=1, available_replicas=1
    )

    assert rollout_state(status) == "degraded"


def test_rollout_state_is_scaled_to_zero_when_desired_is_zero():
    status = _deployment_status(
        desired_replicas=0, ready_replicas=None, available_replicas=None
    )

    assert rollout_state(status) == "scaled-to-zero"


# ---------------------------------------------------------------------------
# list_pod_statuses / list_warning_events -- the only functions in this
# module that touch the Kubernetes API. A hand-built fake client (not a real
# cluster, not moto -- there is no Kubernetes equivalent of moto in this
# project) proves each function reads the right fields and calls the right
# method with the right arguments.
# ---------------------------------------------------------------------------


class _FakeContainerState:
    def __init__(self, *, running=None, waiting=None, terminated=None):
        self.running = running
        self.waiting = waiting
        self.terminated = terminated


class _FakeWaiting:
    def __init__(self, reason, message):
        self.reason = reason
        self.message = message


class _FakeContainerStatus:
    def __init__(self, *, name, image, ready, restart_count, state):
        self.name = name
        self.image = image
        self.ready = ready
        self.restart_count = restart_count
        self.state = state


class _FakePodStatus:
    def __init__(self, *, phase, container_statuses):
        self.phase = phase
        self.container_statuses = container_statuses


class _FakeMetadata:
    def __init__(self, name):
        self.name = name


class _FakePod:
    def __init__(self, *, name, phase, container_statuses):
        self.metadata = _FakeMetadata(name)
        self.status = _FakePodStatus(phase=phase, container_statuses=container_statuses)


class _FakePodList:
    def __init__(self, items):
        self.items = items


class _FakeCoreV1Api:
    """Records every call this test cares about; returns canned responses."""

    def __init__(self, *, pods=None, events=None):
        self._pods = pods or []
        self._events = events or []
        self.list_namespaced_pod_calls = []
        self.list_namespaced_event_calls = []

    def list_namespaced_pod(self, namespace, *, label_selector=None):
        self.list_namespaced_pod_calls.append(
            {"namespace": namespace, "label_selector": label_selector}
        )
        return _FakePodList(self._pods)

    def list_namespaced_event(self, namespace):
        self.list_namespaced_event_calls.append({"namespace": namespace})
        return _FakePodList(self._events)


def test_list_pod_statuses_reports_crashloopbackoff_reason():
    fake_client = _FakeCoreV1Api(
        pods=[
            _FakePod(
                name="payments-abc123-xyz",
                phase="Running",
                container_statuses=[
                    _FakeContainerStatus(
                        name="payments",
                        image="busybox:1.36",
                        ready=False,
                        restart_count=6,
                        state=_FakeContainerState(
                            waiting=_FakeWaiting(
                                "CrashLoopBackOff",
                                "back-off restarting failed container",
                            )
                        ),
                    )
                ],
            )
        ]
    )

    statuses = list_pod_statuses(
        fake_client, namespace="payments", label_selector="app=payments"
    )

    assert fake_client.list_namespaced_pod_calls == [
        {"namespace": "payments", "label_selector": "app=payments"}
    ]
    assert statuses == [
        PodStatus(
            name="payments-abc123-xyz",
            phase="Running",
            containers=[
                ContainerState(
                    name="payments",
                    image="busybox:1.36",
                    ready=False,
                    restart_count=6,
                    state="waiting",
                    reason="CrashLoopBackOff",
                    message="back-off restarting failed container",
                )
            ],
        )
    ]


def test_list_pod_statuses_handles_a_pending_pod_with_no_container_statuses():
    fake_client = _FakeCoreV1Api(
        pods=[
            _FakePod(
                name="payments-pending-1", phase="Pending", container_statuses=None
            )
        ]
    )

    statuses = list_pod_statuses(
        fake_client, namespace="payments", label_selector="app=payments"
    )

    assert statuses == [
        PodStatus(name="payments-pending-1", phase="Pending", containers=[])
    ]


class _FakeInvolvedObject:
    def __init__(self, name):
        self.name = name


class _FakeEvent:
    def __init__(
        self, *, type, reason, message, involved_object_name, count, last_timestamp
    ):
        self.type = type
        self.reason = reason
        self.message = message
        self.involved_object = _FakeInvolvedObject(involved_object_name)
        self.count = count
        self.last_timestamp = last_timestamp


def test_list_warning_events_only_returns_warning_type_for_related_names():
    fake_client = _FakeCoreV1Api(
        events=[
            _FakeEvent(
                type="Warning",
                reason="BackOff",
                message="Back-off restarting failed container",
                involved_object_name="payments-abc123-xyz",
                count=6,
                last_timestamp=datetime(2026, 8, 5, tzinfo=UTC),
            ),
            _FakeEvent(
                type="Normal",
                reason="Scheduled",
                message="Successfully assigned",
                involved_object_name="payments-abc123-xyz",
                count=1,
                last_timestamp=datetime(2026, 8, 5, tzinfo=UTC),
            ),
            _FakeEvent(
                type="Warning",
                reason="BackOff",
                message="stale event from an old, unrelated pod",
                involved_object_name="payments-old-pod-not-related",
                count=1,
                last_timestamp=datetime(2026, 8, 5, tzinfo=UTC),
            ),
        ]
    )

    events = list_warning_events(
        fake_client,
        namespace="payments",
        related_names={"payments", "payments-abc123-xyz"},
    )

    assert fake_client.list_namespaced_event_calls == [{"namespace": "payments"}]
    assert events == [
        WarningEvent(
            reason="BackOff",
            message="Back-off restarting failed container",
            involved_object="payments-abc123-xyz",
            count=6,
            last_seen="2026-08-05T00:00:00+00:00",
        )
    ]


# ---------------------------------------------------------------------------
# inspect_workload -- the one call a CLI command needs. Composes the above
# with get_deployment_status(); an ApiException (a missing Deployment, a
# forbidden namespace) degrades to a structured error, never a crash.
# ---------------------------------------------------------------------------


class _FakeAppsV1Api:
    def __init__(self, *, deployment=None, raises=None):
        self._deployment = deployment
        self._raises = raises
        self.read_namespaced_deployment_calls = []

    def read_namespaced_deployment(self, name, namespace):
        self.read_namespaced_deployment_calls.append(
            {"name": name, "namespace": namespace}
        )
        if self._raises is not None:
            raise self._raises
        return self._deployment


class _FakeDeploymentSpec:
    def __init__(self, *, replicas, match_labels):
        self.replicas = replicas
        self.selector = _FakeLabelSelector(match_labels)


class _FakeLabelSelector:
    def __init__(self, match_labels):
        self.match_labels = match_labels


class _FakeDeploymentStatusRaw:
    def __init__(
        self,
        *,
        ready_replicas,
        available_replicas,
        updated_replicas,
        unavailable_replicas,
        conditions,
    ):
        self.ready_replicas = ready_replicas
        self.available_replicas = available_replicas
        self.updated_replicas = updated_replicas
        self.unavailable_replicas = unavailable_replicas
        self.conditions = conditions


class _FakeDeployment:
    def __init__(self, *, spec, status):
        self.spec = spec
        self.status = status


def _healthy_fake_deployment():
    return _FakeDeployment(
        spec=_FakeDeploymentSpec(replicas=2, match_labels={"app": "payments"}),
        status=_FakeDeploymentStatusRaw(
            ready_replicas=2,
            available_replicas=2,
            updated_replicas=2,
            unavailable_replicas=None,
            conditions=[],
        ),
    )


def test_inspect_workload_returns_ok_report_for_a_healthy_deployment():
    apps_client = _FakeAppsV1Api(deployment=_healthy_fake_deployment())
    core_client = _FakeCoreV1Api(
        pods=[
            _FakePod(
                name="payments-abc-1",
                phase="Running",
                container_statuses=[
                    _FakeContainerStatus(
                        name="payments",
                        image="busybox:1.36",
                        ready=True,
                        restart_count=0,
                        state=_FakeContainerState(running=object()),
                    )
                ],
            )
        ],
        events=[],
    )

    report = inspect_workload(
        apps_client, core_client, name="payments", namespace="payments"
    )

    assert report["status"] == "ok"
    assert report["rollout_state"] == "healthy"
    assert report["deployment"]["desired_replicas"] == 2
    assert len(report["pods"]) == 1
    assert apps_client.read_namespaced_deployment_calls == [
        {"name": "payments", "namespace": "payments"}
    ]
    assert core_client.list_namespaced_pod_calls == [
        {"namespace": "payments", "label_selector": "app=payments"}
    ]


def test_inspect_workload_returns_error_when_deployment_is_not_found():
    apps_client = _FakeAppsV1Api(raises=ApiException(status=404, reason="Not Found"))
    core_client = _FakeCoreV1Api()

    report = inspect_workload(
        apps_client, core_client, name="does-not-exist", namespace="payments"
    )

    assert report["status"] == "error"
    assert report["error"] == "404"
    # No pod or event call is ever made once the Deployment lookup fails.
    assert core_client.list_namespaced_pod_calls == []


def test_inspect_workload_never_calls_a_write_method_on_either_client():
    """Documents the read-only contract this module's docstring and Deep Dive both rely on."""
    for verb in ("create", "patch", "delete", "replace"):
        assert not hasattr(kubernetes_inspect, f"{verb}_namespaced_deployment")


# ---------------------------------------------------------------------------
# get_deployment_resource_profile -- a second, deliberately separate read of
# the same Deployment object, for the labels and resource requests
# DeploymentStatus never captures (Module 34). A deployment's own
# `model-version` label is how a release-readiness check correlates "what
# the model registry says is the right version" against "what is actually
# running"; its containers' resource requests are the GPU/CPU/memory
# metadata that check needs to see configured at all -- this function never
# judges whether the requested amount is enough, only what was declared.
# ---------------------------------------------------------------------------


class _FakeResourceRequirements:
    def __init__(self, requests):
        self.requests = requests


class _FakeContainerSpec:
    def __init__(self, *, name, resources):
        self.name = name
        self.resources = resources


class _FakePodSpec:
    def __init__(self, *, containers):
        self.containers = containers


class _FakePodTemplateSpec:
    def __init__(self, *, spec):
        self.spec = spec


class _FakeDeploymentSpecWithTemplate:
    def __init__(self, *, template):
        self.template = template


class _FakeDeploymentMetadata:
    def __init__(self, labels):
        self.labels = labels


class _FakeDeploymentWithMetadata:
    def __init__(self, *, metadata, spec):
        self.metadata = metadata
        self.spec = spec


def test_get_deployment_resource_profile_reads_labels_and_gpu_resource_requests():
    deployment = _FakeDeploymentWithMetadata(
        metadata=_FakeDeploymentMetadata(
            labels={"app": "support-assistant", "model-version": "4"}
        ),
        spec=_FakeDeploymentSpecWithTemplate(
            template=_FakePodTemplateSpec(
                spec=_FakePodSpec(
                    containers=[
                        _FakeContainerSpec(
                            name="support-assistant",
                            resources=_FakeResourceRequirements(
                                requests={
                                    "cpu": "2",
                                    "memory": "8Gi",
                                    "nvidia.com/gpu": "1",
                                }
                            ),
                        )
                    ]
                )
            )
        ),
    )
    apps_client = _FakeAppsV1Api(deployment=deployment)

    profile = get_deployment_resource_profile(
        apps_client, name="support-assistant", namespace="ml-serving"
    )

    assert profile == DeploymentResourceProfile(
        name="support-assistant",
        namespace="ml-serving",
        labels={"app": "support-assistant", "model-version": "4"},
        container_resource_requests={
            "support-assistant": {"cpu": "2", "memory": "8Gi", "nvidia.com/gpu": "1"}
        },
    )
    assert apps_client.read_namespaced_deployment_calls == [
        {"name": "support-assistant", "namespace": "ml-serving"}
    ]


def test_get_deployment_resource_profile_handles_no_labels_and_no_resource_requests():
    deployment = _FakeDeploymentWithMetadata(
        metadata=_FakeDeploymentMetadata(labels=None),
        spec=_FakeDeploymentSpecWithTemplate(
            template=_FakePodTemplateSpec(
                spec=_FakePodSpec(
                    containers=[
                        _FakeContainerSpec(
                            name="support-assistant",
                            resources=_FakeResourceRequirements(requests=None),
                        )
                    ]
                )
            )
        ),
    )
    apps_client = _FakeAppsV1Api(deployment=deployment)

    profile = get_deployment_resource_profile(
        apps_client, name="support-assistant", namespace="ml-serving"
    )

    assert profile.labels == {}
    assert profile.container_resource_requests == {"support-assistant": {}}


def test_get_deployment_resource_profile_raises_api_exception_for_a_missing_deployment():
    apps_client = _FakeAppsV1Api(raises=ApiException(status=404, reason="Not Found"))

    with pytest.raises(ApiException):
        get_deployment_resource_profile(
            apps_client, name="does-not-exist", namespace="ml-serving"
        )
