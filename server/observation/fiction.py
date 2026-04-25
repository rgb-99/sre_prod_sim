"""SPEC-P4 §9 — Fiction and computed field maintenance.

Maintains the training-env observation fields that don't exist natively in K8s:
sim_tick, slo_budget_remaining, bad_customer_minutes, mttm_achieved_tick.

All formulas match the training env exactly.
"""

from __future__ import annotations

import math
import time


def compute_sim_tick(episode_start_time: float, sim_tick_seconds: int) -> int:
    """SPEC-P4 §9.1: pure computation, no state."""
    if episode_start_time <= 0:
        return 0
    return int(math.floor((time.time() - episode_start_time) / sim_tick_seconds))


def compute_bcm_delta(
    services_snapshot: dict,
    elapsed_ticks: float,
) -> float:
    """SPEC-P4 §9.2: bad_customer_minutes integral delta.

    Args:
        services_snapshot: dict of service_name → ServiceMetrics (or dict
                           with error_rate and latency_p99 keys).
        elapsed_ticks: fractional ticks since last update.
    """
    delta = 0.0
    for _name, metrics in services_snapshot.items():
        if hasattr(metrics, "http_server_error_rate"):
            error_rate = metrics.http_server_error_rate
            latency_p99 = metrics.http_server_request_duration_p99
        else:
            error_rate = metrics.get("http_server_error_rate", 0.0)
            latency_p99 = metrics.get("http_server_request_duration_p99", 0.0)

        latency_normalized = max(0.0, min((latency_p99 - 0.5) / 2.0, 2.0))
        delta += (error_rate + latency_normalized * 0.5) * (30 / 60)

    return delta * elapsed_ticks


def update_slo_budget(
    current_budget: float,
    ticks_elapsed: float,
    burn_rate: float,
    shield_factor: float,
    shield_active: bool,
) -> float:
    """SPEC-P4 §9.4: SLO budget decay per tick.

    Returns the new budget value (clamped to 0).
    """
    active_rate = burn_rate * shield_factor if shield_active else burn_rate
    new_budget = current_budget - active_rate * ticks_elapsed
    return max(0.0, new_budget)


def check_mttm_streak(
    services_snapshot: dict,
    user_facing_services: list[str],
    current_streak: int,
    streak_threshold: int,
    current_mttm: int | None,
    current_tick: int,
) -> tuple[int, int | None]:
    """SPEC-P4 §9.3: update MTTM streak and achievement tick.

    Returns:
        (new_streak, new_mttm_achieved_tick)
    """
    all_healthy = True
    for svc_name in user_facing_services:
        metrics = services_snapshot.get(svc_name)
        if metrics is None:
            all_healthy = False
            break
        status = getattr(metrics, "status", None) or metrics.get("status", "unknown")
        if status not in ("healthy", "degraded"):
            all_healthy = False
            break

    if all_healthy:
        new_streak = current_streak + 1
    else:
        return 0, None

    new_mttm = current_mttm
    if new_streak >= streak_threshold and current_mttm is None:
        new_mttm = current_tick

    return new_streak, new_mttm
