"""SPEC-P4 §8 — Pydantic models for the observation schema.

Mirrors the training env's SystemObservation shape so the agent sees a
structurally identical interface regardless of backend.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LogEntry(BaseModel):
    """Single re-templated log line (SPEC-P4 §10.1)."""

    timestamp: str
    level: str = "INFO"
    message: str = ""
    error_code: int | None = None


class MetricTrends(BaseModel):
    """3-tick trend summary for get_metrics_detail (SPEC-P4 §10.2)."""

    cpu_trend: str = "stable"
    memory_trend: str = "stable"
    error_rate_trend: str = "stable"


class DependencyTrace(BaseModel):
    """Dependency trace result for trace_dependencies (SPEC-P4 §10.3)."""

    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)


class ServiceMetrics(BaseModel):
    """Per-service metrics combining K8s API and Prometheus data (SPEC-P4 §8.3)."""

    name: str
    status: str = "healthy"

    # OTel / HTTP fields
    http_server_error_rate: float = 0.0
    http_server_request_duration_p99: float = 0.0
    http_server_active_requests: float = 0.0

    # Process fields
    process_cpu_utilization: float = 0.0
    process_memory_usage_bytes: float = 0.0
    process_memory_limit_bytes: float = 0.0
    process_memory_utilization: float = 0.0
    process_open_file_descriptors: int = 0

    # K8s infrastructure fields
    restart_count: int = 0
    last_deployment_age_seconds: float = 0.0
    runtime_uptime_seconds: float = 0.0

    # JVM fields (zero for non-JVM services)
    runtime_gc_pause_duration: float = 0.0
    runtime_gc_count_per_second: float = 0.0
    runtime_jvm_threads_count: int = 0
    runtime_jvm_threads_max: int = 0
    runtime_thread_pool_queue_depth: int = 0

    # Populated by fetch_logs and get_metrics_detail actions
    recent_logs: list[LogEntry] = Field(default_factory=list)
    metric_trends: MetricTrends | None = None
    dependency_trace: DependencyTrace | None = None


class Alert(BaseModel):
    """Flattened training-schema alert (SPEC-P4 §8.4)."""

    alertname: str
    severity: str
    service: str
    metric_value: float = 0.0
    threshold_value: float = 0.0
    fired_at_tick: int = 0
    chaos_type: str | None = None
    fault_archetype: str | None = None


class ActionHistoryEntry(BaseModel):
    """Single entry in the action history deque (SPEC-P4 §9.5)."""

    action: str
    target: str | None = None
    tick: int = 0
    feedback: str = ""


class SystemObservation(BaseModel):
    """Full observation returned by /reset and /step (SPEC-P4 §7.4).

    Structurally identical to the training env's SystemObservation.
    """

    services: dict[str, ServiceMetrics] = Field(default_factory=dict)
    dependency_graph: dict[str, list[str]] = Field(default_factory=dict)
    active_alerts: list[Alert] = Field(default_factory=list)
    action_history: list[ActionHistoryEntry] = Field(default_factory=list)

    # Fiction / computed fields
    sim_tick: int = 0
    slo_budget_remaining_pct: float = 100.0
    bad_customer_minutes: float = 0.0
    mttm_achieved_tick: int | None = None
