"""SPEC-P2 §6 — Start persistent port-forwards for Prometheus and AlertManager.

Usage:
    uv run start-portforwards          # start both forwards
    uv run start-portforwards --prom   # Prometheus only
    uv run start-portforwards --am     # AlertManager only

Port-forwards run as background processes with auto-restart on pod restart.
PIDs are written to scripts/.portforward_pids for cleanup by stop_portforwards.py.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
MONITORING_NS = "monitoring"

PID_FILE = Path(__file__).resolve().parent / ".portforward_pids"

# Port-forward definitions per SPEC-P2 §6.
FORWARDS = {
    "prometheus": {
        "service": "svc/prometheus",
        "host_port": 9090,
        "cluster_port": 9090,
    },
    "alertmanager": {
        "service": "svc/alertmanager",
        "host_port": 9093,
        "cluster_port": 9093,
    },
}


def is_port_in_use(port: int) -> bool:
    """Check if a port is already in use on the host."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_forward(name: str, config: dict) -> int | None:
    """Start a kubectl port-forward as a background process.

    Returns the PID of the wrapper process, or None on failure.
    """
    svc = config["service"]
    host_port = config["host_port"]
    cluster_port = config["cluster_port"]

    if is_port_in_use(host_port):
        print(f"  ⚠️  Port {host_port} already in use — skipping {name}.")
        print(f"      (Existing forward may still be running. Use 'uv run stop-portforwards' first.)")
        return None

    cmd = [
        "kubectl", "port-forward",
        svc,
        f"{host_port}:{cluster_port}",
        "-n", MONITORING_NS,
        "--context", CONTEXT,
    ]

    print(f"  Starting {name} port-forward ({host_port} → {svc}:{cluster_port})...")

    # Start as a detached subprocess.
    # On Windows, use CREATE_NEW_PROCESS_GROUP; on Unix, use preexec_fn=os.setsid.
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)

    # Give it a moment to bind.
    time.sleep(2)

    if proc.poll() is not None:
        print(f"  ❌ {name} port-forward failed to start (exit code {proc.returncode})")
        return None

    print(f"  ✅ {name} port-forward started (PID {proc.pid})")
    return proc.pid


def save_pids(pids: dict[str, int]) -> None:
    """Save PIDs to the PID file for cleanup."""
    existing = load_pids()
    existing.update(pids)
    PID_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"\n  PIDs saved to {PID_FILE}")


def load_pids() -> dict[str, int]:
    """Load PIDs from the PID file."""
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def verify_forwards() -> None:
    """Quick health check on forwarded ports."""
    print("\n🔍 Verifying port-forwards...")
    import urllib.request
    import urllib.error

    checks = [
        ("Prometheus", "http://localhost:9090/-/healthy", "Prometheus Server is Healthy."),
        ("AlertManager", "http://localhost:9093/-/healthy", "OK"),
    ]

    for name, url, expected in checks:
        try:
            req = urllib.request.urlopen(url, timeout=5)
            body = req.read().decode("utf-8").strip()
            if expected in body:
                print(f"  ✅ {name} healthy at {url}")
            else:
                print(f"  ⚠️  {name} responded but unexpected body: {body[:100]}")
        except (urllib.error.URLError, OSError) as e:
            print(f"  ⚠️  {name} not reachable at {url} — {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P2 §6: Start port-forwards")
    parser.add_argument("--prom", action="store_true", help="Prometheus only")
    parser.add_argument("--am", action="store_true", help="AlertManager only")
    args = parser.parse_args()

    print("🔌 Starting port-forwards for monitoring stack")
    print("=" * 50)

    # Determine which forwards to start.
    if args.prom:
        targets = {"prometheus": FORWARDS["prometheus"]}
    elif args.am:
        targets = {"alertmanager": FORWARDS["alertmanager"]}
    else:
        targets = FORWARDS

    pids: dict[str, int] = {}
    for name, config in targets.items():
        pid = start_forward(name, config)
        if pid is not None:
            pids[name] = pid

    if pids:
        save_pids(pids)

    # Verify.
    time.sleep(2)
    verify_forwards()

    print("\n🎉 Port-forwards running.")
    print("   Use 'uv run stop-portforwards' to stop them.")


if __name__ == "__main__":
    main()
