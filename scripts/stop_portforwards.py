"""SPEC-P2 §6 — Stop port-forwards started by start_portforwards.py.

Usage:
    uv run stop-portforwards           # stop all
    uv run stop-portforwards --name prometheus  # stop specific forward
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

PID_FILE = Path(__file__).resolve().parent / ".portforward_pids"


def load_pids() -> dict[str, int]:
    """Load PIDs from the PID file."""
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def kill_process(pid: int) -> bool:
    """Kill a process by PID. Returns True if killed or already dead."""
    try:
        if sys.platform == "win32":
            # On Windows, use taskkill to handle process groups.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False,
                capture_output=True,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return True  # Already dead or inaccessible.


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P2 §6: Stop port-forwards")
    parser.add_argument("--name", type=str, help="Stop a specific forward by name")
    args = parser.parse_args()

    pids = load_pids()
    if not pids:
        print("ℹ️  No active port-forwards found (PID file empty or missing).")
        return

    print("🔌 Stopping port-forwards...")

    targets = {args.name: pids[args.name]} if args.name and args.name in pids else pids
    remaining = dict(pids)

    for name, pid in targets.items():
        if kill_process(pid):
            print(f"  ✅ Stopped {name} (PID {pid})")
            remaining.pop(name, None)
        else:
            print(f"  ⚠️  Could not stop {name} (PID {pid})")

    # Update or remove PID file.
    if remaining:
        PID_FILE.write_text(json.dumps(remaining, indent=2), encoding="utf-8")
    elif PID_FILE.exists():
        PID_FILE.unlink()

    print("\n✅ Port-forwards stopped.")


if __name__ == "__main__":
    main()
