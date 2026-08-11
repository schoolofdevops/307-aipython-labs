"""platformops.k8sclient -- one function that builds the Kubernetes API clients this project uses.

`awsclient.get_aws_client()` is the one place this project resolves an AWS
endpoint from a profile, a region and an optional override. This module is
the same idea for Kubernetes: `get_kubernetes_clients()` is the one place
this project decides how to authenticate to a cluster, so "which kubeconfig
context is `kubernetes_inspect` actually talking to?" is answered by
reading one small function, not by tracing global `kubernetes.config` state
through the process.

Two auth paths exist because this toolkit runs in two different places.
`in_cluster=False` (the default) reads a kubeconfig file -- exactly how you
run `platformops` from your own laptop against a `kind` cluster, the way
this module's lab does. `in_cluster=True` reads the token and CA certificate
Kubernetes itself mounts into every pod's filesystem
(`/var/run/secrets/kubernetes.io/serviceaccount/`) -- the path a `platformops`
container running as a scheduled Job *inside* a cluster would use, with no
kubeconfig file anywhere to find. Nothing else in this project needs to know
which path was used; both return the same two client types.
"""

from __future__ import annotations

from kubernetes import client, config


def get_kubernetes_clients(
    *,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    in_cluster: bool = False,
) -> tuple[client.AppsV1Api, client.CoreV1Api]:
    """Authenticate to a cluster and return the two API clients this project's inspectors take.

    `kubeconfig_path=None` (the default) uses the same file `kubectl` itself
    would use -- `$KUBECONFIG` if set, otherwise `~/.kube/config` -- exactly
    the resolution a learner's own `kubectl get pods` already relies on.
    `context=None` uses that kubeconfig's current context, never a
    hardcoded one, so this function never silently points at the wrong
    cluster on a laptop with more than one.
    """
    if in_cluster:
        config.load_incluster_config()
    else:
        config.load_kube_config(config_file=kubeconfig_path, context=context)
    return client.AppsV1Api(), client.CoreV1Api()


__all__ = ["get_kubernetes_clients"]
