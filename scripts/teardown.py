"""SPEC-P1 §7 + SPEC-P2 + SPEC-P3 — Teardown script.

Usage:
    python -m scripts.teardown                    # remove OTel Demo only
    python -m scripts.teardown --monitoring       # remove monitoring stack only
    python -m scripts.teardown --chaos-mesh       # remove Chaos Mesh only
    python -m scripts.teardown --all-apps         # remove OTel Demo + monitoring + Chaos Mesh
    python -m scripts.teardown --full             # destroy entire KinD cluster
    python -m scripts.teardown --confirm          # skip confirmation prompt
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
NAMESPACE = "otel-demo"
MONITORING_NS = "monitoring"
CHAOS_NS = "chaos-mesh"
RELEASE_NAME = "otel-demo"

PID_FILE = Path(__file__).resolve().parent / ".portforward_pids"


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, text=True)


def confirm(msg: str) -> bool:
    resp = input(f"\n⚠️  {msg} [y/N]: ").strip().lower()
    return resp in ("y", "yes")


def stop_portforwards() -> None:
    """Stop any running port-forwards and clean up PID file."""
    if PID_FILE.exists():
        print("  Stopping port-forwards...")
        # Import and reuse the stop logic.
        try:
            from scripts.stop_portforwards import main as stop_main
            stop_main()
        except Exception:
            # Fallback: just remove the PID file.
            PID_FILE.unlink(missing_ok=True)


def teardown_monitoring() -> None:
    """Remove the monitoring stack (SPEC-P2): Prometheus, AlertManager, kube-state-metrics."""
    print("\n🗑️  Removing monitoring stack...")

    stop_portforwards()

    # Delete ClusterRoleBindings and ClusterRoles (not namespace-scoped).
    run(["kubectl", "delete", "clusterrolebinding", "prometheus", "--context", CONTEXT, "--ignore-not-found"], check=False)
    run(["kubectl", "delete", "clusterrole", "prometheus", "--context", CONTEXT, "--ignore-not-found"], check=False)
    run(["kubectl", "delete", "clusterrolebinding", "kube-state-metrics", "--context", CONTEXT, "--ignore-not-found"], check=False)
    run(["kubectl", "delete", "clusterrole", "kube-state-metrics", "--context", CONTEXT, "--ignore-not-found"], check=False)

    # Delete the monitoring namespace (takes all namespace-scoped resources with it).
    run(["kubectl", "delete", "namespace", MONITORING_NS, "--context", CONTEXT, "--ignore-not-found"], check=False)

    print("✅ Monitoring stack removed.")


def teardown_chaos_mesh() -> None:
    """Remove Chaos Mesh (SPEC-P3): cleanup active faults + Helm uninstall."""
    print("\n🗑️  Removing Chaos Mesh...")

    # Clean up any active faults first.
    try:
        from scripts.cleanup_fault import main as cleanup_main
        # Run cleanup in non-interactive mode by monkeypatching input.
        import builtins
        original_input = builtins.input
        builtins.input = lambda *a, **k: "y"
        try:
            cleanup_main()
        except SystemExit:
            pass
        finally:
            builtins.input = original_input
    except Exception:
        pass

    # Helm uninstall.
    run(["helm", "uninstall", "chaos-mesh", "-n", CHAOS_NS, "--kube-context", CONTEXT], check=False)

    # Delete the namespace.
    run(["kubectl", "delete", "namespace", CHAOS_NS, "--context", CONTEXT, "--ignore-not-found"], check=False)

    # Delete cluster-scoped CRDs installed by Chaos Mesh.
    result = subprocess.run(
        ["kubectl", "get", "crds", "-o", "jsonpath={.items[*].metadata.name}",
         "--context", CONTEXT],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        for crd in result.stdout.split():
            if "chaos-mesh" in crd:
                run(["kubectl", "delete", "crd", crd, "--context", CONTEXT], check=False)

    print("✅ Chaos Mesh removed.")


def teardown_otel_demo() -> None:
    """Remove OTel Demo only (Helm uninstall + delete namespace)."""
    print("\n🗑️  Removing OTel Demo...")

    run(["helm", "uninstall", RELEASE_NAME, "-n", NAMESPACE, "--kube-context", CONTEXT], check=False)
    run(["kubectl", "delete", "namespace", NAMESPACE, "--context", CONTEXT, "--ignore-not-found"], check=False)

    print("✅ OTel Demo removed.")
    print("   Cluster and metrics-server remain. Downstream specs (P2-P4) must be re-applied.")


def teardown_full() -> None:
    """Destroy the entire KinD cluster."""
    print(f"\n💀 Destroying KinD cluster '{CLUSTER_NAME}'...")

    stop_portforwards()
    run(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=False)

    print(f"✅ Cluster '{CLUSTER_NAME}' destroyed.")
    print("   All data, installed components, and kubeconfig context removed.")
    print("   Downstream specs (P2-P4) must be re-applied from scratch.")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P1/P2 Teardown")
    parser.add_argument("--full", action="store_true", help="Destroy entire KinD cluster (not just apps)")
    parser.add_argument("--monitoring", action="store_true", help="Remove monitoring stack only (SPEC-P2)")
    parser.add_argument("--chaos-mesh", action="store_true", help="Remove Chaos Mesh only (SPEC-P3)")
    parser.add_argument("--all-apps", action="store_true", help="Remove OTel Demo + monitoring + Chaos Mesh")
    parser.add_argument("--confirm", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    if args.full:
        if args.confirm or confirm(f"This will DESTROY the entire '{CLUSTER_NAME}' cluster. Continue?"):
            teardown_full()
        else:
            print("Aborted.")
            sys.exit(0)
    elif args.monitoring:
        if args.confirm or confirm("This will remove the monitoring stack (Prometheus, AlertManager, kube-state-metrics). Continue?"):
            teardown_monitoring()
        else:
            print("Aborted.")
            sys.exit(0)
    elif args.chaos_mesh:
        if args.confirm or confirm("This will remove Chaos Mesh. Continue?"):
            teardown_chaos_mesh()
        else:
            print("Aborted.")
            sys.exit(0)
    elif args.all_apps:
        if args.confirm or confirm("This will remove OTel Demo, monitoring stack, AND Chaos Mesh. Continue?"):
            teardown_chaos_mesh()
            teardown_monitoring()
            teardown_otel_demo()
        else:
            print("Aborted.")
            sys.exit(0)
    else:
        if args.confirm or confirm("This will remove the OTel Demo. Continue?"):
            teardown_otel_demo()
        else:
            print("Aborted.")
            sys.exit(0)


if __name__ == "__main__":
    main()
