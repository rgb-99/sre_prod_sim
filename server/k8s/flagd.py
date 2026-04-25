"""SPEC-P4 §10.6 Path 1 — flagd ConfigMap reset for revert_config action.

When a fault was injected via flagd, this module restores the snapshotted
flag configuration taken at bridge startup (Step 5).
"""

from __future__ import annotations

import copy
import json
import logging

from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger("bridge.k8s.flagd")


def restore_flagd_snapshot(
    core_api: CoreV1Api,
    namespace: str,
    snapshot: dict[str, str],
) -> str:
    """Reset flagd ConfigMap data to the startup snapshot.

    Args:
        core_api: K8s core API client.
        namespace: Target namespace.
        snapshot: The deep-copy of the ConfigMap's ``data`` field taken at
                  bridge startup (SPEC-P4 §4 Step 5).

    Returns:
        Feedback string for the action history.
    """
    try:
        cms = core_api.list_namespaced_config_map(
            namespace=namespace,
            label_selector="app.kubernetes.io/component=flagd",
        )
    except ApiException as exc:
        raise ApiException(
            status=exc.status,
            reason=f"Cannot list flagd ConfigMaps: {exc.reason}",
        ) from exc

    if not cms.items:
        raise ApiException(status=404, reason="flagd ConfigMap not found")

    cm = cms.items[0]
    cm.data = copy.deepcopy(snapshot)

    try:
        core_api.replace_namespaced_config_map(cm.metadata.name, namespace, cm)
    except ApiException as exc:
        raise ApiException(
            status=exc.status,
            reason=f"Failed to restore flagd ConfigMap: {exc.reason}",
        ) from exc

    logger.info("Restored flagd ConfigMap to startup snapshot")
    return "reverted flagd flags to startup snapshot"
