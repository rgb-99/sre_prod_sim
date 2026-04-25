"""SPEC-P3 §5.3 — inject_bad_deploy_payment: Bad deploy via flagd paymentServiceFailure.

Usage:
    uv run inject-bad-deploy-payment
    uv run inject-bad-deploy-payment --service product-catalog
    uv run inject-bad-deploy-payment --service cart
    uv run inject-bad-deploy-payment --service ad
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from infra.fault_scenarios.common import (
    CONTEXT,
    NAMESPACE,
    precondition_check,
    print_summary,
    run,
    toggle_flagd_flag,
    write_fault_metadata,
)

# Service → flagd flag mapping (SPEC-P3 §3.2 + §5.3).
SERVICE_FLAG_MAP = {
    "product-catalog": "productCatalogFailure",
    "payment": "paymentServiceFailure",
    "cart": "cartServiceFailure",
    "ad": "adServiceFailure",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P3 §5.3: Inject bad_deploy fault")
    parser.add_argument(
        "--service", default="payment",
        choices=list(SERVICE_FLAG_MAP.keys()),
        help="Target service (default: payment)",
    )
    args = parser.parse_args()

    service = args.service
    flag_name = SERVICE_FLAG_MAP[service]

    print(f"🔧 SPEC-P3: Injecting bad_deploy → {service}")
    print("=" * 50)

    if not precondition_check():
        sys.exit(1)

    # SPEC-P3 §5.3: Perform a no-op rollout restart to reset deployment age.
    # This makes last_deployment_age_seconds < 300 — matching training env behavior.
    print(f"\n🔄 Performing rollout restart on {service} (reset deployment age)...")
    # Find the actual deployment name (OTel Demo prefixes with release name).
    result = run(
        ["kubectl", "get", "deployment", "-n", NAMESPACE, "--context", CONTEXT,
         "-l", f"app.kubernetes.io/name={service}",
         "-o", "jsonpath={.items[0].metadata.name}"],
        check=False, capture=True,
    )
    dep_name = result.stdout.strip()
    if dep_name:
        run(
            ["kubectl", "rollout", "restart", f"deployment/{dep_name}",
             "-n", NAMESPACE, "--context", CONTEXT],
            check=False,
        )
        print(f"  ✅ Rollout restart issued for {dep_name}")
    else:
        print(f"  ⚠️  Could not find deployment for '{service}' — skipping rollout restart")

    # Toggle the flagd flag (SPEC-P3 §5.3 Path A).
    print(f"\n🚀 Enabling {flag_name} flag...")
    if not toggle_flagd_flag(flag_name, "ENABLED"):
        print(f"❌ Failed to enable {flag_name} flag.")
        sys.exit(1)

    write_fault_metadata("bad_deploy", service, "flagd")

    print_summary(
        archetype="bad_deploy",
        target_service=service,
        mechanism=f"flagd {flag_name}=ENABLED + rollout restart",
        expected_alerts=["HighErrorRate"],
    )
    print("\n     Run 'uv run cleanup-fault' to halt the fault.")


if __name__ == "__main__":
    main()
