"""SPEC-P3 §5.5 — inject_network_partition_payment: Network disruption via Chaos Mesh NetworkChaos.

Usage:
    uv run inject-network-partition-payment
    uv run inject-network-partition-payment --service shipping
    uv run inject-network-partition-payment --duration 3m
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from infra.fault_scenarios.common import (
    APPLICATION_SERVICES,
    INFRA_SERVICES,
    NAMESPACE,
    apply_chaos_crd,
    precondition_check,
    print_summary,
    write_fault_metadata,
)


def build_network_delay_yaml(service: str, duration: str) -> str:
    """Build a NetworkChaos delay CRD per SPEC-P3 §5.5 Path A."""
    return f"""\
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: netpart-delay-{service}
  namespace: {NAMESPACE}
  labels:
    firewatch-fault-archetype: network_partition
spec:
  action: delay
  mode: one
  duration: "{duration}"
  selector:
    namespaces:
      - {NAMESPACE}
    labelSelectors:
      app.kubernetes.io/name: {service}
  direction: both
  delay:
    latency: "1000ms"
    correlation: "25"
    jitter: "200ms"
  suspend: false
"""


def build_network_loss_yaml(service: str, duration: str) -> str:
    """Build a NetworkChaos loss CRD per SPEC-P3 §5.5."""
    return f"""\
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: netpart-loss-{service}
  namespace: {NAMESPACE}
  labels:
    firewatch-fault-archetype: network_partition
spec:
  action: loss
  mode: one
  duration: "{duration}"
  selector:
    namespaces:
      - {NAMESPACE}
    labelSelectors:
      app.kubernetes.io/name: {service}
  direction: both
  loss:
    loss: "50"
    correlation: "25"
  suspend: false
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P3 §5.5: Inject network_partition fault")
    parser.add_argument("--service", default="payment", help="Target service (default: payment)")
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

    print(f"🔧 SPEC-P3: Injecting network_partition → {service}")
    print("=" * 50)

    if not precondition_check():
        sys.exit(1)

    # Apply both delay and loss CRDs (SPEC-P3 §5.5 Path A).
    print("\n🚀 Applying NetworkChaos CRDs (delay + loss)...")

    delay_crd = build_network_delay_yaml(service, duration)
    if not apply_chaos_crd(delay_crd):
        print("❌ Failed to apply NetworkChaos delay CRD.")
        sys.exit(1)
    print("  ✅ Delay CRD applied (1000ms ± 200ms jitter)")

    loss_crd = build_network_loss_yaml(service, duration)
    if not apply_chaos_crd(loss_crd):
        print("❌ Failed to apply NetworkChaos loss CRD.")
        sys.exit(1)
    print("  ✅ Loss CRD applied (50% packet loss)")

    write_fault_metadata("network_partition", service, "chaos_mesh")

    print_summary(
        archetype="network_partition",
        target_service=service,
        mechanism="Chaos Mesh NetworkChaos (delay 1s + 50% packet loss)",
        expected_alerts=["NetworkPartition", "HighLatency"],
    )


if __name__ == "__main__":
    main()
