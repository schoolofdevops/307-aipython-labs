from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime

import pytest
from kubernetes.client.exceptions import ApiException

from platformops import kubernetes_restart
from platformops.kubernetes_restart import (
    RESTART_ANNOTATION,
    RestartCoolingDownError,
    RestartNotApprovedError,
    RestartPlan,
    append_audit_log,
    build_restart_plan,
    execute_restart,
    last_restart_at,
    load_ops_config,
    monitor_rollout,
)

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
ALLOWLIST = ["checkout"]


# ---------------------------------------------------------------------------
# Fake AppsV1Api -- a hand-built stand-in, the same discipline
# test_kubernetes_inspect.py uses. Never a real cluster, never moto (there is
# no Kubernetes equivalent of moto in this project).
# ---------------------------------------------------------------------------


class _FakeSelector:
    def __init__(self, match_labels):
        self.match_labels = match_labels


class _FakeSpec:
    def __init__(self, *, replicas, selector):
        self.replicas = replicas
        self.selector = _FakeSelector(selector)


class _FakeStatus:
    def __init__(self, *, ready, available, updated, unavailable=None):
        self.ready_replicas = ready
        self.available_replicas = available
        self.updated_replicas = updated
        self.unavailable_replicas = unavailable
        self.conditions = []


class _FakeDeployment:
    def __init__(
        self, *, replicas=2, ready=2, available=2, updated=2, unavailable=None
    ):
        self.spec = _FakeSpec(replicas=replicas, selector={"app": "checkout-web"})
        self.status = _FakeStatus(
            ready=ready, available=available, updated=updated, unavailable=unavailable
        )


class _FakeAppsV1Api:
    """`sequence` is returned across successive `read_namespaced_deployment` calls; the last entry repeats once exhausted."""

    def __init__(self, *, sequence=None, not_found=False):
        self._sequence = list(sequence) if sequence else [_FakeDeployment()]
        self._index = 0
        self._not_found = not_found
        self.read_calls: list[tuple[str, str]] = []
        self.patch_calls: list[dict] = []

    def read_namespaced_deployment(self, name, namespace):
        self.read_calls.append((name, namespace))
        if self._not_found:
            raise ApiException(status=404, reason="Not Found")
        state = self._sequence[min(self._index, len(self._sequence) - 1)]
        self._index += 1
        return state

    def patch_namespaced_deployment(self, name, namespace, body, dry_run=None):
        self.patch_calls.append(
            {"name": name, "namespace": namespace, "body": body, "dry_run": dry_run}
        )


def _clock(steps):
    it = iter(steps)
    return lambda: next(it)


def _no_sleep(_seconds):
    pass


# ---------------------------------------------------------------------------
# build_restart_plan -- the namespace allowlist gate is checked FIRST, before
# any client call at all. A namespace off the list never even reads the
# Deployment.
# ---------------------------------------------------------------------------


def test_build_restart_plan_refuses_a_namespace_off_the_allowlist_without_any_client_call():
    client = _FakeAppsV1Api()

    plan = build_restart_plan(
        client, namespace="billing", name="invoices", allowlist=ALLOWLIST, now=NOW
    )

    assert plan.status == "not_allowed"
    assert "not on the namespace allowlist" in plan.reason
    assert client.read_calls == []
    assert client.patch_calls == []


def test_build_restart_plan_reports_not_found_for_a_missing_deployment():
    client = _FakeAppsV1Api(not_found=True)

    plan = build_restart_plan(
        client, namespace="checkout", name="ghost", allowlist=ALLOWLIST, now=NOW
    )

    assert plan.status == "not_found"
    assert "could not read Deployment" in plan.reason


def test_build_restart_plan_validates_with_a_server_side_dry_run_only():
    client = _FakeAppsV1Api(sequence=[_FakeDeployment(ready=2, available=2, updated=2)])

    plan = build_restart_plan(
        client, namespace="checkout", name="checkout-web", allowlist=ALLOWLIST, now=NOW
    )

    assert plan.status == "plannable"
    assert plan.dry_run_validated is True
    assert plan.before_rollout_state == "healthy"
    assert plan.action.api_call == "patch_namespaced_deployment"
    assert len(client.patch_calls) == 1
    assert client.patch_calls[0]["dry_run"] == "All"
    assert (
        RESTART_ANNOTATION
        in client.patch_calls[0]["body"]["spec"]["template"]["metadata"]["annotations"]
    )


def test_build_restart_plan_before_rollout_state_reflects_a_broken_deployment():
    client = _FakeAppsV1Api(
        sequence=[_FakeDeployment(ready=None, available=None, updated=0)]
    )

    plan = build_restart_plan(
        client,
        namespace="checkout",
        name="checkout-worker",
        allowlist=ALLOWLIST,
        now=NOW,
    )

    assert plan.status == "plannable"
    assert plan.before_rollout_state == "unavailable"


# ---------------------------------------------------------------------------
# The read-only vs write-capable boundary, provable by grep -- the same
# discipline M24/M25/M28 use. Kubernetes' own dry_run="All" lets the planner
# call the SAME method name the real mutator uses without ever persisting,
# so the proof here is sharper than "the method is never called": the only
# LITERAL `.patch_namespaced_deployment(` call site anywhere in this module
# is the dry-run one inside build_restart_plan(). The real mutation in
# execute_restart() is dispatched dynamically via getattr(), so its method
# name never appears as a literal write-shaped call at all.
# ---------------------------------------------------------------------------


def test_only_one_literal_patch_call_site_exists_in_the_whole_module():
    source = inspect.getsource(kubernetes_restart)
    literal_calls = re.findall(r"\.patch_namespaced_deployment\(", source)
    assert len(literal_calls) == 1


def test_that_one_literal_call_site_is_inside_build_restart_plan_and_is_dry_run_all():
    source = inspect.getsource(kubernetes_restart.build_restart_plan)
    assert re.search(r"\.patch_namespaced_deployment\(", source)
    assert 'dry_run="All"' in source


def test_execute_restart_has_no_literal_write_call_it_dispatches_dynamically():
    source = inspect.getsource(kubernetes_restart.execute_restart)
    assert not re.search(r"\.patch_namespaced_deployment\(", source)
    assert "getattr(apps_client, action.api_call)" in source


def test_kubernetes_restart_module_makes_no_create_delete_or_replace_call():
    source = inspect.getsource(kubernetes_restart)
    assert not re.search(r"\.(create|delete|replace)_namespaced", source)


# ---------------------------------------------------------------------------
# execute_restart -- not_allowed/not_found pass straight through, a
# plannable restart without --approve raises, and an approved restart
# rebuilds the plan fresh before ever touching the cluster for real.
# ---------------------------------------------------------------------------


def test_execute_restart_passes_through_not_allowed_without_requiring_approve():
    plan = RestartPlan(namespace="billing", name="invoices", status="not_allowed",
                        reason="namespace 'billing' is not on the namespace allowlist")
    client = _FakeAppsV1Api()

    result = execute_restart(
        client,
        plan,
        approve=False,
        allowlist=ALLOWLIST,
        cooldown_seconds=120,
        rollout_timeout_seconds=60,
        now=NOW,
    )

    assert result.status == "not_allowed"
    assert client.patch_calls == []


def test_execute_restart_raises_without_approve_for_a_plannable_restart():
    client = _FakeAppsV1Api(sequence=[_FakeDeployment(ready=2, available=2, updated=2)])
    plan = build_restart_plan(
        client, namespace="checkout", name="checkout-web", allowlist=ALLOWLIST, now=NOW
    )
    client.patch_calls.clear()

    with pytest.raises(RestartNotApprovedError):
        execute_restart(
            client,
            plan,
            approve=False,
            allowlist=ALLOWLIST,
            cooldown_seconds=120,
            rollout_timeout_seconds=60,
            now=NOW,
        )

    assert client.patch_calls == []


def test_execute_restart_raises_when_cooling_down(tmp_path):
    audit_log = tmp_path / "audit.jsonl"
    append_audit_log(
        audit_log,
        {
            "timestamp": datetime(2026, 8, 6, 11, 59, 30, tzinfo=UTC).isoformat(),
            "namespace": "checkout",
            "name": "checkout-web",
            "status": "restarted",
        },
    )
    client = _FakeAppsV1Api(sequence=[_FakeDeployment(ready=2, available=2, updated=2)])
    plan = build_restart_plan(
        client, namespace="checkout", name="checkout-web", allowlist=ALLOWLIST, now=NOW
    )
    client.patch_calls.clear()

    with pytest.raises(RestartCoolingDownError) as exc_info:
        execute_restart(
            client,
            plan,
            approve=True,
            allowlist=ALLOWLIST,
            cooldown_seconds=120,
            rollout_timeout_seconds=60,
            audit_log_path=audit_log,
            now=NOW,
        )

    # 30s elapsed of a 120s cooldown -- ~90s remaining.
    assert 85 <= exc_info.value.seconds_remaining <= 95
    assert client.patch_calls == []


def test_execute_restart_succeeds_and_reports_rolled_out(tmp_path):
    audit_log = tmp_path / "audit.jsonl"
    client = _FakeAppsV1Api(
        sequence=[
            _FakeDeployment(ready=None, available=None, updated=0),  # plan's read
            _FakeDeployment(ready=None, available=None, updated=0),  # monitor poll 1
            _FakeDeployment(ready=2, available=2, updated=2),  # monitor poll 2 -- rolled out
        ]
    )
    plan = build_restart_plan(
        client, namespace="checkout", name="checkout-web", allowlist=ALLOWLIST, now=NOW
    )
    client.patch_calls.clear()

    result = execute_restart(
        client,
        plan,
        approve=True,
        allowlist=ALLOWLIST,
        cooldown_seconds=120,
        rollout_timeout_seconds=30,
        rollout_poll_interval_seconds=2,
        audit_log_path=audit_log,
        now=NOW,
        sleep_fn=_no_sleep,
        clock_fn=_clock([0, 5, 10]),
    )

    assert result.status == "restarted"
    assert result.rollout_outcome == "rolled_out"
    assert result.after_rollout_state == "healthy"
    # execute_restart() rebuilds the plan fresh (one more dry-run patch
    # call) before making exactly one REAL patch call -- the only one with
    # dry_run=None.
    real_patch_calls = [c for c in client.patch_calls if c["dry_run"] is None]
    dry_run_calls = [c for c in client.patch_calls if c["dry_run"] == "All"]
    assert len(real_patch_calls) == 1
    assert len(dry_run_calls) == 1

    lines = audit_log.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["rollout_outcome"] == "rolled_out"
    assert record["namespace"] == "checkout"
    assert record["name"] == "checkout-web"


def test_execute_restart_reports_timed_out_for_a_permanently_broken_deployment(tmp_path):
    audit_log = tmp_path / "audit.jsonl"
    # Never recovers -- simulates a bad image tag: restart is triggered, but
    # the rollout never becomes healthy.
    broken = _FakeDeployment(ready=None, available=None, updated=0)
    client = _FakeAppsV1Api(sequence=[broken, broken, broken, broken])
    plan = build_restart_plan(
        client, namespace="checkout", name="checkout-worker", allowlist=ALLOWLIST, now=NOW
    )
    client.patch_calls.clear()

    result = execute_restart(
        client,
        plan,
        approve=True,
        allowlist=ALLOWLIST,
        cooldown_seconds=120,
        rollout_timeout_seconds=6,
        rollout_poll_interval_seconds=2,
        audit_log_path=audit_log,
        now=NOW,
        sleep_fn=_no_sleep,
        clock_fn=_clock([0, 2, 4, 6, 8]),
    )

    assert result.status == "restarted"
    assert result.rollout_outcome == "timed_out"
    assert "did NOT complete" in result.message

    record = json.loads(audit_log.read_text().strip().splitlines()[0])
    assert record["rollout_outcome"] == "timed_out"


# ---------------------------------------------------------------------------
# monitor_rollout -- pure polling logic, tested directly with an injected
# fake clock and a no-op sleep so the test suite never actually waits.
# ---------------------------------------------------------------------------


def test_monitor_rollout_returns_rolled_out_once_updated_replicas_match_desired():
    client = _FakeAppsV1Api(
        sequence=[
            _FakeDeployment(ready=1, available=1, updated=1),
            _FakeDeployment(ready=2, available=2, updated=2),
        ]
    )

    # One clock read at start, then one more per polling iteration -- two
    # iterations here (first degraded, second healthy), so three clock
    # values are consumed in total.
    outcome, state, elapsed = monitor_rollout(
        client,
        namespace="checkout",
        name="checkout-web",
        timeout_seconds=30,
        poll_interval_seconds=2,
        sleep_fn=_no_sleep,
        clock_fn=_clock([0, 2, 4]),
    )

    assert outcome == "rolled_out"
    assert state == "healthy"
    assert elapsed == 4


def test_monitor_rollout_returns_timed_out_when_the_deadline_passes():
    broken = _FakeDeployment(ready=None, available=None, updated=0)
    client = _FakeAppsV1Api(sequence=[broken, broken, broken])

    outcome, state, elapsed = monitor_rollout(
        client,
        namespace="checkout",
        name="checkout-worker",
        timeout_seconds=4,
        poll_interval_seconds=2,
        sleep_fn=_no_sleep,
        clock_fn=_clock([0, 2, 4]),
    )

    assert outcome == "timed_out"
    assert state == "unavailable"
    assert elapsed == 4


# ---------------------------------------------------------------------------
# last_restart_at -- the cooldown gate's memory. Reads from disk, filters by
# exact (namespace, name) and status == "restarted", ignores everything else.
# ---------------------------------------------------------------------------


def test_last_restart_at_returns_none_when_the_log_does_not_exist(tmp_path):
    assert last_restart_at(tmp_path / "missing.jsonl", namespace="checkout", name="checkout-web") is None


def test_last_restart_at_ignores_other_targets_and_non_restart_records(tmp_path):
    audit_log = tmp_path / "audit.jsonl"
    append_audit_log(audit_log, {
        "timestamp": datetime(2026, 8, 6, 10, 0, tzinfo=UTC).isoformat(),
        "namespace": "checkout", "name": "checkout-worker", "status": "restarted",
    })
    append_audit_log(audit_log, {
        "timestamp": datetime(2026, 8, 6, 11, 0, tzinfo=UTC).isoformat(),
        "namespace": "checkout", "name": "checkout-web", "status": "not_allowed",
    })

    assert last_restart_at(audit_log, namespace="checkout", name="checkout-web") is None


def test_last_restart_at_returns_the_most_recent_matching_timestamp(tmp_path):
    audit_log = tmp_path / "audit.jsonl"
    append_audit_log(audit_log, {
        "timestamp": datetime(2026, 8, 6, 9, 0, tzinfo=UTC).isoformat(),
        "namespace": "checkout", "name": "checkout-web", "status": "restarted",
    })
    append_audit_log(audit_log, {
        "timestamp": datetime(2026, 8, 6, 11, 0, tzinfo=UTC).isoformat(),
        "namespace": "checkout", "name": "checkout-web", "status": "restarted",
    })

    result = last_restart_at(audit_log, namespace="checkout", name="checkout-web")

    assert result == datetime(2026, 8, 6, 11, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# load_ops_config -- defaults filled in, same pattern as
# cloudremediate.load_remediation_config().
# ---------------------------------------------------------------------------


def test_load_ops_config_fills_in_defaults(tmp_path):
    config_path = tmp_path / "k8s-ops.yaml"
    config_path.write_text("namespace_allowlist:\n  - checkout\n")

    config = load_ops_config(config_path)

    assert config["namespace_allowlist"] == ["checkout"]
    assert config["cooldown_seconds"] == 120
    assert config["rollout_timeout_seconds"] == 60
    assert config["audit_log_path"] == "k8s-ops-audit.jsonl"
