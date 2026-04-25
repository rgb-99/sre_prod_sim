"""SPEC-P3 §5.2 — inject_memory_leak_recommendation: Non-JVM memory leak via flagd recommendationCacheFailure.

Usage:
    uv run inject-memory-leak-recommendation
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
    print("🔧 SPEC-P3: Injecting memory_leak (non-JVM path) → recommendation service")
    print("=" * 50)

    if not precondition_check():
        sys.exit(1)

    # Toggle the flagd flag (SPEC-P3 §5.2 Path B).
    print("\n🚀 Enabling recommendationCacheFailure flag...")
    if not toggle_flagd_flag("recommendationCacheFailure", "ENABLED"):
        print("❌ Failed to enable recommendationCacheFailure flag.")
        sys.exit(1)

    write_fault_metadata("memory_leak", "recommendation", "flagd")

    print_summary(
        archetype="memory_leak",
        target_service="recommendation (non-JVM path)",
        mechanism="flagd recommendationCacheFailure=ENABLED (cache accumulation)",
        expected_alerts=["ServiceMemoryCritical"],
    )
    print("\n  ℹ️  Keep enabled ≥3m for memory to visibly accumulate.")
    print("     Run 'uv run cleanup-fault' to halt the fault.")


if __name__ == "__main__":
    main()
