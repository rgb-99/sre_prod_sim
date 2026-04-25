"""SPEC-P3 §5.1 — inject_oom_ad: OOM-kill the ad service via Chaos Mesh StressChaos.

Usage:
    uv run inject-oom-ad
    uv run inject-oom-ad --duration 3m
    uv run inject-oom-ad --service ad
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from infra.fault_scenarios.common import (
    APPLICATION_SERVICES,
    INFRA_SERVICES,
    NAMESPACE,
    apply_chaos_crd,
    get_container_memory_limit,
    parse_memory_to_bytes,
    precondition_check,
    print_summary,
    write_fault_metadata,
)


def build_stress_chaos_yaml(service: str, mem_size: str, duration: str) -> str:
    """Build a StressChaos CRD YAML per SPEC-P3 §4 + §5.1."""
    return f"""\
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: oom-{service}
  namespace: {NAMESPACE}
  labels:
    firewatch-fault-archetype: oom
spec:
  mode: one
  duration: "{duration}"
  selector:
    namespaces:
      - {NAMESPACE}
    labelSelectors:
      app.kubernetes.io/name: {service}
  stressors:
    memory:
      workers: 2
      size: "{mem_size}"
  suspend: false
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P3 §5.1: Inject OOM fault")
    parser.add_argument("--service", default="ad", help="Target service (default: ad)")
    parser.add_argument("--duration", default="5m", help="Fault duration (default: 5m)")
    args = parser.parse_args()

    service = args.service
    duration = args.duration

    if service in INFRA_SERVICES:
        print(f"❌ Cannot target infrastructure service '{service}'. Use application services only.")
        sys.exit(1)
    if service not in APPLICATION_SERVICES:
        print(f"❌ Unknown service '{service}'. Valid: {', '.join(APPLICATION_SERVICES)}")
        sys.exit(1)

    print(f"🔧 SPEC-P3: Injecting OOM fault → {service}")
    print("=" * 50)

    if not precondition_check():
        sys.exit(1)

    # Compute memory stress size: 95% of container limit (SPEC-P3 §5.1).
    mem_limit = get_container_memory_limit(service)
    if not mem_limit:
        print(f"  ⚠️  No memory limit found for '{service}'. Using 230Mi as fallback (95% of 250Mi default).")
        mem_size = "230Mi"
    else:
        limit_bytes = parse_memory_to_bytes(mem_limit)
        stress_bytes = int(limit_bytes * 0.95)
        # Express as MiB for readability.
        stress_mib = stress_bytes // (1024 * 1024)
        mem_size = f"{stress_mib}Mi"
        print(f"  Memory limit: {mem_limit} → stress size: {mem_size} (95%)")

    # Build and apply the CRD.
    crd = build_stress_chaos_yaml(service, mem_size, duration)
    print(f"\n🚀 Applying StressChaos CRD...")
    if not apply_chaos_crd(crd):
        print("❌ Failed to apply StressChaos CRD.")
        sys.exit(1)

    # Write fault metadata.
    write_fault_metadata("oom", service, "chaos_mesh")

    print_summary(
        archetype="oom",
        target_service=service,
        mechanism="Chaos Mesh StressChaos (memory stress → OOM kill)",
        expected_alerts=["ServiceOOMKilled", "ServiceMemoryCritical"],
    )


if __name__ == "__main__":
    main()
