"""SPEC-P3 §6.3/§6.4 — Cleanup all active faults (Chaos Mesh experiments + flagd flags).

Usage:
    uv run cleanup-fault          # full cleanup
    uv run cleanup-fault --force  # skip confirmation

This is the canonical post-experiment and manual-abort cleanup path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from infra.fault_scenarios.common import (
    delete_chaos_experiments,
    delete_fault_metadata,
    has_active_chaos_experiments,
    has_enabled_flagd_flags,
    has_fault_metadata_cm,
    reset_all_flagd_flags,
    wait_for_services_available,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P3 §6.3: Cleanup active faults")
    parser.add_argument("--force", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    print("🧹 SPEC-P3: Fault Cleanup")
    print("=" * 50)

    # Quick status check.
    has_chaos = has_active_chaos_experiments()
    has_flags = has_enabled_flagd_flags()
    has_cm = has_fault_metadata_cm()

    if not has_chaos and not has_flags and not has_cm:
        print("\n  ✅ No active faults detected. Nothing to clean up.")
        return

    print("\n  Active fault indicators found:")
    if has_cm:
        print("    • firewatch-active-fault ConfigMap exists")
    if has_chaos:
        print("    • Chaos Mesh experiments running")
    if has_flags:
        print("    • flagd flags ENABLED")

    if not args.force:
        resp = input("\n  Proceed with cleanup? [y/N]: ").strip().lower()
        if resp not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)

    # Step 1: Delete Chaos Mesh CRDs (SPEC-P3 §6.3 step 1).
    delete_chaos_experiments()

    # Step 2: Reset all flagd flags (SPEC-P3 §6.3 step 2).
    reset_all_flagd_flags()

    # Step 3: Delete fault metadata ConfigMap (SPEC-P3 §6.3 step 3).
    print("\n🗑️  Deleting fault metadata ConfigMap...")
    delete_fault_metadata()

    # Step 4: Wait for services to recover (SPEC-P3 §6.3 step 4).
    wait_for_services_available(timeout_seconds=120)

    print("\n🎉 Fault cleanup complete.")


if __name__ == "__main__":
    main()
