"""SPEC-P3 §5.4 — inject_config_drift_kafka: Config drift via flagd kafkaQueueProblems.

Usage:
    uv run inject-config-drift-kafka
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from infra.fault_scenarios.common import (
    precondition_check,
    print_summary,
    toggle_flagd_flag,
    write_fault_metadata,
)


def main() -> None:
    print("🔧 SPEC-P3: Injecting config_drift → kafka (queue problems)")
    print("=" * 50)

    if not precondition_check():
        sys.exit(1)

    # Toggle the flagd flag (SPEC-P3 §5.4 Path A).
    print("\n🚀 Enabling kafkaQueueProblems flag...")
    if not toggle_flagd_flag("kafkaQueueProblems", "ENABLED"):
        print("❌ Failed to enable kafkaQueueProblems flag.")
        sys.exit(1)

    write_fault_metadata("config_drift", "kafka", "flagd")

    print_summary(
        archetype="config_drift",
        target_service="kafka (observable on checkout/accounting/fraud-detection)",
        mechanism="flagd kafkaQueueProblems=ENABLED (kafka publishing fails → cascading errors)",
        expected_alerts=["HighLatency", "HighErrorRate"],
    )
    print("\n     Run 'uv run cleanup-fault' to halt the fault.")


if __name__ == "__main__":
    main()
