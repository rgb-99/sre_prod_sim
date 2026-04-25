"""Shared fault injection utilities for SPEC-P3 v2.

Every injection script must call precondition_check() before injecting,
and write_fault_metadata() after injection succeeds. The cleanup_fault()
function is the canonical cleanup path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
NAMESPACE = "otel-demo"
CHAOS_NS = "chaos-mesh"

FAULT_METADATA_CM = "firewatch-active-fault"

# The 5 training archetypes.
VALID_ARCHETYPES = {"oom", "memory_leak", "bad_deploy", "config_drift", "network_partition"}

# 15 application services from SPEC-P1 §5.4.
APPLICATION_SERVICES = [
    "frontend", "frontend-proxy", "cart", "checkout", "currency",
    "email", "payment", "shipping", "quote", "ad",
    "recommendation", "product-catalog", "accounting",
    "fraud-detection", "image-provider",
]

# Infrastructure services that MUST NOT be targeted by Chaos Mesh (SPEC-P3 §4).
INFRA_SERVICES = ["otel-collector", "flagd", "valkey-cart", "kafka", "loadgenerator"]

# flagd flags from SPEC-P3 §3.2.
FLAGD_FLAGS = [
    "productCatalogFailure", "recommendationCacheFailure",
    "adServiceManualGc", "adServiceFailure",
    "paymentServiceFailure", "paymentServiceUnreachable",
    "cartServiceFailure", "kafkaQueueProblems",
]


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    if not quiet:
        print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def has_active_chaos_experiments() -> bool:
    """Check if any Chaos Mesh experiments are currently active in otel-demo."""
    for kind in ("podchaos", "networkchaos", "stresschaos"):
        result = run(
            ["kubectl", "get", kind, "-n", NAMESPACE, "--context", CONTEXT,
             "-o", "jsonpath={.items[*].metadata.name}"],
            check=False, capture=True, quiet=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            print(f"  ❌ Active {kind} found: {result.stdout.strip()}")
            return True
    return False


def has_enabled_flagd_flags() -> bool:
    """Check if any flagd flags are currently ENABLED."""
    result = run(
        ["kubectl", "get", "configmap", "-n", NAMESPACE, "--context", CONTEXT,
         "-l", "app.kubernetes.io/component=flagd",
         "-o", "jsonpath={.items[0].data}"],
        check=False, capture=True, quiet=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False

    # The flagd ConfigMap data contains a JSON blob under a key (usually demo.flagd.json).
    # Parse it to check flag states.
    try:
        data = json.loads(result.stdout)
        # data is a dict of filename -> JSON string.
        for _filename, content_str in data.items():
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
            flags = content.get("flags", {})
            for flag_name, flag_def in flags.items():
                if flag_name in FLAGD_FLAGS and flag_def.get("state") == "ENABLED":
                    print(f"  ❌ flagd flag '{flag_name}' is ENABLED")
                    return True
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return False


def has_fault_metadata_cm() -> bool:
    """Check if the firewatch-active-fault ConfigMap exists."""
    result = run(
        ["kubectl", "get", "configmap", FAULT_METADATA_CM, "-n", NAMESPACE,
         "--context", CONTEXT],
        check=False, capture=True, quiet=True,
    )
    return result.returncode == 0


def precondition_check() -> bool:
    """SPEC-P3 §6.2 step 1: Verify no other fault is currently active.

    Returns True if all clear, False if a fault is active.
    """
    print("\n📋 Precondition check — verifying no active faults...")

    if has_fault_metadata_cm():
        print("  ❌ firewatch-active-fault ConfigMap exists — another fault is active.")
        print("     Run 'uv run cleanup-fault' first.")
        return False

    if has_active_chaos_experiments():
        print("  ❌ Active Chaos Mesh experiments detected.")
        print("     Run 'uv run cleanup-fault' first.")
        return False

    if has_enabled_flagd_flags():
        print("  ❌ One or more flagd flags are ENABLED.")
        print("     Run 'uv run cleanup-fault' first.")
        return False

    print("  ✅ No active faults. Clear to inject.")
    return True


def write_fault_metadata(
    archetype: str,
    target_service: str,
    injection_mechanism: str,
) -> None:
    """SPEC-P3 §6.2 step 3: Create firewatch-active-fault ConfigMap."""
    if archetype not in VALID_ARCHETYPES:
        raise ValueError(f"Invalid archetype '{archetype}'. Must be one of: {VALID_ARCHETYPES}")

    now = datetime.now(timezone.utc).isoformat()

    run([
        "kubectl", "create", "configmap", FAULT_METADATA_CM,
        "-n", NAMESPACE, "--context", CONTEXT,
        f"--from-literal=archetype={archetype}",
        f"--from-literal=target_service={target_service}",
        f"--from-literal=injection_mechanism={injection_mechanism}",
        f"--from-literal=injected_at={now}",
    ])
    print(f"  ✅ Fault metadata written: {archetype} → {target_service} via {injection_mechanism}")


def get_container_memory_limit(service: str) -> str | None:
    """Read the memory limit from a service's deployment spec.

    Returns the limit string (e.g., '250Mi') or None if not set.
    SPEC-P3 §5.1 requires computing stress size from this at injection time.
    """
    # OTel Demo Helm chart prefixes deployment names with the release name.
    result = run(
        ["kubectl", "get", "deployment", "-n", NAMESPACE, "--context", CONTEXT,
         "-l", f"app.kubernetes.io/name={service}",
         "-o", "jsonpath={.items[0].spec.template.spec.containers[0].resources.limits.memory}"],
        check=False, capture=True, quiet=True,
    )
    limit = result.stdout.strip()
    return limit if limit else None


def parse_memory_to_bytes(mem_str: str) -> int:
    """Convert Kubernetes memory string to bytes (e.g., '250Mi' → 262144000)."""
    mem_str = mem_str.strip()
    units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
        if mem_str.endswith(suffix):
            return int(mem_str[: -len(suffix)]) * multiplier
    return int(mem_str)


def toggle_flagd_flag(flag_name: str, state: str = "ENABLED") -> bool:
    """Toggle a flagd flag via ConfigMap patch (SPEC-P3 §3.3).

    Uses kubectl to patch the flagd ConfigMap's JSON data in-place.
    Returns True on success.
    """
    if flag_name not in FLAGD_FLAGS:
        print(f"  ❌ Unknown flagd flag: '{flag_name}'")
        return False

    if state not in ("ENABLED", "DISABLED"):
        print(f"  ❌ Invalid state: '{state}'. Must be 'ENABLED' or 'DISABLED'.")
        return False

    # Find the flagd ConfigMap name.
    result = run(
        ["kubectl", "get", "configmap", "-n", NAMESPACE, "--context", CONTEXT,
         "-l", "app.kubernetes.io/component=flagd",
         "-o", "jsonpath={.items[0].metadata.name}"],
        check=False, capture=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("  ❌ Could not find flagd ConfigMap.")
        return False

    cm_name = result.stdout.strip()

    # Get the current ConfigMap data.
    result = run(
        ["kubectl", "get", "configmap", cm_name, "-n", NAMESPACE, "--context", CONTEXT,
         "-o", "json"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        print(f"  ❌ Could not read ConfigMap '{cm_name}'.")
        return False

    cm = json.loads(result.stdout)
    data = cm.get("data", {})

    # The flagd ConfigMap has a single key containing the flag definitions JSON.
    patched = False
    for key, value_str in data.items():
        try:
            value = json.loads(value_str)
        except (json.JSONDecodeError, TypeError):
            continue

        flags = value.get("flags", {})
        if flag_name in flags:
            flags[flag_name]["state"] = state
            data[key] = json.dumps(value, indent=2)
            patched = True
            break

    if not patched:
        print(f"  ❌ Flag '{flag_name}' not found in ConfigMap '{cm_name}'.")
        return False

    # Write back the patched ConfigMap.
    cm["data"] = data
    # Remove resourceVersion to avoid conflict — kubectl replace handles this.
    cm.get("metadata", {}).pop("resourceVersion", None)
    cm.get("metadata", {}).pop("uid", None)
    cm.get("metadata", {}).pop("creationTimestamp", None)
    cm.get("metadata", {}).pop("managedFields", None)

    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(cm, f)
        tmp_path = f.name

    try:
        run([
            "kubectl", "apply", "-f", tmp_path,
            "-n", NAMESPACE, "--context", CONTEXT,
        ])
        print(f"  ✅ flagd flag '{flag_name}' → {state}")
        return True
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def reset_all_flagd_flags() -> bool:
    """Reset all known flagd flags to DISABLED."""
    print("\n🔄 Resetting all flagd flags to DISABLED...")
    all_ok = True
    for flag in FLAGD_FLAGS:
        if not toggle_flagd_flag(flag, "DISABLED"):
            all_ok = False
    return all_ok


def delete_chaos_experiments() -> None:
    """Delete all Chaos Mesh CRDs in the otel-demo namespace."""
    print("\n🗑️  Deleting Chaos Mesh experiments in otel-demo...")
    for kind in ("podchaos", "networkchaos", "stresschaos"):
        run(
            ["kubectl", "delete", kind, "--all", "-n", NAMESPACE,
             "--context", CONTEXT, "--ignore-not-found"],
            check=False,
        )


def delete_fault_metadata() -> None:
    """Delete the firewatch-active-fault ConfigMap."""
    run(
        ["kubectl", "delete", "configmap", FAULT_METADATA_CM,
         "-n", NAMESPACE, "--context", CONTEXT, "--ignore-not-found"],
        check=False,
    )


def wait_for_services_available(timeout_seconds: int = 120) -> bool:
    """SPEC-P3 §6.3 step 4: Wait for all 15 application services to be Available."""
    print(f"\n⏳ Waiting for all application services to become Available (timeout {timeout_seconds}s)...")

    result = run(
        [
            "kubectl", "wait", "deployment", "--all",
            "--for=condition=Available",
            f"--timeout={timeout_seconds}s",
            "-n", NAMESPACE,
            "--context", CONTEXT,
        ],
        check=False,
    )
    if result.returncode == 0:
        print("  ✅ All deployments Available.")
        return True
    else:
        print("  ⚠️  Some deployments did not reach Available state within timeout.")
        print("     The next experiment may run on a degraded cluster.")
        return False


def apply_chaos_crd(crd_yaml: str) -> bool:
    """Apply a Chaos Mesh CRD from a YAML string."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(crd_yaml)
        tmp_path = f.name

    try:
        result = run(
            ["kubectl", "apply", "-f", tmp_path, "--context", CONTEXT],
            check=False,
        )
        return result.returncode == 0
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def print_summary(
    archetype: str,
    target_service: str,
    mechanism: str,
    expected_alerts: list[str],
) -> None:
    """SPEC-P3 §6.2 step 4: Print injection summary."""
    print(f"\n{'='*60}")
    print(f"  🎯 FAULT INJECTED")
    print(f"     Archetype:       {archetype}")
    print(f"     Target:          {target_service}")
    print(f"     Mechanism:       {mechanism}")
    print(f"     Expected alerts: {', '.join(expected_alerts)}")
    print(f"{'='*60}")
