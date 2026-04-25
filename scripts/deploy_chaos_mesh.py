"""SPEC-P3 §2 — Deploy Chaos Mesh for fault injection.

Usage:
    uv run deploy-chaos-mesh              # full deploy
    uv run deploy-chaos-mesh --dry-run    # Helm dry-run only

Requires: KinD cluster 'firewatch' + OTel Demo running (SPEC-P1).
Chaos Mesh must be installed BEFORE running any fault injection scripts.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
CHAOS_NS = "chaos-mesh"

# Pinned chart version (matches infra/versions.yaml).
HELM_REPO_NAME = "chaos-mesh"
HELM_REPO_URL = "https://charts.chaos-mesh.org"
CHART_NAME = f"{HELM_REPO_NAME}/chaos-mesh"
CHART_VERSION = "2.7.0"

# From SPEC-P1 §3.2 / infra/versions.yaml.
CONTAINER_RUNTIME = "containerd"
SOCKET_PATH = "/run/containerd/containerd.sock"

DEPLOY_TIMEOUT = "300s"


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def verify_cluster() -> None:
    result = run(
        ["kubectl", "cluster-info", "--context", CONTEXT],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Cluster '{CLUSTER_NAME}' not reachable. Run setup_cluster.py first.")
        sys.exit(1)


def verify_otel_demo() -> None:
    result = run(
        ["kubectl", "get", "namespace", "otel-demo", "--context", CONTEXT],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        print("ERROR: 'otel-demo' namespace not found. Run deploy_otel_demo.py first.")
        sys.exit(1)


def add_helm_repo() -> None:
    print("\n📦 Adding Chaos Mesh Helm repo...")
    run(["helm", "repo", "add", HELM_REPO_NAME, HELM_REPO_URL], check=False)
    run(["helm", "repo", "update"])


def install_chaos_mesh(dry_run: bool = False) -> None:
    print(f"\n🚀 Installing Chaos Mesh (chart v{CHART_VERSION}) into namespace '{CHAOS_NS}'...")

    cmd = [
        "helm", "upgrade", "--install", "chaos-mesh", CHART_NAME,
        "--version", CHART_VERSION,
        "--namespace", CHAOS_NS,
        "--create-namespace",
        "--kube-context", CONTEXT,
        "--timeout", DEPLOY_TIMEOUT,
        "--wait",
        # SPEC-P3 §2.1 required flags for KinD.
        "--set", f"chaosDaemon.runtime={CONTAINER_RUNTIME}",
        "--set", f"chaosDaemon.socketPath={SOCKET_PATH}",
        "--set", "dashboard.securityMode=false",
    ]

    if dry_run:
        cmd.append("--dry-run")
        print("  (dry-run mode — no changes will be applied)")

    run(cmd)

    if dry_run:
        print("\n✅ Dry-run complete. No resources were created.")
        return

    print(f"\n✅ Chaos Mesh installed in namespace '{CHAOS_NS}'.")


def wait_for_rollout(deployment: str, timeout: str = "180s") -> bool:
    result = run(
        [
            "kubectl", "rollout", "status",
            f"deployment/{deployment}",
            "-n", CHAOS_NS,
            f"--timeout={timeout}",
            "--context", CONTEXT,
        ],
        check=False,
    )
    return result.returncode == 0


def verify_chaos_mesh_pods() -> None:
    """SPEC-P3 §2.2 — Verify chaos-controller-manager, chaos-daemon, chaos-dashboard are Running."""
    print("\n🔍 Verifying Chaos Mesh pods...")

    # Check deployments.
    deployments = ["chaos-controller-manager", "chaos-dashboard"]
    for dep in deployments:
        result = run(
            [
                "kubectl", "get", "deployment", dep,
                "-n", CHAOS_NS, "--context", CONTEXT,
                "-o", "jsonpath={.status.availableReplicas}",
            ],
            check=False,
            capture=True,
        )
        replicas = result.stdout.strip()
        if replicas and int(replicas) >= 1:
            print(f"  ✅ {dep} — available")
        else:
            print(f"  ⚠️  {dep} — available replicas: {replicas or '0'}")

    # chaos-daemon is a DaemonSet, not a Deployment.
    result = run(
        [
            "kubectl", "get", "daemonset", "chaos-daemon",
            "-n", CHAOS_NS, "--context", CONTEXT,
            "-o", "jsonpath={.status.numberReady}",
        ],
        check=False,
        capture=True,
    )
    ready = result.stdout.strip()
    if ready and int(ready) >= 1:
        print(f"  ✅ chaos-daemon (DaemonSet) — {ready} ready")
    else:
        print(f"  ⚠️  chaos-daemon (DaemonSet) — ready: {ready or '0'}")
        print("      If 0, check runtime/socket path: chaosDaemon.runtime and chaosDaemon.socketPath")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P3: Deploy Chaos Mesh")
    parser.add_argument("--dry-run", action="store_true", help="Helm dry-run only")
    parser.add_argument("--skip-verify", action="store_true", help="Skip post-deploy verification")
    args = parser.parse_args()

    print("🔧 SPEC-P3: Deploying Chaos Mesh")
    print("=" * 50)

    print("\n📋 Pre-flight checks...")
    verify_cluster()
    verify_otel_demo()

    add_helm_repo()
    install_chaos_mesh(dry_run=args.dry_run)

    if args.dry_run:
        return

    # Wait for rollouts.
    print("\n⏳ Waiting for Chaos Mesh components...")
    for dep in ["chaos-controller-manager", "chaos-dashboard"]:
        print(f"  Waiting for {dep}...")
        if not wait_for_rollout(dep):
            print(f"  ⚠️  {dep} did not reach ready state within timeout.")

    if not args.skip_verify:
        verify_chaos_mesh_pods()

    print("\n🎉 SPEC-P3 Chaos Mesh deployment complete.")
    print(f"   Namespace: {CHAOS_NS}")
    print("   Dashboard: ClusterIP svc/chaos-dashboard:2333")
    print("\n   Next: Use fault injection scripts in infra/fault_scenarios/")


if __name__ == "__main__":
    main()
