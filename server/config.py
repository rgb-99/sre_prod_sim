"""SPEC-P4 §3 — Bridge configuration loader and validation.

Loads a YAML config file, expands paths, and validates all required fields.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator


_DEFAULT_CONFIG_PATH = Path(__file__).parent / "bridge_config.yaml"


class BridgeConfig(BaseModel):
    """Strongly-typed bridge configuration matching SPEC-P4 §3."""

    kubeconfig_path: str = "~/.kube/config"
    kubeconfig_context: str = "kind-firewatch"
    prometheus_url: str = "http://localhost:9090"
    alertmanager_url: str = "http://localhost:9093"
    otel_demo_namespace: str = "otel-demo"

    application_services: list[str]
    infrastructure_services: list[str]

    sim_tick_seconds: int = 30
    slo_initial_budget: float = 60.0
    slo_burn_rate_per_tick: float = 2.0
    slo_mitigation_shield_factor: float = 0.2
    slo_user_facing_services: list[str] = ["frontend", "checkout"]

    mttm_streak_ticks: int = 2

    resolution_poll_interval_seconds: int = 5
    resolution_poll_timeout_seconds: int = 60
    configmap_snapshot_timeout_seconds: int = 30

    @field_validator("kubeconfig_path")
    @classmethod
    def expand_kubeconfig(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())

    @field_validator("application_services")
    @classmethod
    def validate_app_services(cls, v: list[str]) -> list[str]:
        if len(v) != 15:
            raise ValueError(
                f"application_services must contain exactly 15 entries, got {len(v)}"
            )
        return v


def load_config(path: str | Path | None = None) -> BridgeConfig:
    """Load and validate bridge config from YAML.

    Args:
        path: Path to YAML config. Defaults to bridge_config.yaml in the
              server package directory.

    Raises:
        SystemExit: If the config is missing or invalid.
    """
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    with config_path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise SystemExit(f"Config file must be a YAML mapping, got {type(raw).__name__}")

    try:
        return BridgeConfig(**raw)
    except Exception as exc:
        raise SystemExit(f"Config validation failed: {exc}") from exc
