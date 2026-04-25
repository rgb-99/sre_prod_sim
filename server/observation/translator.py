"""SPEC-P4 §7.3 / §8.3 — Observation translator.

Merges K8s API data and Prometheus metrics into a full SystemObservation.
This is the interpretation layer that makes real cluster state look identical
to the training env's observation shape.
"""

from __future__ import annotations

import logging
import re
import time

import httpx
from kubernetes.client import AppsV1Api, CoreV1Api

from server.config import BridgeConfig
from server.k8s.queries import K8sServiceData, get_service_k8s_data
from server.k8s.metrics import PrometheusMetrics, query_service_metrics, query_trend_value
from server.observation.schema import (
    LogEntry,
    MetricTrends,
    ServiceMetrics,
    SystemObservation,
)

logger = logging.getLogger("bridge.observation.translator")


def _derive_status(error_rate: float, latency_p99: float, memory_util: float) -> str:
    """SPEC-P4 §8.3: status derivation using training env thresholds."""
    if error_rate >= 0.90 or memory_util >= 0.98:
        return "down"
    if error_rate >= 0.50 or latency_p99 >= 2.0:
        return "critical"
    if error_rate >= 0.10 or latency_p99 >= 0.50:
        return "degraded"
    return "healthy"


def merge_service_data(
    service: str,
    k8s: K8sServiceData,
    prom: PrometheusMetrics,
) -> ServiceMetrics:
    """SPEC-P4 §8.3: merge K8s API and Prometheus data into ServiceMetrics."""
    # Process fields — OTel takes precedence (§8.3 rule 1)
    cpu = prom.process_cpu_utilization if prom.process_cpu_utilization is not None else 0.0
    mem_usage = prom.process_memory_usage_bytes if prom.process_memory_usage_bytes is not None else 0.0
    mem_limit = k8s.process_memory_limit_bytes if k8s.process_memory_limit_bytes > 0 else 1.0
    mem_util = mem_usage / mem_limit if mem_limit > 0 else 0.0

    error_rate = prom.http_server_error_rate if prom.http_server_error_rate is not None else 0.0
    latency_p99 = prom.http_server_request_duration_p99 if prom.http_server_request_duration_p99 is not None else 0.0

    status = _derive_status(error_rate, latency_p99, mem_util)

    return ServiceMetrics(
        name=service,
        status=status,
        http_server_error_rate=error_rate,
        http_server_request_duration_p99=latency_p99,
        http_server_active_requests=(
            prom.http_server_active_requests
            if prom.http_server_active_requests is not None
            else 0.0
        ),
        process_cpu_utilization=cpu,
        process_memory_usage_bytes=mem_usage,
        process_memory_limit_bytes=k8s.process_memory_limit_bytes,
        process_memory_utilization=mem_util,
        process_open_file_descriptors=k8s.process_open_file_descriptors,
        restart_count=k8s.restart_count,
        last_deployment_age_seconds=k8s.last_deployment_age_seconds,
        runtime_uptime_seconds=k8s.runtime_uptime_seconds,
        # JVM fields — zero for non-JVM (§8.3 rule 3)
        runtime_gc_pause_duration=(
            prom.runtime_gc_pause_duration
            if prom.runtime_gc_pause_duration is not None
            else 0.0
        ),
        runtime_gc_count_per_second=(
            prom.runtime_gc_count_per_second
            if prom.runtime_gc_count_per_second is not None
            else 0.0
        ),
        runtime_jvm_threads_count=(
            int(prom.runtime_jvm_threads_count)
            if prom.runtime_jvm_threads_count is not None
            else 0
        ),
        runtime_jvm_threads_max=0,
        runtime_thread_pool_queue_depth=(
            int(prom.runtime_thread_pool_queue_depth)
            if prom.runtime_thread_pool_queue_depth is not None
            else 0
        ),
    )


def build_all_service_metrics(
    apps_api: AppsV1Api,
    core_api: CoreV1Api,
    http_client: httpx.Client,
    config: BridgeConfig,
) -> dict[str, ServiceMetrics]:
    """SPEC-P4 §7.3 Step 1-3: collect and merge data for all 15 services."""
    result: dict[str, ServiceMetrics] = {}
    all_services = config.application_services + config.infrastructure_services

    for service in all_services:
        k8s_data = get_service_k8s_data(
            apps_api, core_api, service, config.otel_demo_namespace,
        )
        prom_data = query_service_metrics(
            http_client, config.prometheus_url, service, config.otel_demo_namespace,
        )
        result[service] = merge_service_data(service, k8s_data, prom_data)

    return result


def build_single_service_metrics(
    apps_api: AppsV1Api,
    core_api: CoreV1Api,
    http_client: httpx.Client,
    config: BridgeConfig,
    service: str,
) -> ServiceMetrics:
    """Re-query a single service (used by get_metrics_detail)."""
    k8s_data = get_service_k8s_data(
        apps_api, core_api, service, config.otel_demo_namespace,
    )
    prom_data = query_service_metrics(
        http_client, config.prometheus_url, service, config.otel_demo_namespace,
    )
    return merge_service_data(service, k8s_data, prom_data)


def compute_trend(current: float | None, past: float | None) -> str:
    """Compute trend label from current vs past value."""
    if current is None or past is None:
        return "stable"
    diff = (current or 0.0) - (past or 0.0)
    if abs(diff) < 0.01:
        return "stable"
    return "rising" if diff > 0 else "falling"


def build_metric_trends(
    http_client: httpx.Client,
    config: BridgeConfig,
    service: str,
    current_metrics: ServiceMetrics,
    sim_tick_seconds: int,
) -> MetricTrends:
    """SPEC-P4 §10.2: 3-tick trend summary by comparing current vs 3 ticks ago."""
    past_time = str(int(time.time() - 3 * sim_tick_seconds))
    ns = config.otel_demo_namespace

    past_cpu = query_trend_value(
        http_client, config.prometheus_url,
        f'process_cpu_utilization{{service_namespace="{ns}", service="{service}"}}',
        past_time,
    )
    past_mem = query_trend_value(
        http_client, config.prometheus_url,
        f'process_memory_usage_bytes{{service_namespace="{ns}", service="{service}"}}',
        past_time,
    )
    past_err_num = query_trend_value(
        http_client, config.prometheus_url,
        f'sum by (service) (rate(http_server_request_duration_seconds_count'
        f'{{service_namespace="{ns}", http_response_status_code=~"5..", '
        f'service="{service}"}}[2m]))',
        past_time,
    )
    past_err_den = query_trend_value(
        http_client, config.prometheus_url,
        f'sum by (service) (rate(http_server_request_duration_seconds_count'
        f'{{service_namespace="{ns}", service="{service}"}}[2m]))',
        past_time,
    )
    past_error_rate = (
        (past_err_num / past_err_den)
        if past_err_num is not None and past_err_den and past_err_den > 0
        else None
    )

    return MetricTrends(
        cpu_trend=compute_trend(current_metrics.process_cpu_utilization, past_cpu),
        memory_trend=compute_trend(current_metrics.process_memory_usage_bytes, past_mem),
        error_rate_trend=compute_trend(current_metrics.http_server_error_rate, past_error_rate),
    )


def parse_log_lines(raw_lines: list[str]) -> list[LogEntry]:
    """SPEC-P4 §10.1: re-template raw kubectl log lines into training format."""
    entries: list[LogEntry] = []

    # Regex for kubectl timestamp prefix
    ts_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+(.*)$")
    error_patterns = re.compile(r"(?i)(error|fatal|exception|panic|traceback)")
    warn_patterns = re.compile(r"(?i)(warn|deprecated)")
    error_code_pattern = re.compile(r"(?:exit_code=|status\s+|code:\s*)(\d+)")

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue

        timestamp = ""
        message = line

        m = ts_pattern.match(line)
        if m:
            timestamp = m.group(1)
            message = m.group(2)

        # Infer level
        if error_patterns.search(message):
            level = "ERROR"
        elif warn_patterns.search(message):
            level = "WARN"
        else:
            level = "INFO"

        # Extract error code
        code_match = error_code_pattern.search(message)
        error_code = int(code_match.group(1)) if code_match else None

        entries.append(LogEntry(
            timestamp=timestamp,
            level=level,
            message=message,
            error_code=error_code,
        ))

    return entries
