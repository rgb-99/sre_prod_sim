"""SPEC-P1 Layer 0 — Create the 'firewatch' KinD cluster and install metrics-server.

Usage:
    python -m scripts.setup_cluster          # create cluster + metrics-server
    python -m scripts.setup_cluster --skip-metrics  # cluster only

Idempotent: if the cluster already exists and is healthy, this is a no-op.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
KIND_CONFIG = INFRA_DIR / "kind-config.yaml"

# Metrics-server pinned version (matches infra/versions.yaml).
METRICS_SERVER_VERSION = "v0.7.2"
METRICS_SERVER_URL = (
    f"https://github.com/kubernetes-sigs/metrics-server/releases/download/"
    f"{METRICS_SERVER_VERSION}/components.yaml"
)


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, printing the command for transparency."""
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def cluster_exists() -> bool:
    """Check if the 'firewatch' KinD cluster already exists."""
    result = run(["kind", "get", "clusters"], capture=True, check=False)
    return CLUSTER_NAME in result.stdout.splitlines()


def cluster_healthy() -> bool:
    """Verify the cluster is reachable and the node is Ready."""
    result = run(
        ["kubectl", "cluster-info", "--context", CONTEXT],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return False

    result = run(
        ["kubectl", "get", "nodes", "--context", CONTEXT, "-o", "jsonpath={.items[0].status.conditions[-1].type}"],
        check=False,
        capture=True,
    )
    return result.stdout.strip() == "Ready"


def create_cluster() -> None:
    """Create the KinD cluster from config."""
    if not KIND_CONFIG.exists():
        print(f"ERROR: Kind config not found at {KIND_CONFIG}")
        sys.exit(1)

    print(f"\n🔧 Creating KinD cluster '{CLUSTER_NAME}'...")
    run(["kind", "create", "cluster", "--name", CLUSTER_NAME, "--config", str(KIND_CONFIG)])

    # Post-creation verification (SPEC-P1 §3.3).
    print("\n🔍 Post-creation verification...")
    run(["kubectl", "cluster-info", "--context", CONTEXT])
    run(["kubectl", "get", "nodes", "--context", CONTEXT])

    # Verify node label.
    result = run(
        ["kubectl", "get", "nodes", "--context", CONTEXT, "-o",
         "jsonpath={.items[0].metadata.labels.kubernetes\\.io/hostname}"],
        capture=True,
    )
    expected_hostname = f"{CLUSTER_NAME}-control-plane"
    if result.stdout.strip() != expected_hostname:
        print(f"WARNING: Expected hostname label '{expected_hostname}', got '{result.stdout.strip()}'")

    print(f"✅ Cluster '{CLUSTER_NAME}' created and verified.")


def install_metrics_server() -> None:
    """Install metrics-server and patch for KinD's self-signed certs (SPEC-P1 §4)."""
    print("\n📦 Installing metrics-server...")
    run(["kubectl", "apply", "-f", METRICS_SERVER_URL, "--context", CONTEXT])

    # Mandatory patch: --kubelet-insecure-tls (SPEC-P1 §4.2).
    print("🔧 Patching metrics-server for KinD (--kubelet-insecure-tls)...")
    patch_json = (
        '{"spec":{"template":{"spec":{"containers":[{"name":"metrics-server",'
        '"args":["--cert-dir=/tmp","--secure-port=10250",'
        '"--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname",'
        '"--kubelet-use-node-status-port","--metric-resolution=15s",'
        '"--kubelet-insecure-tls"]}]}}}}'
    )
    run([
        "kubectl", "patch", "deployment", "metrics-server",
        "-n", "kube-system",
        "--type=strategic",
        f"-p={patch_json}",
        "--context", CONTEXT,
    ])

    # Wait for metrics-server to become ready.
    print("⏳ Waiting for metrics-server rollout...")
    run([
        "kubectl", "rollout", "status", "deployment/metrics-server",
        "-n", "kube-system", "--timeout=120s", "--context", CONTEXT,
    ])

    # Verify (SPEC-P1 §4.3) — kubectl top nodes may take ~60s after install.
    print("⏳ Waiting for metrics to become available (up to 90s)...")
    for attempt in range(18):
        result = run(
            ["kubectl", "top", "nodes", "--context", CONTEXT],
            check=False,
            capture=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            print("✅ Metrics-server installed and verified.")
            return
        time.sleep(5)

    print("⚠️  metrics-server installed but 'kubectl top nodes' not yet returning data.")
    print("   This is usually transient — retry in 60 seconds.")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P1 Layer 0: KinD cluster setup")
    parser.add_argument("--skip-metrics", action="store_true", help="Skip metrics-server installation")
    args = parser.parse_args()

    # Idempotent: skip if cluster exists and is healthy.
    if cluster_exists():
        if cluster_healthy():
            print(f"✅ Cluster '{CLUSTER_NAME}' already exists and is healthy. Skipping creation.")
        else:
            print(f"⚠️  Cluster '{CLUSTER_NAME}' exists but is unhealthy. Deleting and recreating...")
            run(["kind", "delete", "cluster", "--name", CLUSTER_NAME])
            create_cluster()
    else:
        create_cluster()

    if not args.skip_metrics:
        install_metrics_server()

    print("\n🎉 Layer 0 setup complete.")
    print(f"   Context: {CONTEXT}")
    print(f"   Node:    {CLUSTER_NAME}-control-plane")


if __name__ == "__main__":
    main()
