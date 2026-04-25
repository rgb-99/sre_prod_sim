"""SPEC-P4 §10 — Kubernetes write operations for agent actions.

Every cluster-modifying action is isolated here. The bridge server calls
these functions; they never call the K8s API anywhere else.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

from kubernetes.client import (
    AppsV1Api,
    CoreV1Api,
    NetworkingV1Api,
    V1NetworkPolicy,
    V1NetworkPolicySpec,
    V1LabelSelector,
    V1ObjectMeta,
)
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger("bridge.k8s.actions")


def restart_service(
    apps_api: AppsV1Api,
    service: str,
    namespace: str,
) -> str:
    """SPEC-P4 §10.4: rolling restart via restartedAt annotation patch."""
    now = datetime.now(timezone.utc).isoformat()

    # Find the deployment by label
    deps = apps_api.list_namespaced_deployment(
        namespace=namespace,
        label_selector=f"app.kubernetes.io/name={service}",
    )
    if not deps.items:
        raise ApiException(status=404, reason=f"Deployment not found for {service}")

    dep_name = deps.items[0].metadata.name

    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now,
                    }
                }
            }
        }
    }
    apps_api.patch_namespaced_deployment(dep_name, namespace, patch)
    return f"restarted {service}"


def rollback_deploy(
    apps_api: AppsV1Api,
    service: str,
    namespace: str,
) -> str:
    """SPEC-P4 §10.5: rollback to previous ReplicaSet revision."""
    deps = apps_api.list_namespaced_deployment(
        namespace=namespace,
        label_selector=f"app.kubernetes.io/name={service}",
    )
    if not deps.items:
        raise ApiException(status=404, reason=f"Deployment not found for {service}")

    dep = deps.items[0]
    dep_name = dep.metadata.name
    current_rev_str = (dep.metadata.annotations or {}).get(
        "deployment.kubernetes.io/revision", "1"
    )
    current_rev = int(current_rev_str)

    if current_rev <= 1:
        return "__NO_PREVIOUS_REVISION__"

    prev_rev = current_rev - 1

    # Find the ReplicaSet with the previous revision
    rsets = apps_api.list_namespaced_replica_set(
        namespace=namespace,
        label_selector=f"app.kubernetes.io/name={service}",
    )
    target_rs = None
    for rs in rsets.items:
        rs_rev = (rs.metadata.annotations or {}).get(
            "deployment.kubernetes.io/revision", ""
        )
        if rs_rev == str(prev_rev):
            target_rs = rs
            break

    if not target_rs:
        return "__NO_PREVIOUS_REVISION__"

    # Patch the deployment's pod template to match the previous RS
    patch = {
        "spec": {
            "template": target_rs.spec.template.to_dict(),
        }
    }
    apps_api.patch_namespaced_deployment(dep_name, namespace, patch)
    return f"rolled back {service} to revision {prev_rev}"


def scale_replicas(
    apps_api: AppsV1Api,
    service: str,
    namespace: str,
    replicas: int,
) -> str:
    """SPEC-P4 §10.7: scale deployment replicas."""
    deps = apps_api.list_namespaced_deployment(
        namespace=namespace,
        label_selector=f"app.kubernetes.io/name={service}",
    )
    if not deps.items:
        raise ApiException(status=404, reason=f"Deployment not found for {service}")

    dep_name = deps.items[0].metadata.name

    patch = {"spec": {"replicas": replicas}}
    apps_api.patch_namespaced_deployment_scale(dep_name, namespace, patch)
    return f"scaled {service} to {replicas} replicas"


def circuit_break(
    networking_api: NetworkingV1Api,
    service: str,
    namespace: str,
) -> str:
    """SPEC-P4 §10.8: create NetworkPolicy to deny all ingress."""
    policy_name = f"firewatch-circuit-break-{service}"

    policy = V1NetworkPolicy(
        metadata=V1ObjectMeta(
            name=policy_name,
            namespace=namespace,
            labels={
                "managed-by": "firewatch",
                "circuit-break": "true",
            },
        ),
        spec=V1NetworkPolicySpec(
            pod_selector=V1LabelSelector(
                match_labels={"app.kubernetes.io/name": service},
            ),
            ingress=[],
            policy_types=["Ingress"],
        ),
    )

    try:
        networking_api.create_namespaced_network_policy(namespace, policy)
    except ApiException as exc:
        if exc.status == 409:
            logger.info("NetworkPolicy %s already exists", policy_name)
        else:
            raise

    return f"circuit-broke {service}"


def delete_network_policies(
    networking_api: NetworkingV1Api,
    namespace: str,
    policy_names: list[str],
) -> list[str]:
    """SPEC-P4 §10.9 Step 1: cleanup circuit-break policies. Returns failures."""
    failures: list[str] = []
    for name in policy_names:
        try:
            networking_api.delete_namespaced_network_policy(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to delete NetworkPolicy %s: %s", name, exc.reason)
                failures.append(name)
    return failures


def revert_configmap(
    core_api: CoreV1Api,
    namespace: str,
    cm_name: str,
    snapshot_data: dict,
) -> str:
    """SPEC-P4 §10.6 Path 2: restore a ConfigMap to its snapshotted data."""
    try:
        cm = core_api.read_namespaced_config_map(cm_name, namespace)
        cm.data = copy.deepcopy(snapshot_data)
        core_api.replace_namespaced_config_map(cm_name, namespace, cm)
        return f"reverted ConfigMap {cm_name}"
    except ApiException as exc:
        raise ApiException(
            status=exc.status,
            reason=f"Failed to revert {cm_name}: {exc.reason}",
        ) from exc
