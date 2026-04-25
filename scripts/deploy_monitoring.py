"""SPEC-P2 — Deploy the observability stack: kube-state-metrics, Prometheus, AlertManager.

Usage:
    uv run deploy-monitoring              # full deploy
    uv run deploy-monitoring --dry-run    # kubectl dry-run only
    uv run deploy-monitoring --skip-ksm   # skip kube-state-metrics

Requires: KinD cluster 'firewatch' + OTel Demo running (SPEC-P1).
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
MONITORING_NS = "monitoring"

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
MONITORING_DIR = INFRA_DIR / "monitoring"

# Ordered manifest files — applied sequentially.
MANIFESTS = [
    "00-namespace.yaml",
    "07-kube-state-metrics.yaml",
    "01-prometheus-rbac.yaml",
    "02-prometheus-config.yaml",
    "03-prometheus-rules.yaml",
    "04-prometheus-deployment.yaml",
    "05-alertmanager-config.yaml",
    "06-alertmanager-deployment.yaml",
]


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, printing the command for transparency."""
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def verify_cluster() -> None:
    """Ensure the KinD cluster is running."""
    result = run(
        ["kubectl", "cluster-info", "--context", CONTEXT],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Cluster '{CLUSTER_NAME}' is not reachable. Run setup_cluster.py first.")
        sys.exit(1)


def verify_otel_demo() -> None:
    """Check that OTel Demo namespace exists (SPEC-P2 depends on SPEC-P1)."""
    result = run(
        ["kubectl", "get", "namespace", "otel-demo", "--context", CONTEXT],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        print("ERROR: 'otel-demo' namespace not found. Run deploy_otel_demo.py first.")
        sys.exit(1)


def detect_webhook_host() -> str:
    """Detect the correct webhook host for AlertManager → agent webhook.

    Per SPEC-P2 §5.2:
      - macOS/Windows (Docker Desktop): host.docker.internal
      - Linux: detect Docker bridge gateway IP via docker network inspect
    """
    os_name = platform.system().lower()

    if os_name in ("darwin", "windows"):
        host = "host.docker.internal"
        print(f"  Detected {platform.system()} — using '{host}'")
        return host

    # Linux: detect Docker bridge gateway IP.
    result = run(
        ["docker", "network", "inspect", "bridge", "--format", "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
        check=False,
        capture=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        host = result.stdout.strip()
        print(f"  Detected Linux — Docker bridge gateway: '{host}'")
        return host

    # Fallback — common Docker bridge IP.
    host = "172.17.0.1"
    print(f"  ⚠️  Could not detect Docker bridge gateway — falling back to '{host}'")
    return host


def patch_alertmanager_config(webhook_host: str, dry_run: bool = False) -> None:
    """Replace the webhook placeholder in the AlertManager ConfigMap with the detected host."""
    config_path = MONITORING_DIR / "05-alertmanager-config.yaml"
    content = config_path.read_text(encoding="utf-8")

    if "AGENT_WEBHOOK_HOST_PLACEHOLDER" not in content:
        print("  AlertManager config already patched.")
        return

    patched = content.replace("AGENT_WEBHOOK_HOST_PLACEHOLDER", webhook_host)

    if dry_run:
        print(f"  [dry-run] Would patch webhook host to '{webhook_host}'")
        return

    config_path.write_text(patched, encoding="utf-8")
    print(f"  ✅ Patched AlertManager webhook host → '{webhook_host}'")


def apply_manifests(dry_run: bool = False, skip_ksm: bool = False) -> None:
    """Apply all monitoring manifests in order."""
    for manifest_file in MANIFESTS:
        if skip_ksm and "kube-state-metrics" in manifest_file:
            print(f"  ⏭️  Skipping {manifest_file} (--skip-ksm)")
            continue

        manifest_path = MONITORING_DIR / manifest_file
        if not manifest_path.exists():
            print(f"  ERROR: Manifest not found: {manifest_path}")
            sys.exit(1)

        cmd = [
            "kubectl", "apply", "-f", str(manifest_path),
            "--context", CONTEXT,
        ]
        if dry_run:
            cmd.extend(["--dry-run=client"])

        run(cmd)


def wait_for_rollout(deployment: str, timeout: str = "180s") -> bool:
    """Wait for a deployment to complete its rollout."""
    result = run(
        [
            "kubectl", "rollout", "status",
            f"deployment/{deployment}",
            "-n", MONITORING_NS,
            f"--timeout={timeout}",
            "--context", CONTEXT,
        ],
        check=False,
    )
    return result.returncode == 0


def verify_monitoring_pods() -> None:
    """Verify all monitoring pods are Running."""
    print("\n🔍 Verifying monitoring pods...")

    deployments = ["kube-state-metrics", "prometheus", "alertmanager"]
    all_healthy = True

    for dep in deployments:
        result = run(
            [
                "kubectl", "get", "deployment", dep,
                "-n", MONITORING_NS,
                "--context", CONTEXT,
                "-o", "jsonpath={.status.availableReplicas}",
            ],
            check=False,
            capture=True,
        )
        replicas = result.stdout.strip()
        if replicas == "1":
            print(f"  ✅ {dep} — 1/1 available")
        else:
            print(f"  ⚠️  {dep} — available replicas: {replicas or '0'}")
            all_healthy = False

    if all_healthy:
        print("\n✅ All monitoring pods healthy.")
    else:
        print("\n⚠️  Some pods are not ready. Check 'kubectl get pods -n monitoring'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P2: Deploy Observability Stack")
    parser.add_argument("--dry-run", action="store_true", help="kubectl dry-run only")
    parser.add_argument("--skip-ksm", action="store_true", help="Skip kube-state-metrics deployment")
    parser.add_argument("--skip-verify", action="store_true", help="Skip post-deploy verification")
    args = parser.parse_args()

    print("🔧 SPEC-P2: Deploying Observability Stack")
    print("=" * 50)

    # Pre-flight checks.
    print("\n📋 Pre-flight checks...")
    verify_cluster()
    verify_otel_demo()

    # Detect and patch webhook host.
    print("\n🔍 Detecting webhook host for AlertManager...")
    webhook_host = detect_webhook_host()
    patch_alertmanager_config(webhook_host, dry_run=args.dry_run)

    # Apply manifests.
    print("\n📦 Applying monitoring manifests...")
    apply_manifests(dry_run=args.dry_run, skip_ksm=args.skip_ksm)

    if args.dry_run:
        print("\n✅ Dry-run complete. No resources were created.")
        return

    # Wait for rollouts.
    print("\n⏳ Waiting for deployments to roll out...")
    deployments = ["kube-state-metrics", "prometheus", "alertmanager"]
    if args.skip_ksm:
        deployments.remove("kube-state-metrics")

    for dep in deployments:
        print(f"  Waiting for {dep}...")
        if not wait_for_rollout(dep):
            print(f"  ⚠️  {dep} did not reach ready state within timeout.")

    # Post-deploy verification.
    if not args.skip_verify:
        verify_monitoring_pods()

    print("\n🎉 SPEC-P2 deployment complete.")
    print("   Prometheus: ClusterIP svc/prometheus:9090")
    print("   AlertManager: ClusterIP svc/alertmanager:9093")
    print("   kube-state-metrics: ClusterIP svc/kube-state-metrics:8080")
    print("\n   Next: run 'uv run start-portforwards' for host access,")
    print("         then 'uv run verify-stack' for full verification.")


if __name__ == "__main__":
    main()
