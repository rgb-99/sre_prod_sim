"""SPEC-P4 §8.4 / §8.5 / §11 — AlertManager fetch, flattening, and PromQL mapping.

Fetches active alerts from AlertManager, flattens them into the training
env's Alert schema, and provides the constant PromQL mapping for resolution
verification during declare_resolved.
"""

from __future__ import annotations

import logging

import httpx

from server.observation.schema import Alert

logger = logging.getLogger("bridge.k8s.alerts")

# SPEC-P4 §11 — Alert name to PromQL mapping.
# {service} is substituted at runtime.
ALERT_PROMQL_MAP: dict[str, str] = {
    "ServiceOOMKilled": (
        'increase(kube_pod_container_status_last_terminated_reason'
        '{reason="OOMKilled", namespace="otel-demo", pod=~"{service}-.*"}[3m]) > 0'
    ),
    "ServiceMemoryCritical": (
        'container_memory_working_set_bytes{namespace="otel-demo", pod=~"{service}-.*"}'
        ' / container_spec_memory_limit_bytes{namespace="otel-demo", pod=~"{service}-.*"} > 0.85'
    ),
    "HighLatency": (
        'histogram_quantile(0.99, sum by (service, le) (rate('
        'http_server_request_duration_seconds_bucket'
        '{service_namespace="otel-demo", service="{service}"}[2m]))) > 2.0'
    ),
    "HighErrorRate": (
        'sum by (service) (rate(http_server_request_duration_seconds_count'
        '{service_namespace="otel-demo", service="{service}", '
        'http_response_status_code=~"5.."}[2m])) / '
        'sum by (service) (rate(http_server_request_duration_seconds_count'
        '{service_namespace="otel-demo", service="{service}"}[2m])) > 0.10'
    ),
    "PodCrashLooping": (
        'rate(kube_pod_container_status_restarts_total'
        '{namespace="otel-demo", pod=~"{service}-.*"}[5m]) * 60 > 1'
    ),
    "NetworkPartition": (
        'rate(container_network_receive_errors_total'
        '{namespace="otel-demo", pod=~"{service}-.*"}[2m]) + '
        'rate(container_network_transmit_errors_total'
        '{namespace="otel-demo", pod=~"{service}-.*"}[2m]) > 1'
    ),
    "GCPressure": (
        'rate(jvm_gc_duration_seconds_sum'
        '{service_namespace="otel-demo", service="{service}"}[2m]) / '
        'rate(jvm_gc_duration_seconds_count'
        '{service_namespace="otel-demo", service="{service}"}[2m]) > 0.05'
    ),
}

# Hardcoded threshold map per alert (SPEC-P4 §8.4).
ALERT_THRESHOLD_MAP: dict[str, float] = {
    "ServiceOOMKilled": 0.0,
    "ServiceMemoryCritical": 0.85,
    "HighLatency": 2.0,
    "HighErrorRate": 0.10,
    "PodCrashLooping": 1.0,
    "NetworkPartition": 1.0,
    "GCPressure": 0.05,
}

_SEVERITY_ORDER = {"page": 3, "critical": 2, "warning": 1}


def get_firing_promql(alert_name: str, service: str) -> str:
    """Get the PromQL expression for resolution polling, substituting service."""
    template = ALERT_PROMQL_MAP.get(alert_name)
    if template:
        return template.replace("{service}", service)
    # Generic fallback
    return f'up{{namespace="otel-demo"}} == 1'


def fetch_active_alerts(
    client: httpx.Client,
    alertmanager_url: str,
    namespace: str,
    episode_start_time: float,
    sim_tick_seconds: int,
    fault_archetype: str | None = None,
) -> list[Alert]:
    """SPEC-P4 §8.5: fetch and flatten active alerts from AlertManager."""
    try:
        resp = client.get(
            f"{alertmanager_url}/api/v2/alerts",
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("AlertManager returned %d", resp.status_code)
            return []
        raw_alerts = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("AlertManager fetch failed: %s", exc)
        return []

    alerts: list[Alert] = []

    for raw in raw_alerts:
        status = raw.get("status", {})
        if isinstance(status, dict) and status.get("state") != "active":
            continue
        elif isinstance(status, str) and status != "active":
            continue

        labels = raw.get("labels", {})
        ns_label = labels.get("namespace", "")
        if ns_label and ns_label != namespace:
            continue

        alertname = labels.get("alertname", "unknown")
        severity = labels.get("severity", "warning")
        if severity not in ("warning", "critical", "page"):
            severity = "warning"
        service = labels.get("service", "unknown")

        # Compute fired_at_tick
        starts_at = raw.get("startsAt", "")
        fired_at_tick = 0
        if starts_at and episode_start_time > 0:
            try:
                from datetime import datetime, timezone

                if starts_at.endswith("Z"):
                    starts_at = starts_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(starts_at)
                starts_unix = dt.timestamp()
                raw_tick = int((starts_unix - episode_start_time) / sim_tick_seconds)
                fired_at_tick = max(0, raw_tick)
            except (ValueError, OSError):
                pass

        threshold = ALERT_THRESHOLD_MAP.get(alertname, 0.0)

        alert = Alert(
            alertname=alertname,
            severity=severity,
            service=service,
            metric_value=0.0,
            threshold_value=threshold,
            fired_at_tick=fired_at_tick,
            chaos_type=fault_archetype,
            fault_archetype=labels.get("fault_archetype"),
        )
        alerts.append(alert)

    # Sort by severity descending
    alerts.sort(key=lambda a: _SEVERITY_ORDER.get(a.severity, 0), reverse=True)
    return alerts
