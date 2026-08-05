"""platformops.kubernetes_restart -- restart a Deployment against a live cluster, deliberately.

Module 28's `kubernetes_inspect.py` is the read-only inspector: it looks at a
Deployment's real health and never once calls a write-mode Kubernetes API.
This module is the first in the whole project allowed to write to a live
cluster, the same shift `cloudremediate.py` (Module 25) made for AWS -- and
it is held to the same discipline: build a plan first (safe to run any
number of times), refuse to touch anything without an explicit `--approve`,
re-check right before mutating, record exactly what changed, and never let
the same target be restarted twice in a short window without a human
choosing that on purpose.

The only mutation this module ever performs is a **rollout restart** -- the
same mechanism `kubectl rollout restart deployment/<name>` uses: patching
the Deployment's pod template with a fresh
`kubectl.kubernetes.io/restartedAt` annotation, which the Deployment
controller reads as "the pod template changed" and rolls out new Pods for.
There is no other kind of "restart" a Deployment supports -- this module
does not invent one.

Two independent gates stand between a Deployment and a real restart.
**The namespace allowlist** (`namespace_allowlist` in the ops config) is
checked first, before anything else -- a namespace absent from it is always
`not_allowed`, even with `--approve`, even for a Deployment that genuinely
needs restarting. **`--approve`** is the second -- `execute_restart()`
raises rather than silently doing nothing when it is missing. Neither gate
can substitute for the other.

A rollout restart is not naturally idempotent the way a tag-and-encrypt
remediation is: running it twice is not "safe to no-op the second time," it
is "two real rollouts." So this module adds a third control M25 never
needed -- a **cooldown**, read from the audit log's own history, that
refuses to restart the same `(namespace, name)` target again inside
`cooldown_seconds` of its last real restart. This is what stops a runaway
loop -- an agent, a script, or a person retrying too fast -- from turning
one governed action into an unbounded stream of rollouts.

`build_restart_plan()` never patches for real. It validates the exact patch
Kubernetes' own API server would apply using `dry_run="All"` -- the API
server checks the object is well-formed and would be accepted, but persists
nothing. `execute_restart()` rebuilds that plan fresh, immediately before
mutating, the same "prove it's still valid right now" idempotency guarantee
`execute_remediation_plan()` uses in Module 25 -- so the namespace allowlist
and the dry-run validation both apply at execute time too, not only when a
caller happens to have called `build_restart_plan()` first.

Restarting a Deployment is honest about its limits. It forces the same
"try starting the containers again" a `kubectl rollout restart` would --
which recovers a Deployment stuck on a transient condition that has since
cleared (a dependency that was briefly unavailable, a ConfigMap that did
not exist yet when the Pods first started). It does **not** fix a
Deployment whose failure is permanent -- a container image tag that does
not exist will still not exist after the restart, and `monitor_rollout()`
will report `timed_out`, not `rolled_out`, when that happens. This module
never pretends otherwise.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kubernetes.client.exceptions import ApiException

from platformops.config import load_yaml_dict
from platformops.kubernetes_inspect import get_deployment_status, rollout_state

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
"""The exact annotation `kubectl rollout restart` writes -- there is no other Deployment restart mechanism."""


class RestartNotApprovedError(Exception):
    """Raised by `execute_restart()` when a plannable restart is run without `approve=True`.

    Carries the plan (`.plan`) so the caller -- the CLI command -- can print
    the exact plan the operator was about to reject. There is no code path
    here where omitting `--approve` looks like success.
    """

    def __init__(self, plan: RestartPlan) -> None:
        self.plan = plan
        super().__init__(
            f"restart of '{plan.namespace}/{plan.name}' requires --approve -- "
            "refusing to mutate"
        )


class RestartCoolingDownError(Exception):
    """Raised by `execute_restart()` when the target was restarted too recently.

    This is the loop guard: even with `--approve` and an allowlisted
    namespace, a second restart of the same `(namespace, name)` inside
    `cooldown_seconds` of the last one is refused outright, not queued or
    throttled silently. `seconds_remaining` is how much longer the caller
    has to wait.
    """

    def __init__(self, plan: RestartPlan, seconds_remaining: float) -> None:
        self.plan = plan
        self.seconds_remaining = seconds_remaining
        super().__init__(
            f"'{plan.namespace}/{plan.name}' was restarted less than "
            f"{round(seconds_remaining)}s ago -- refusing to restart again this soon "
            "(this is the runaway-restart-loop guard, not a bug)"
        )


@dataclass
class RestartAction:
    """The exact Kubernetes API call a plan would make -- `api_call` names the client method, `args` its kwargs."""

    api_call: str
    args: dict[str, Any]


@dataclass
class RestartPlan:
    """What `build_restart_plan()` returns -- always safe to compute, never mutates for real.

    `status` is one of `plannable` (a dry-run-validated `action` is ready to
    run), `not_allowed` (this namespace is not on the allowlist -- checked
    before anything else, including whether the Deployment even exists), or
    `not_found` (the Deployment does not exist in this namespace).
    `before_rollout_state` is `rollout_state()`'s summary of the Deployment
    at plan time -- the same word `workload-inspect` reports.
    """

    namespace: str
    name: str
    status: str
    reason: str | None = None
    action: RestartAction | None = None
    before_rollout_state: str | None = None
    dry_run_validated: bool = False


@dataclass
class RestartResult:
    """What `execute_restart()` returns -- the outcome of trying to act on a plan.

    `status` is `restarted` (a real rollout was triggered), `not_allowed`,
    `refused` (a plannable restart was attempted without approval -- only
    reachable by catching `RestartNotApprovedError`, not returned directly),
    or `cooling_down`. `rollout_outcome` is only meaningful for `restarted`:
    `rolled_out` when every replica came up healthy and updated within the
    timeout, `timed_out` otherwise -- a `timed_out` restart still happened,
    it just did not fix the underlying problem.
    """

    namespace: str
    name: str
    status: str
    reason: str | None = None
    rollout_outcome: str | None = None
    before_rollout_state: str | None = None
    after_rollout_state: str | None = None
    timeout_seconds: int | None = None
    elapsed_seconds: float | None = None
    approved_by: str | None = None
    timestamp: str | None = None
    message: str = ""


def load_ops_config(path: Path) -> dict[str, Any]:
    """Load a governed-operations config YAML file -- reuses `config.load_yaml_dict()`, like `cloudremediate.load_remediation_config()` does."""
    data = load_yaml_dict(path)
    data.setdefault("namespace_allowlist", [])
    data.setdefault("cooldown_seconds", 120)
    data.setdefault("rollout_timeout_seconds", 60)
    data.setdefault("rollout_poll_interval_seconds", 2)
    data.setdefault("audit_log_path", "k8s-ops-audit.jsonl")
    return data


def _restart_patch(now: datetime) -> dict[str, Any]:
    """The strategic-merge patch body `kubectl rollout restart` sends -- one annotation, nothing else touched."""
    return {
        "spec": {
            "template": {
                "metadata": {"annotations": {RESTART_ANNOTATION: now.isoformat()}}
            }
        }
    }


def append_audit_log(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON-lines record -- this file IS both the rollback context and the cooldown gate's memory."""
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def last_restart_at(
    audit_log_path: Path, *, namespace: str, name: str
) -> datetime | None:
    """Read the audit log for the most recent real restart of `(namespace, name)`, or `None` if there is none.

    Reads from disk every time rather than tracking state in memory, so the
    cooldown guard holds across separate CLI invocations -- a fresh process
    calling `restart-execute` twice, thirty seconds apart, is still caught.
    """
    if not audit_log_path.exists():
        return None

    latest: datetime | None = None
    with audit_log_path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if (
                record.get("namespace") == namespace
                and record.get("name") == name
                and record.get("status") == "restarted"
            ):
                seen = datetime.fromisoformat(record["timestamp"])
                if latest is None or seen > latest:
                    latest = seen
    return latest


def plan_hash(plan: RestartPlan, *, now: datetime) -> str:
    """A short, stable fingerprint of a plan at the moment it was built -- printed for an operator to cross-check, not used as a secret token."""
    raw = f"{plan.namespace}:{plan.name}:{plan.before_rollout_state}:{now.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def build_restart_plan(
    apps_client: Any,
    *,
    namespace: str,
    name: str,
    allowlist: list[str],
    now: datetime | None = None,
) -> RestartPlan:
    """Build a `RestartPlan` for one Deployment -- validates with a server-side dry run, never restarts for real.

    The namespace allowlist is checked first, before this function even
    tries to read the Deployment -- a namespace missing from `allowlist` is
    always `not_allowed`, regardless of whether the Deployment exists or
    needs restarting at all.
    """
    resolved_now = now if now is not None else datetime.now(UTC)

    if namespace not in allowlist:
        return RestartPlan(
            namespace=namespace,
            name=name,
            status="not_allowed",
            reason=(
                f"namespace '{namespace}' is not on the namespace allowlist -- "
                "restart is refused regardless of --approve"
            ),
        )

    try:
        status = get_deployment_status(apps_client, name=name, namespace=namespace)
    except ApiException as exc:
        return RestartPlan(
            namespace=namespace,
            name=name,
            status="not_found",
            reason=f"could not read Deployment '{namespace}/{name}': {exc.reason}",
        )

    before_state = rollout_state(status)
    patch = _restart_patch(resolved_now)
    action = RestartAction(
        api_call="patch_namespaced_deployment",
        args={"name": name, "namespace": namespace, "body": patch},
    )

    # Server-side dry run: the API server validates and returns what it
    # WOULD store, without persisting anything -- proof the patch is
    # well-formed and would be accepted, not a client-side guess.
    apps_client.patch_namespaced_deployment(
        name=name, namespace=namespace, body=patch, dry_run="All"
    )

    return RestartPlan(
        namespace=namespace,
        name=name,
        status="plannable",
        action=action,
        before_rollout_state=before_state,
        dry_run_validated=True,
    )


def monitor_rollout(
    apps_client: Any,
    *,
    namespace: str,
    name: str,
    timeout_seconds: int,
    poll_interval_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> tuple[str, str, float]:
    """Poll a Deployment until it is healthy AND has rolled every replica onto the new template, or until `timeout_seconds` runs out.

    Checking `rollout_state() == "healthy"` alone is not enough during a
    rollout -- old, already-ready Pods from the previous ReplicaSet can keep
    a Deployment looking "healthy" while the new Pods are still coming up.
    `updated_replicas == desired_replicas` is what proves the replicas that
    are ready are the NEW ones this restart actually created. Returns
    `(outcome, final_rollout_state, elapsed_seconds)` -- `outcome` is
    `"rolled_out"` or `"timed_out"`. `sleep_fn`/`clock_fn` are injected so
    tests never wait on a real clock.
    """
    start = clock_fn()
    final_state = "unavailable"
    while True:
        status = get_deployment_status(apps_client, name=name, namespace=namespace)
        final_state = rollout_state(status)
        elapsed = clock_fn() - start
        if final_state == "healthy" and status.updated_replicas == status.desired_replicas:
            return "rolled_out", final_state, elapsed
        if elapsed >= timeout_seconds:
            return "timed_out", final_state, elapsed
        sleep_fn(poll_interval_seconds)


def execute_restart(
    apps_client: Any,
    plan: RestartPlan,
    *,
    approve: bool,
    allowlist: list[str],
    cooldown_seconds: int,
    rollout_timeout_seconds: int,
    rollout_poll_interval_seconds: float = 2.0,
    audit_log_path: Path | None = None,
    actor: str = "cli-operator",
    now: datetime | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> RestartResult:
    """Act on a plan -- refuses without `approve=True`, refuses inside the cooldown window, re-checks before writing.

    `not_allowed`/`not_found` plans pass straight through -- there is
    nothing to approve either way. A `plannable` plan with `approve=False`
    raises `RestartNotApprovedError`. An approved plan is then checked
    against the cooldown: if the SAME `(namespace, name)` was restarted
    inside `cooldown_seconds`, this raises `RestartCoolingDownError` instead
    of ever calling the API -- the loop guard. Only after both gates pass
    does this rebuild the plan fresh (a new dry-run, against current live
    state) and, if it is still `plannable`, apply the real patch and
    monitor the rollout.
    """
    if plan.status in {"not_allowed", "not_found"}:
        return RestartResult(
            namespace=plan.namespace,
            name=plan.name,
            status=plan.status,
            reason=plan.reason,
            message=plan.reason or plan.status,
        )

    # plan.status == "plannable"
    if not approve:
        raise RestartNotApprovedError(plan)

    resolved_now = now if now is not None else datetime.now(UTC)
    resolved_audit_log = (
        audit_log_path if audit_log_path is not None else Path("k8s-ops-audit.jsonl")
    )

    last_restart = last_restart_at(
        resolved_audit_log, namespace=plan.namespace, name=plan.name
    )
    if last_restart is not None:
        elapsed_since = (resolved_now - last_restart).total_seconds()
        if elapsed_since < cooldown_seconds:
            raise RestartCoolingDownError(plan, cooldown_seconds - elapsed_since)

    fresh_plan = build_restart_plan(
        apps_client,
        namespace=plan.namespace,
        name=plan.name,
        allowlist=allowlist,
        now=resolved_now,
    )
    if fresh_plan.status != "plannable" or fresh_plan.action is None:
        # The allowlist changed, or the Deployment disappeared, between plan
        # and execute -- refuse, the same as a plan that was never
        # plannable in the first place.
        return RestartResult(
            namespace=plan.namespace,
            name=plan.name,
            status=fresh_plan.status,
            reason=fresh_plan.reason,
            message=fresh_plan.reason or fresh_plan.status,
        )

    action = fresh_plan.action
    before_state = fresh_plan.before_rollout_state

    # The real patch -- no dry_run this time. This is the only line in this
    # module that actually mutates the cluster.
    getattr(apps_client, action.api_call)(**action.args)

    outcome, after_state, elapsed = monitor_rollout(
        apps_client,
        namespace=plan.namespace,
        name=plan.name,
        timeout_seconds=rollout_timeout_seconds,
        poll_interval_seconds=rollout_poll_interval_seconds,
        sleep_fn=sleep_fn,
        clock_fn=clock_fn,
    )

    record: dict[str, Any] = {
        "timestamp": resolved_now.isoformat(),
        "namespace": plan.namespace,
        "name": plan.name,
        "status": "restarted",
        "rollout_outcome": outcome,
        "before_rollout_state": before_state,
        "after_rollout_state": after_state,
        "timeout_seconds": rollout_timeout_seconds,
        "elapsed_seconds": round(elapsed, 2),
        "approved_by": actor,
    }
    append_audit_log(resolved_audit_log, record)

    message = (
        "restart triggered -- rollout completed within timeout"
        if outcome == "rolled_out"
        else (
            "restart triggered -- rollout did NOT complete within timeout "
            "(the restart alone did not fix this; see rollback guidance)"
        )
    )

    return RestartResult(
        namespace=plan.namespace,
        name=plan.name,
        status="restarted",
        rollout_outcome=outcome,
        before_rollout_state=before_state,
        after_rollout_state=after_state,
        timeout_seconds=rollout_timeout_seconds,
        elapsed_seconds=round(elapsed, 2),
        approved_by=actor,
        timestamp=record["timestamp"],
        message=message,
    )


def plan_to_dict(plan: RestartPlan) -> dict[str, Any]:
    """`RestartPlan` as a plain, JSON-serializable dict -- what the CLI prints for `--json`."""
    return asdict(plan)


def result_to_dict(result: RestartResult) -> dict[str, Any]:
    """`RestartResult` as a plain, JSON-serializable dict -- what the CLI prints for `--json`."""
    return asdict(result)


__all__ = [
    "RESTART_ANNOTATION",
    "RestartAction",
    "RestartCoolingDownError",
    "RestartNotApprovedError",
    "RestartPlan",
    "RestartResult",
    "append_audit_log",
    "build_restart_plan",
    "execute_restart",
    "last_restart_at",
    "load_ops_config",
    "monitor_rollout",
    "plan_hash",
    "plan_to_dict",
    "result_to_dict",
]
