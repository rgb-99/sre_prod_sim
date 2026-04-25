"""SPEC-P1 Layer 1 — Deploy OpenTelemetry Demo via Helm into the 'otel-demo' namespace.

Usage:
    python -m scripts.deploy_otel_demo           # install/upgrade
    python -m scripts.deploy_otel_demo --dry-run  # Helm dry-run only

Requires: KinD cluster 'firewatch' to be running (run setup_cluster.py first).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
NAMESPACE = "otel-demo"
RELEASE_NAME = "otel-demo"

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
VALUES_FILE = INFRA_DIR / "otel-demo-values.yaml"

# Pinned chart version (matches infra/versions.yaml).
HELM_REPO_NAME = "open-telemetry"
HELM_REPO_URL = "https://open-telemetry.github.io/opentelemetry-helm-charts"
CHART_NAME = f"{HELM_REPO_NAME}/opentelemetry-demo"
CHART_VERSION = "0.32.8"

# Deployment timeout — 600s per SPEC-P1 §5.7 (more images to pull than Boutique).
DEPLOY_TIMEOUT = "600s"


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, printing the command for transparency."""
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def verify_cluster() -> None:
    """Ensure the KinD cluster is running before deploying."""
    result = run(
        ["kubectl", "cluster-info", "--context", CONTEXT],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        print(f"ERROR: Cluster '{CLUSTER_NAME}' is not reachable. Run setup_cluster.py first.")
        sys.exit(1)


def add_helm_repo() -> None:
    """Add the OpenTelemetry Helm repo (idempotent)."""
    print("\n📦 Adding OpenTelemetry Helm repo...")
    run(["helm", "repo", "add", HELM_REPO_NAME, HELM_REPO_URL], check=False)
    run(["helm", "repo", "update"])


def install_otel_demo(dry_run: bool = False) -> None:
    """Install or upgrade the OTel Demo Helm release."""
    if not VALUES_FILE.exists():
        print(f"ERROR: Values file not found at {VALUES_FILE}")
        sys.exit(1)

    print(f"\n🚀 Installing OTel Demo (chart v{CHART_VERSION}) into namespace '{NAMESPACE}'...")

    cmd = [
        "helm", "upgrade", "--install", RELEASE_NAME, CHART_NAME,
        "--version", CHART_VERSION,
        "--namespace", NAMESPACE,
        "--create-namespace",
        "--values", str(VALUES_FILE),
        "--kube-context", CONTEXT,
        "--timeout", DEPLOY_TIMEOUT,
        "--wait",
    ]

    if dry_run:
        cmd.append("--dry-run")
        print("  (dry-run mode — no changes will be applied)")

    run(cmd)

    if dry_run:
        print("\n✅ Dry-run complete. No resources were created.")
        return

    print(f"\n✅ OTel Demo installed in namespace '{NAMESPACE}'.")


def verify_deployment() -> None:
    """Run post-deployment verification checks (SPEC-P1 §5.7)."""
    print("\n🔍 Running post-deployment verification...")

    # Check all deployments are Available.
    print("  Checking all deployments are Available...")
    result = run(
        [
            "kubectl", "wait", "deployment", "--all",
            "--for=condition=Available",
            f"--timeout={DEPLOY_TIMEOUT}",
            "-n", NAMESPACE,
            "--context", CONTEXT,
        ],
        check=False,
    )
    if result.returncode != 0:
        print("⚠️  Some deployments did not reach Available state.")
        print("   This is usually caused by memory pressure on KinD.")
        print("   Try reducing resource requests or allocating more memory to Docker.")
        run(["kubectl", "get", "deployments", "-n", NAMESPACE, "--context", CONTEXT])
        return

    # List all running pods.
    print("\n  Listing pods:")
    run(["kubectl", "get", "pods", "-n", NAMESPACE, "--context", CONTEXT])

    # Check loadgenerator is running.
    print("\n  Checking loadgenerator...")
    result = run(
        [
            "kubectl", "get", "pods", "-n", NAMESPACE,
            "-l", "app.kubernetes.io/component=loadgenerator",
            "-o", "jsonpath={.items[0].status.phase}",
            "--context", CONTEXT,
        ],
        check=False,
        capture=True,
    )
    if result.stdout.strip() == "Running":
        print("  ✅ loadgenerator is Running.")
    else:
        print(f"  ⚠️  loadgenerator phase: {result.stdout.strip()}")

    # Check otel-collector is running.
    print("\n  Checking otel-collector...")
    result = run(
        [
            "kubectl", "get", "pods", "-n", NAMESPACE,
            "-l", "app.kubernetes.io/component=opentelemetry-collector",
            "-o", "jsonpath={.items[0].status.phase}",
            "--context", CONTEXT,
        ],
        check=False,
        capture=True,
    )
    if result.stdout.strip() == "Running":
        print("  ✅ otel-collector is Running.")
    else:
        print(f"  ⚠️  otel-collector phase: {result.stdout.strip()}")

    # Check flagd is running.
    print("\n  Checking flagd...")
    result = run(
        [
            "kubectl", "get", "pods", "-n", NAMESPACE,
            "-l", "app.kubernetes.io/component=flagd",
            "-o", "jsonpath={.items[0].status.phase}",
            "--context", CONTEXT,
        ],
        check=False,
        capture=True,
    )
    if result.stdout.strip() == "Running":
        print("  ✅ flagd is Running.")
    else:
        print(f"  ⚠️  flagd phase: {result.stdout.strip()}")

    print("\n🎉 OTel Demo deployment verification complete.")
    print("   Frontend should be accessible at http://localhost:8080")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P1 Layer 1: Deploy OpenTelemetry Demo")
    parser.add_argument("--dry-run", action="store_true", help="Helm dry-run only, no actual deployment")
    parser.add_argument("--skip-verify", action="store_true", help="Skip post-deployment verification")
    args = parser.parse_args()

    verify_cluster()
    add_helm_repo()
    install_otel_demo(dry_run=args.dry_run)

    if not args.dry_run and not args.skip_verify:
        verify_deployment()


if __name__ == "__main__":
    main()
