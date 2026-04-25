"""SPEC-P4 v2 — LiveK8sEnv Bridge Server.

FastAPI application on port 8002 that bridges the real OTel Demo cluster
and the RL agent. The agent sees structurally identical observations to
its training environment.

Usage:
    uv run start-bridge
    uv run start-bridge -- --config /path/to/config.yaml
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from collections import deque
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from kubernetes import config as k8s_config
from kubernetes.client import AppsV1Api, CoreV1Api, NetworkingV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel

from k8s.dependency_graph import APPLICATION_SERVICES, DEPENDENCY_GRAPH
from server.config import BridgeConfig, load_config
from server.k8s.alerts import fetch_active_alerts, get_firing_promql
from server.k8s.actions import (
    circuit_break,
    delete_network_policies,
    restart_service,
    revert_configmap,
    rollback_deploy,
    scale_replicas,
)
from server.k8s.flagd import restore_flagd_snapshot
from server.k8s.metrics import (
    poll_promql,
    verify_otel_metrics_present,
    verify_prometheus_up,
)
from server.k8s.queries import (
    get_pod_logs,
    get_service_k8s_data,
    read_fault_metadata,
    snapshot_flagd_configmap,
    snapshot_service_configmaps,
    verify_deployments_exist,
)
from server.k8s.dependency_graph import bfs_downstream, bfs_upstream
from server.observation.fiction import (
    check_mttm_streak,
    compute_bcm_delta,
    compute_sim_tick,
    update_slo_budget,
)
from server.observation.schema import (
    ActionHistoryEntry,
    Alert,
    DependencyTrace,
    ServiceMetrics,
    SystemObservation,
)
from server.observation.translator import (
    build_all_service_metrics,
    build_metric_trends,
    build_single_service_metrics,
    parse_log_lines,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-30s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("bridge")


# ---------------------------------------------------------------------------
# Episode State (§6)
# ---------------------------------------------------------------------------
class EpisodeState:
    """Mutable in-memory episode state, reset at each /reset call."""

    def __init__(self) -> None:
        self.current_alert: Alert | None = None
        self.current_service: str | None = None
        self.firing_promql_expression: str | None = None
        self.fault_archetype: str | None = None
        self.episode_start_wall_time: float = 0.0
        self.sim_tick: int = 0
        self.slo_budget_remaining: float = 60.0
        self.bad_customer_minutes: float = 0.0
        self.mttm_achieved_tick: int | None = None
        self.action_history: deque[dict] = deque(maxlen=10)
        self.user_facing_healthy_streak: int = 0
        self.circuit_break_policies_applied: list[str] = []
        self.cached_service_metrics: dict[str, ServiceMetrics] = {}
        self._last_update_time: float = 0.0


# ---------------------------------------------------------------------------
# Global server state (populated during startup)
# ---------------------------------------------------------------------------
_cfg: BridgeConfig | None = None
_apps_api: AppsV1Api | None = None
_core_api: CoreV1Api | None = None
_net_api: NetworkingV1Api | None = None
_http_client: httpx.Client | None = None
_episode = EpisodeState()

_flagd_snapshot: dict | None = None
_service_cm_snapshots: dict[str, dict[str, dict]] = {}


# ---------------------------------------------------------------------------
# Startup / Shutdown (§4)
# ---------------------------------------------------------------------------
def _startup_sequence(cfg: BridgeConfig) -> None:
    """SPEC-P4 §4 Steps 1-8. All steps must pass or we exit(1)."""
    global _cfg, _apps_api, _core_api, _net_api, _http_client
    global _flagd_snapshot, _service_cm_snapshots

    _cfg = cfg
    logger.info("Step 1: Config loaded and validated.")

    # Step 2: kubeconfig
    try:
        k8s_config.load_kube_config(
            config_file=cfg.kubeconfig_path,
            context=cfg.kubeconfig_context,
        )
        logger.info("Step 2: kubeconfig loaded (context=%s).", cfg.kubeconfig_context)
    except Exception as exc:
        logger.critical("Step 2 FAILED: %s", exc)
        sys.exit(1)

    _apps_api = AppsV1Api()
    _core_api = CoreV1Api()
    _net_api = NetworkingV1Api()

    # Step 3: verify K8s API
    try:
        _core_api.list_namespace()
        logger.info("Step 3: Kubernetes API reachable.")
    except Exception as exc:
        logger.critical("Step 3 FAILED — cannot reach K8s API: %s", exc)
        sys.exit(1)

    # Step 4: verify 15 deployments
    missing = verify_deployments_exist(_apps_api, cfg.otel_demo_namespace, cfg.application_services)
    if missing:
        logger.critical("Step 4 FAILED — missing deployments: %s", missing)
        sys.exit(1)
    logger.info("Step 4: All 15 application deployments verified.")

    # Step 5: snapshot flagd ConfigMap
    _flagd_snapshot = snapshot_flagd_configmap(_core_api, cfg.otel_demo_namespace)
    logger.info("Step 5: flagd ConfigMap snapshot %s.", "taken" if _flagd_snapshot else "empty (no flagd CM found)")

    # Step 6: snapshot service ConfigMaps
    _service_cm_snapshots = snapshot_service_configmaps(
        _core_api, cfg.otel_demo_namespace, cfg.application_services,
    )
    logger.info("Step 6: Service ConfigMap snapshots taken.")

    # Step 7: verify Prometheus
    _http_client = httpx.Client()
    if not verify_prometheus_up(_http_client, cfg.prometheus_url):
        logger.critical("Step 7 FAILED — Prometheus not reachable at %s", cfg.prometheus_url)
        sys.exit(1)
    logger.info("Step 7: Prometheus reachable.")

    # Step 8: verify OTel metrics
    count = verify_otel_metrics_present(
        _http_client, cfg.prometheus_url, cfg.otel_demo_namespace, cfg.application_services,
    )
    if count < 10:
        logger.warning(
            "Step 8: Only %d/15 services have OTel metrics (want ≥10). Continuing — collector may still be warming up.",
            count,
        )
    else:
        logger.info("Step 8: OTel metrics present for %d/15 services.", count)

    logger.info("✅ Startup complete. Bridge ready on port 8002.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config and run the 8-step startup before accepting requests."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None, help="Path to bridge config YAML")
    args, _unknown = parser.parse_known_args()

    cfg = load_config(args.config)
    _startup_sequence(cfg)
    yield
    # Shutdown
    if _http_client:
        _http_client.close()


app = FastAPI(
    title="LiveK8sEnv Bridge",
    description="SPEC-P4 v2 — Bridge between the real OTel Demo cluster and the RL agent.",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Global Exception Handler (§12)
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """SPEC-P4 §12: log full traceback, return 500, permit fresh /reset."""
    global _episode
    logger.error(
        "Unhandled exception on %s %s:\n%s",
        request.method,
        request.url.path,
        traceback.format_exc(),
    )
    # Reset episode state to permit a fresh /reset
    _episode = EpisodeState()
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class ResetRequest(BaseModel):
    service: str


class StepRequest(BaseModel):
    action_name: str
    parameters: dict = {}


class StepResponse(BaseModel):
    observation: dict
    reward: float | None = None
    done: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_observation() -> SystemObservation:
    """Assemble a full SystemObservation from cached metrics + fiction fields."""
    assert _cfg is not None

    tick = compute_sim_tick(_episode.episode_start_wall_time, _cfg.sim_tick_seconds)
    _episode.sim_tick = tick

    budget_pct = (
        (_episode.slo_budget_remaining / _cfg.slo_initial_budget) * 100.0
        if _cfg.slo_initial_budget > 0
        else 0.0
    )

    return SystemObservation(
        services=_episode.cached_service_metrics,
        dependency_graph=DEPENDENCY_GRAPH,
        active_alerts=[_episode.current_alert] if _episode.current_alert else [],
        action_history=[
            ActionHistoryEntry(**entry) for entry in _episode.action_history
        ],
        sim_tick=tick,
        slo_budget_remaining_pct=round(budget_pct, 2),
        bad_customer_minutes=round(_episode.bad_customer_minutes, 4),
        mttm_achieved_tick=_episode.mttm_achieved_tick,
    )


def _update_fiction_fields() -> None:
    """Update all fiction/computed fields based on current state."""
    assert _cfg is not None

    now = time.time()
    tick = compute_sim_tick(_episode.episode_start_wall_time, _cfg.sim_tick_seconds)

    # Elapsed ticks since last update
    if _episode._last_update_time > 0:
        elapsed_seconds = now - _episode._last_update_time
        elapsed_ticks = elapsed_seconds / _cfg.sim_tick_seconds
    else:
        elapsed_ticks = 0.0

    _episode._last_update_time = now
    _episode.sim_tick = tick

    # BCM integral (§9.2)
    if elapsed_ticks > 0 and _episode.cached_service_metrics:
        delta = compute_bcm_delta(_episode.cached_service_metrics, elapsed_ticks)
        _episode.bad_customer_minutes += delta

    # MTTM streak (§9.3)
    streak, mttm = check_mttm_streak(
        _episode.cached_service_metrics,
        _cfg.slo_user_facing_services,
        _episode.user_facing_healthy_streak,
        _cfg.mttm_streak_ticks,
        _episode.mttm_achieved_tick,
        tick,
    )
    _episode.user_facing_healthy_streak = streak
    _episode.mttm_achieved_tick = mttm

    # SLO budget (§9.4)
    shield_active = streak >= _cfg.mttm_streak_ticks
    _episode.slo_budget_remaining = update_slo_budget(
        _episode.slo_budget_remaining,
        elapsed_ticks,
        _cfg.slo_burn_rate_per_tick,
        _cfg.slo_mitigation_shield_factor,
        shield_active,
    )


def _add_action_history(action: str, target: str | None, feedback: str) -> None:
    """Append to action history deque (§9.5)."""
    _episode.action_history.append({
        "action": action,
        "target": target,
        "tick": _episode.sim_tick,
        "feedback": feedback,
    })


def _validate_target(target: str | None) -> str:
    """Validate target is one of the 15 application services."""
    if target is None or target not in APPLICATION_SERVICES:
        raise HTTPException(400, f"Invalid target service: {target}")
    return target


def _check_episode_done() -> bool:
    """Check if episode should end (SLO budget exhausted)."""
    return _episode.slo_budget_remaining <= 0


# ---------------------------------------------------------------------------
# Health Endpoints (§5)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """SPEC-P4 §5: basic liveness."""
    return {"status": "ok", "server": "livek8senv"}


@app.get("/health/kubernetes")
async def health_kubernetes():
    """SPEC-P4 §5: K8s API connectivity."""
    assert _core_api is not None
    try:
        _core_api.list_namespace()
        return {"status": "ok", "detail": "kubernetes API reachable"}
    except ApiException as exc:
        raise HTTPException(503, detail=str(exc.reason))


@app.get("/health/prometheus")
async def health_prometheus():
    """SPEC-P4 §5: Prometheus connectivity."""
    assert _cfg is not None and _http_client is not None
    if verify_prometheus_up(_http_client, _cfg.prometheus_url):
        return {"status": "ok", "detail": "prometheus reachable"}
    raise HTTPException(503, detail="Prometheus not reachable")


# ---------------------------------------------------------------------------
# /reset (§7)
# ---------------------------------------------------------------------------
@app.post("/reset")
async def reset(req: ResetRequest):
    """SPEC-P4 §7: initialize a new episode."""
    global _episode
    assert _cfg is not None and _core_api is not None
    assert _apps_api is not None and _http_client is not None

    # §7.1 Validation
    if req.service not in APPLICATION_SERVICES:
        raise HTTPException(400, f"Invalid service: {req.service}. Must be one of the 15 application services.")

    # Reset episode state
    _episode = EpisodeState()
    _episode.current_service = req.service
    _episode.episode_start_wall_time = time.time()
    _episode._last_update_time = time.time()
    _episode.slo_budget_remaining = _cfg.slo_initial_budget

    # §7.2 Fault metadata
    fault_meta = read_fault_metadata(_core_api, _cfg.otel_demo_namespace)
    _episode.fault_archetype = fault_meta.archetype

    # §7.3 Full observation assembly
    _episode.cached_service_metrics = build_all_service_metrics(
        _apps_api, _core_api, _http_client, _cfg,
    )

    # §8.5 Alerts
    alerts = fetch_active_alerts(
        _http_client,
        _cfg.alertmanager_url,
        _cfg.otel_demo_namespace,
        _episode.episode_start_wall_time,
        _cfg.sim_tick_seconds,
        _episode.fault_archetype,
    )
    if alerts:
        _episode.current_alert = alerts[0]
        # Store firing PromQL for resolution verification
        _episode.firing_promql_expression = get_firing_promql(
            alerts[0].alertname, req.service,
        )

    obs = _build_observation()

    return {
        "observation": obs.model_dump(),
        "reward": None,
        "done": False,
    }


# ---------------------------------------------------------------------------
# /step (§10)
# ---------------------------------------------------------------------------
@app.post("/step")
async def step(req: StepRequest):
    """SPEC-P4 §10: execute an agent action and return updated observation."""
    assert _cfg is not None and _core_api is not None
    assert _apps_api is not None and _http_client is not None
    assert _net_api is not None

    action_name = req.action_name
    params = req.parameters

    VALID_ACTIONS = {
        "fetch_logs", "get_metrics_detail", "trace_dependencies",
        "restart_service", "rollback_deploy", "revert_config",
        "scale_replicas", "circuit_break", "declare_resolved", "escalate",
    }
    if action_name not in VALID_ACTIONS:
        raise HTTPException(400, f"Unknown action: {action_name}")

    feedback = ""

    try:
        if action_name == "fetch_logs":
            feedback = _action_fetch_logs(params)
        elif action_name == "get_metrics_detail":
            feedback = _action_get_metrics_detail(params)
        elif action_name == "trace_dependencies":
            feedback = _action_trace_dependencies(params)
        elif action_name == "restart_service":
            feedback = _action_restart_service(params)
        elif action_name == "rollback_deploy":
            feedback = _action_rollback_deploy(params)
        elif action_name == "revert_config":
            feedback = _action_revert_config(params)
        elif action_name == "scale_replicas":
            feedback = _action_scale_replicas(params)
        elif action_name == "circuit_break":
            feedback = _action_circuit_break(params)
        elif action_name == "declare_resolved":
            return _action_declare_resolved(params)
        elif action_name == "escalate":
            feedback = _action_escalate(params)
    except ApiException as exc:
        if exc.status == 404:
            raise HTTPException(404, detail=str(exc.reason))
        elif exc.status == 403:
            raise HTTPException(403, detail=str(exc.reason))
        else:
            raise HTTPException(502, detail=str(exc.reason))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error during action %s", action_name)
        raise HTTPException(500, detail=str(exc))

    target = params.get("target")
    _add_action_history(action_name, target, feedback)
    _update_fiction_fields()

    done = _check_episode_done()
    obs = _build_observation()

    return {
        "observation": obs.model_dump(),
        "reward": None,
        "done": done,
    }


# ---------------------------------------------------------------------------
# Action implementations (§10.1 – §10.10)
# ---------------------------------------------------------------------------

def _action_fetch_logs(params: dict) -> str:
    """§10.1: fetch and re-template logs."""
    assert _core_api is not None and _cfg is not None
    target = _validate_target(params.get("target"))

    raw_lines = get_pod_logs(_core_api, _cfg.otel_demo_namespace, target)
    entries = parse_log_lines(raw_lines)

    if target in _episode.cached_service_metrics:
        _episode.cached_service_metrics[target].recent_logs = entries

    return f"fetched {len(entries)} log lines for {target}"


def _action_get_metrics_detail(params: dict) -> str:
    """§10.2: refresh single service metrics + trends."""
    assert _apps_api is not None and _core_api is not None
    assert _http_client is not None and _cfg is not None
    target = _validate_target(params.get("target"))

    refreshed = build_single_service_metrics(
        _apps_api, _core_api, _http_client, _cfg, target,
    )
    trends = build_metric_trends(
        _http_client, _cfg, target, refreshed, _cfg.sim_tick_seconds,
    )
    refreshed.metric_trends = trends
    _episode.cached_service_metrics[target] = refreshed

    return f"refreshed metrics and trend for {target}"


def _action_trace_dependencies(params: dict) -> str:
    """§10.3: compute upstream/downstream via BFS."""
    target = _validate_target(params.get("target"))

    upstream = bfs_upstream(target)
    downstream = bfs_downstream(target)

    if target in _episode.cached_service_metrics:
        _episode.cached_service_metrics[target].dependency_trace = DependencyTrace(
            upstream=upstream, downstream=downstream,
        )

    return f"traced dependencies for {target}: {len(upstream)} upstream, {len(downstream)} downstream"


def _action_restart_service(params: dict) -> str:
    """§10.4: rolling restart."""
    assert _apps_api is not None and _cfg is not None
    target = _validate_target(params.get("target"))
    return restart_service(_apps_api, target, _cfg.otel_demo_namespace)


def _action_rollback_deploy(params: dict) -> str:
    """§10.5: rollback to previous revision."""
    assert _apps_api is not None and _cfg is not None
    target = _validate_target(params.get("target"))
    result = rollback_deploy(_apps_api, target, _cfg.otel_demo_namespace)
    if result == "__NO_PREVIOUS_REVISION__":
        raise HTTPException(400, detail="No previous revision")
    return result


def _action_revert_config(params: dict) -> str:
    """§10.6: revert config via flagd or ConfigMap snapshot."""
    assert _core_api is not None and _cfg is not None
    target = _validate_target(params.get("target"))

    # Path 1: flagd
    fault_meta = read_fault_metadata(_core_api, _cfg.otel_demo_namespace)
    if fault_meta.injection_mechanism == "flagd" and _flagd_snapshot:
        feedback = restore_flagd_snapshot(_core_api, _cfg.otel_demo_namespace, _flagd_snapshot)
        return f"reverted config for {target}"

    # Path 2: service ConfigMap
    svc_snapshots = _service_cm_snapshots.get(target, {})
    if svc_snapshots:
        for cm_name, snapshot_data in svc_snapshots.items():
            revert_configmap(_core_api, _cfg.otel_demo_namespace, cm_name, snapshot_data)
        return f"reverted config for {target}"

    raise HTTPException(400, detail=f"No config snapshot available for {target}")


def _action_scale_replicas(params: dict) -> str:
    """§10.7: scale deployment."""
    assert _apps_api is not None and _cfg is not None
    target = _validate_target(params.get("target"))
    replicas = params.get("replicas")
    if replicas is None or not isinstance(replicas, int) or replicas < 1 or replicas > 5:
        raise HTTPException(400, detail="replicas must be an integer in [1, 5]")
    return scale_replicas(_apps_api, target, _cfg.otel_demo_namespace, replicas)


def _action_circuit_break(params: dict) -> str:
    """§10.8: create deny-all-ingress NetworkPolicy."""
    assert _net_api is not None and _cfg is not None
    target = _validate_target(params.get("target"))
    result = circuit_break(_net_api, target, _cfg.otel_demo_namespace)
    policy_name = f"firewatch-circuit-break-{target}"
    _episode.circuit_break_policies_applied.append(policy_name)
    return result


def _action_declare_resolved(params: dict) -> dict:
    """§10.9: cleanup + resolution verification poll."""
    assert _net_api is not None and _cfg is not None
    assert _http_client is not None

    summary = params.get("summary", "")
    logger.info("declare_resolved called with summary: %s", summary)

    # Step 1: cleanup NetworkPolicies
    if _episode.circuit_break_policies_applied:
        failures = delete_network_policies(
            _net_api, _cfg.otel_demo_namespace,
            _episode.circuit_break_policies_applied,
        )
        if failures:
            logger.warning("Failed to delete some NetworkPolicies: %s", failures)
        _episode.circuit_break_policies_applied.clear()

    # Step 2: resolution verification poll
    done = False
    if _episode.firing_promql_expression:
        consecutive_empty = 0
        deadline = time.time() + _cfg.resolution_poll_timeout_seconds

        while time.time() < deadline:
            has_results = poll_promql(
                _http_client,
                _cfg.prometheus_url,
                _episode.firing_promql_expression,
            )
            if not has_results:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    done = True
                    break
            else:
                consecutive_empty = 0
            time.sleep(_cfg.resolution_poll_interval_seconds)
    else:
        # No firing expression — no way to verify, mark as done
        done = True

    feedback = f"resolution verification: {'done' if done else 'not done'}"
    _add_action_history("declare_resolved", None, feedback)
    _update_fiction_fields()

    obs = _build_observation()
    return {
        "observation": obs.model_dump(),
        "reward": None,
        "done": done,
    }


def _action_escalate(params: dict) -> str:
    """§10.10: log escalation event."""
    logger.warning("ESCALATION — agent has given up on autonomous resolution.")
    return "escalated"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Script entry point: ``uv run start-bridge``."""
    uvicorn.run(
        "server.live_k8s_env:app",
        host="0.0.0.0",
        port=8002,
        log_level="info",
    )


if __name__ == "__main__":
    main()
