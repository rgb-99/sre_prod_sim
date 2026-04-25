"""SPEC-P3 §5.2 — inject_memory_leak_ad_jvm: JVM memory leak via flagd adServiceManualGc.

Usage:
    uv run inject-memory-leak-jvm
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
    print("🔧 SPEC-P3: Injecting memory_leak (JVM path) → ad service")
    print("=" * 50)

    if not precondition_check():
        sys.exit(1)

    # Toggle the flagd flag (SPEC-P3 §5.2 Path A).
    print("\n🚀 Enabling adServiceManualGc flag...")
    if not toggle_flagd_flag("adServiceManualGc", "ENABLED"):
        print("❌ Failed to enable adServiceManualGc flag.")
        sys.exit(1)

    write_fault_metadata("memory_leak", "ad", "flagd")

    print_summary(
        archetype="memory_leak",
        target_service="ad (JVM path)",
        mechanism="flagd adServiceManualGc=ENABLED (full GC every request)",
        expected_alerts=["ServiceMemoryCritical", "GCPressure"],
    )
    print("\n  ℹ️  Keep enabled ≥3m for memory to visibly accumulate.")
    print("     Run 'uv run cleanup-fault' to halt the fault.")


if __name__ == "__main__":
    main()
