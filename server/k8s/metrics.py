"""SPEC-P4 §8.2 — Prometheus and OTel metric queries.

All Prometheus HTTP queries are isolated here. Uses httpx for async-capable
HTTP but called synchronously from the bridge (single-threaded episode state).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("bridge.k8s.metrics")

_QUERY_TIMEOUT = 10.0


@dataclass
class PrometheusMetrics:
    """Raw Prometheus query results for a single service."""

    # OTel HTTP fields
    http_server_error_rate: float | None = None
    http_server_request_duration_p99: float | None = None
    http_server_active_requests: float | None = None

    # Process fields (OTel or cAdvisor fallback)
    process_cpu_utilization: float | None = None
    process_memory_usage_bytes: float | None = None

    # JVM fields
    runtime_gc_pause_duration: float | None = None
    runtime_gc_count_per_second: float | None = None
    runtime_jvm_threads_count: float | None = None
    runtime_thread_pool_queue_depth: float | None = None


def _query_instant(
    client: httpx.Client,
    prometheus_url: str,
    query: str,
    *,
    time_param: str | None = None,
) -> float | None:
    """Execute an instant PromQL query and return the first numeric result."""
    params: dict[str, str] = {"query": query}
    if time_param:
        params["time"] = time_param

    try:
        resp = client.get(
            f"{prometheus_url}/api/v1/query",
            params=params,
            timeout=_QUERY_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Prometheus query failed (%d): %s", resp.status_code, query[:80])
            return None

        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            return None

        # Take the first result's value
        value_pair = results[0].get("value", [])
        if len(value_pair) >= 2:
            val = float(value_pair[1])
            # NaN / Inf guard
            if val != val or val == float("inf") or val == float("-inf"):
                return None
            return val
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        logger.warning("Prometheus query error for '%s': %s", query[:80], exc)
    return None


def _query_has_results(
    client: httpx.Client,
    prometheus_url: str,
    query: str,
) -> bool:
    """Check whether a PromQL query returns any results."""
    try:
        resp = client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query},
            timeout=_QUERY_TIMEOUT,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        return len(results) > 0
    except (httpx.HTTPError, ValueError):
        return False


def query_service_metrics(
    client: httpx.Client,
    prometheus_url: str,
    service: str,
    namespace: str = "otel-demo",
) -> PrometheusMetrics:
    """SPEC-P4 §8.2: execute all PromQL queries for a single service."""
    m = PrometheusMetrics()

    # --- OTel HTTP fields ---
    # Error rate: 5xx / total
    numerator = _query_instant(
        client, prometheus_url,
        f'sum by (service) (rate(http_server_request_duration_seconds_count'
        f'{{service_namespace="{namespace}", http_response_status_code=~"5..", '
        f'service="{service}"}}[2m]))',
    )
    denominator = _query_instant(
        client, prometheus_url,
        f'sum by (service) (rate(http_server_request_duration_seconds_count'
        f'{{service_namespace="{namespace}", service="{service}"}}[2m]))',
    )
    if numerator is not None and denominator is not None and denominator > 0:
        m.http_server_error_rate = numerator / denominator
    else:
        m.http_server_error_rate = 0.0

    # Latency P99
    m.http_server_request_duration_p99 = _query_instant(
        client, prometheus_url,
        f'histogram_quantile(0.99, sum by (service, le) (rate('
        f'http_server_request_duration_seconds_bucket'
        f'{{service_namespace="{namespace}", service="{service}"}}[2m])))',
    )

    # Active requests
    m.http_server_active_requests = _query_instant(
        client, prometheus_url,
        f'http_server_active_requests{{service_namespace="{namespace}", service="{service}"}}',
    )

    # --- Process fields (OTel preferred) ---
    m.process_cpu_utilization = _query_instant(
        client, prometheus_url,
        f'process_cpu_utilization{{service_namespace="{namespace}", service="{service}"}}',
    )
    m.process_memory_usage_bytes = _query_instant(
        client, prometheus_url,
        f'process_memory_usage_bytes{{service_namespace="{namespace}", service="{service}"}}',
    )

    # cAdvisor fallback (§8.2)
    if m.process_cpu_utilization is None:
        m.process_cpu_utilization = _query_instant(
            client, prometheus_url,
            f'sum by (pod) (rate(container_cpu_usage_seconds_total'
            f'{{namespace="{namespace}", pod=~"{service}-.*", '
            f'container!="", container!="POD"}}[2m])) / '
            f'sum by (pod) (container_spec_cpu_quota'
            f'{{namespace="{namespace}", pod=~"{service}-.*"}} / '
            f'container_spec_cpu_period'
            f'{{namespace="{namespace}", pod=~"{service}-.*"}})',
        )

    if m.process_memory_usage_bytes is None:
        m.process_memory_usage_bytes = _query_instant(
            client, prometheus_url,
            f'sum by (pod) (container_memory_working_set_bytes'
            f'{{namespace="{namespace}", pod=~"{service}-.*", '
            f'container!="", container!="POD"}})',
        )

    # --- JVM fields (ad, fraud-detection only; zero/null for others) ---
    m.runtime_gc_pause_duration = _query_instant(
        client, prometheus_url,
        f'histogram_quantile(0.99, sum by (service, le) (rate('
        f'jvm_gc_duration_seconds_bucket{{service_namespace="{namespace}", '
        f'service="{service}"}}[2m])))',
    )
    m.runtime_gc_count_per_second = _query_instant(
        client, prometheus_url,
        f'rate(jvm_gc_duration_seconds_count{{service_namespace="{namespace}", '
        f'service="{service}"}}[2m])',
    )
    m.runtime_jvm_threads_count = _query_instant(
        client, prometheus_url,
        f'jvm_thread_count{{service_namespace="{namespace}", service="{service}"}}',
    )
    m.runtime_thread_pool_queue_depth = _query_instant(
        client, prometheus_url,
        f'executor_queued{{service_namespace="{namespace}", service="{service}"}}',
    )

    return m


def query_trend_value(
    client: httpx.Client,
    prometheus_url: str,
    query: str,
    time_param: str,
) -> float | None:
    """Query a PromQL at a specific past timestamp for trend computation."""
    return _query_instant(client, prometheus_url, query, time_param=time_param)


def verify_prometheus_up(client: httpx.Client, prometheus_url: str) -> bool:
    """SPEC-P4 §4 Step 7: verify Prometheus connectivity."""
    try:
        resp = client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": "up"},
            timeout=_QUERY_TIMEOUT,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def verify_otel_metrics_present(
    client: httpx.Client,
    prometheus_url: str,
    namespace: str,
    services: list[str],
) -> int:
    """SPEC-P4 §4 Step 8: count how many services have OTel metrics."""
    query = (
        f'http_server_request_duration_seconds_count{{service_namespace="{namespace}"}}'
    )
    try:
        resp = client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query},
            timeout=_QUERY_TIMEOUT,
        )
        if resp.status_code != 200:
            return 0

        data = resp.json()
        results = data.get("data", {}).get("result", [])
        found_services: set[str] = set()
        for r in results:
            svc = r.get("metric", {}).get("service", "")
            if svc in services:
                found_services.add(svc)
        return len(found_services)
    except (httpx.HTTPError, ValueError):
        return 0


def poll_promql(
    client: httpx.Client,
    prometheus_url: str,
    expression: str,
) -> bool:
    """Check if a PromQL expression returns any results (for resolution polling)."""
    return _query_has_results(client, prometheus_url, expression)
