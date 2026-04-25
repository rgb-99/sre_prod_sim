"""SPEC-P4 §8.1 — Kubernetes API read operations.

All K8s reads are isolated here so the bridge server never calls the K8s API
directly. Every function accepts pre-loaded API clients for testability.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from kubernetes.client import AppsV1Api, CoreV1Api
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger("bridge.k8s.queries")


@dataclass
class K8sServiceData:
    """Raw K8s API data for a single service (SPEC-P4 §8.1)."""

    restart_count: int = 0
    last_deployment_age_seconds: float = 0.0
    runtime_uptime_seconds: float = 0.0
    process_memory_limit_bytes: float = 0.0
    process_open_file_descriptors: int = 0
    replica_count: int = 1
    available_replicas: int = 0
    deployment_revision: str = "1"


@dataclass
class FaultMetadata:
    """Contents of the firewatch-active-fault ConfigMap (SPEC-P4 §7.2)."""

    archetype: str | None = None
    target_service: str | None = None
    injection_mechanism: str | None = None
    injected_at: str | None = None


def _parse_memory_string(mem: str) -> float:
    """Convert K8s memory string (e.g. '300Mi') to bytes."""
    mem = mem.strip()
    units = {
        "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
        "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
        if mem.endswith(suffix):
            return float(mem[: -len(suffix)]) * multiplier
    try:
        return float(mem)
    except ValueError:
        return 0.0


def get_service_k8s_data(
    apps_api: AppsV1Api,
    core_api: CoreV1Api,
    service: str,
    namespace: str,
) -> K8sServiceData:
    """Collect K8s API fields for a single service (SPEC-P4 §8.1)."""
    data = K8sServiceData()
    now = time.time()

    # --- Deployment data ---
    try:
        deps = apps_api.list_namespaced_deployment(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/name={service}",
        )
        if deps.items:
            dep = deps.items[0]
            data.replica_count = dep.spec.replicas or 1
            data.available_replicas = dep.status.available_replicas or 0
            data.deployment_revision = (
                dep.metadata.annotations or {}
            ).get("deployment.kubernetes.io/revision", "1")

            # Deployment age from creation timestamp
            if dep.metadata.creation_timestamp:
                created = dep.metadata.creation_timestamp.timestamp()
                data.last_deployment_age_seconds = now - created

            # Container resource limits
            containers = dep.spec.template.spec.containers or []
            if containers:
                resources = containers[0].resources
                if resources and resources.limits and "memory" in resources.limits:
                    data.process_memory_limit_bytes = _parse_memory_string(
                        resources.limits["memory"]
                    )
    except ApiException as exc:
        logger.warning("Failed to read deployment for %s: %s", service, exc.reason)

    # --- Pod data ---
    try:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/name={service}",
        )
        total_restarts = 0
        oldest_creation = now

        for pod in pods.items:
            # Restart count from all container statuses
            if pod.status and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    total_restarts += cs.restart_count or 0

            # Track oldest pod for uptime
            if pod.metadata.creation_timestamp:
                ts = pod.metadata.creation_timestamp.timestamp()
                oldest_creation = min(oldest_creation, ts)

        data.restart_count = total_restarts
        data.runtime_uptime_seconds = now - oldest_creation if pods.items else 0.0
    except ApiException as exc:
        logger.warning("Failed to list pods for %s: %s", service, exc.reason)

    return data


def verify_deployments_exist(
    apps_api: AppsV1Api,
    namespace: str,
    required_services: list[str],
) -> list[str]:
    """SPEC-P4 §4 Step 4: verify all 15 deployments exist. Returns missing names."""
    try:
        deps = apps_api.list_namespaced_deployment(namespace=namespace)
    except ApiException as exc:
        raise SystemExit(f"Cannot list deployments: {exc.reason}") from exc

    # OTel Demo Helm chart uses app.kubernetes.io/name label
    found_names: set[str] = set()
    for dep in deps.items:
        labels = dep.metadata.labels or {}
        name = labels.get("app.kubernetes.io/name", "")
        if name:
            found_names.add(name)

    missing = [s for s in required_services if s not in found_names]
    return missing


def read_fault_metadata(
    core_api: CoreV1Api,
    namespace: str,
) -> FaultMetadata:
    """SPEC-P4 §7.2: read firewatch-active-fault ConfigMap."""
    try:
        cm = core_api.read_namespaced_config_map("firewatch-active-fault", namespace)
        data = cm.data or {}
        return FaultMetadata(
            archetype=data.get("archetype"),
            target_service=data.get("target_service"),
            injection_mechanism=data.get("injection_mechanism"),
            injected_at=data.get("injected_at"),
        )
    except ApiException as exc:
        if exc.status == 404:
            logger.info("firewatch-active-fault ConfigMap not found — no active fault")
            return FaultMetadata()
        raise


def snapshot_flagd_configmap(
    core_api: CoreV1Api,
    namespace: str,
) -> dict | None:
    """SPEC-P4 §4 Step 5: snapshot the flagd ConfigMap flags."""
    try:
        cms = core_api.list_namespaced_config_map(
            namespace=namespace,
            label_selector="app.kubernetes.io/component=flagd",
        )
        if cms.items:
            cm = cms.items[0]
            return dict(cm.data or {})
    except ApiException as exc:
        logger.warning("Failed to snapshot flagd ConfigMap: %s", exc.reason)
    return None


def snapshot_service_configmaps(
    core_api: CoreV1Api,
    namespace: str,
    services: list[str],
) -> dict[str, dict[str, dict]]:
    """SPEC-P4 §4 Step 6: deep-copy each service's ConfigMap data.

    Returns: {service_name: {cm_name: {data_dict}}} or {service_name: {}} if none.
    """
    result: dict[str, dict[str, dict]] = {}
    for svc in services:
        result[svc] = {}
        try:
            cms = core_api.list_namespaced_config_map(
                namespace=namespace,
                label_selector=f"app.kubernetes.io/name={svc}",
            )
            for cm in cms.items:
                if cm.data:
                    result[svc][cm.metadata.name] = dict(cm.data)
        except ApiException:
            pass
    return result


def get_pod_logs(
    core_api: CoreV1Api,
    namespace: str,
    service: str,
    tail_lines: int = 100,
) -> list[str]:
    """SPEC-P4 §10.1: fetch logs from first Running pod matching the service."""
    try:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app.kubernetes.io/name={service}",
        )
    except ApiException:
        return []

    # Prefer a Running pod; fall back to any pod
    target_pod = None
    for pod in pods.items:
        if pod.status and pod.status.phase == "Running":
            target_pod = pod.metadata.name
            break
    if not target_pod and pods.items:
        target_pod = pods.items[0].metadata.name

    if not target_pod:
        return []

    try:
        log_text = core_api.read_namespaced_pod_log(
            name=target_pod,
            namespace=namespace,
            tail_lines=tail_lines,
            timestamps=True,
        )
        return log_text.strip().splitlines() if log_text else []
    except ApiException:
        return []
