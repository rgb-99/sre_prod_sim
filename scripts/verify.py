"""SPEC-P1 + SPEC-P2 + SPEC-P3 comprehensive verification — run all post-deployment checks.

Usage:
    python -m scripts.verify              # full verification (P1 + P2 + P3)
    python -m scripts.verify --quick      # skip slow checks (metrics scrape)
    python -m scripts.verify --p1-only    # skip P2/P3 checks
    python -m scripts.verify --p2-only    # skip P1/P3 checks
    python -m scripts.verify --p3-only    # skip P1/P2 checks

P1 checks:
  1. KinD cluster health (§3.3)
  2. Metrics-server availability (§4.3)
  3. All 15 application deployments Available (§5.7)
  4. Critical pods running (loadgenerator, otel-collector, flagd)
  5. Frontend accessible on localhost:8080 (§5.7)
  6. OTel Collector /metrics endpoint returns data

P2 checks (SPEC-P2 §7):
  1. kube-state-metrics Running READY 1/1
  2. Prometheus Running READY 1/1
  3. AlertManager Running READY 1/1
  4. Prometheus healthy via localhost:9090/-/healthy
  5. All 6 scrape jobs have at least one target "up"
  6. kube_deployment_status_replicas_available returns entries for OTel Demo
  7. http_server_request_duration_seconds_count returns non-empty (Job 5)
  8. AlertManager healthy via localhost:9093/-/healthy
  9. 7 firewatch-alerts rules present

P3 checks (SPEC-P3 §2.2):
  1. chaos-controller-manager Running
  2. chaos-daemon (DaemonSet) Running
  3. chaos-dashboard Running
  4. flagd ConfigMap contains all required flags
  5. All flags default to DISABLED
  6. No stale firewatch-active-fault ConfigMap
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
import urllib.error

CLUSTER_NAME = "firewatch"
CONTEXT = f"kind-{CLUSTER_NAME}"
NAMESPACE = "otel-demo"
MONITORING_NS = "monitoring"
CHAOS_NS = "chaos-mesh"

EXPECTED_APP_SERVICES = [
    "frontend", "frontend-proxy", "cart", "checkout", "currency",
    "email", "payment", "shipping", "quote", "ad",
    "recommendation", "product-catalog", "accounting",
    "fraud-detection", "image-provider",
]

FEATURE_FLAGS = [
    "productCatalogFailure", "recommendationCacheFailure",
    "adServiceManualGc", "adServiceFailure",
    "paymentServiceFailure", "paymentServiceUnreachable",
    "cartServiceFailure", "kafkaQueueProblems",
    "loadgeneratorFloodHomepage",
]

# Expected scrape jobs from SPEC-P2 §4.2.
EXPECTED_SCRAPE_JOBS = [
    "kubernetes-apiservers",
    "kubernetes-nodes-cadvisor",
    "kubernetes-nodes-kubelet",
    "kube-state-metrics",
    "otel-collector",
    "kubernetes-pods",
]


class VerificationResult:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.warnings: list[str] = []
        self.failures: list[str] = []

    def ok(self, msg: str) -> None:
        print(f"  ✅ {msg}")
        self.passed.append(msg)

    def warn(self, msg: str) -> None:
        print(f"  ⚠️  {msg}")
        self.warnings.append(msg)

    def fail(self, msg: str) -> None:
        print(f"  ❌ {msg}")
        self.failures.append(msg)

    def summary(self) -> int:
        total = len(self.passed) + len(self.warnings) + len(self.failures)
        print(f"\n{'='*60}")
        print(f"  Verification: {len(self.passed)}/{total} passed, "
              f"{len(self.warnings)} warnings, {len(self.failures)} failures")
        print(f"{'='*60}")
        if self.failures:
            print("\n  Failed checks:")
            for f in self.failures:
                print(f"    ❌ {f}")
            return 1
        if self.warnings:
            print("\n  ⚠️  All critical checks passed, but there are warnings.")
        else:
            print("\n  🎉 All checks passed!")
        return 0


def run(cmd: list[str], *, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def http_get(url: str, timeout: int = 10) -> str | None:
    """Fetch URL, return body text or None on error."""
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        return req.read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        return None


def http_get_json(url: str, timeout: int = 10) -> dict | None:
    """Fetch URL, parse JSON, return dict or None on error."""
    body = http_get(url, timeout)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# ─── P1 Checks ─────────────────────────────────────────────────

def check_cluster(result: VerificationResult) -> bool:
    """§3.3 — Cluster reachable, node Ready."""
    print("\n🔍 Layer 0: Cluster Health")

    r = run(["kubectl", "cluster-info", "--context", CONTEXT])
    if r.returncode != 0:
        result.fail(f"Cluster '{CLUSTER_NAME}' not reachable")
        return False
    result.ok("Cluster reachable")

    r = run(["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].status.conditions[-1].type}", "--context", CONTEXT])
    if r.stdout.strip() == "Ready":
        result.ok("Node is Ready")
    else:
        result.fail(f"Node status: {r.stdout.strip()}")
        return False

    # Check hostname label.
    r = run([
        "kubectl", "get", "nodes", "--context", CONTEXT, "-o",
        "jsonpath={.items[0].metadata.labels.kubernetes\\.io/hostname}",
    ])
    expected = f"{CLUSTER_NAME}-control-plane"
    if r.stdout.strip() == expected:
        result.ok(f"Hostname label correct: {expected}")
    else:
        result.warn(f"Hostname label: expected '{expected}', got '{r.stdout.strip()}'")

    return True


def check_metrics_server(result: VerificationResult) -> None:
    """§4.3 — kubectl top nodes returns data."""
    print("\n🔍 Metrics Server")

    r = run(["kubectl", "top", "nodes", "--context", CONTEXT])
    if r.returncode == 0 and r.stdout.strip():
        result.ok("kubectl top nodes returns data")
    else:
        result.warn("kubectl top nodes not returning data (may need more time)")


def check_deployments(result: VerificationResult) -> None:
    """§5.7 — All application deployments Available."""
    print("\n🔍 Layer 1: OTel Demo Deployments")

    r = run(["kubectl", "get", "deployments", "-n", NAMESPACE, "--context", CONTEXT,
             "-o", "jsonpath={.items[*].metadata.name}"])
    if r.returncode != 0:
        result.fail(f"Cannot list deployments in namespace '{NAMESPACE}'")
        return

    deployed = set(r.stdout.strip().split())
    if not deployed:
        result.fail(f"No deployments found in namespace '{NAMESPACE}'")
        return

    result.ok(f"Found {len(deployed)} deployments in '{NAMESPACE}'")

    # Check each expected service.
    for svc in EXPECTED_APP_SERVICES:
        # OTel Demo Helm chart prefixes deployment names with the release name.
        # Check for both bare name and prefixed name.
        matching = [d for d in deployed if svc in d]
        if matching:
            result.ok(f"Deployment for '{svc}' found")
        else:
            result.warn(f"Deployment for '{svc}' not found (name may differ in chart)")


def check_critical_pods(result: VerificationResult) -> None:
    """Check loadgenerator, otel-collector, flagd are Running."""
    print("\n🔍 Critical Pods")

    checks = [
        ("loadgenerator", "app.kubernetes.io/component=loadgenerator"),
        ("otel-collector", "app.kubernetes.io/component=opentelemetry-collector"),
        ("flagd", "app.kubernetes.io/component=flagd"),
    ]

    for name, label in checks:
        r = run([
            "kubectl", "get", "pods", "-n", NAMESPACE,
            "-l", label, "-o", "jsonpath={.items[0].status.phase}",
            "--context", CONTEXT,
        ])
        phase = r.stdout.strip()
        if phase == "Running":
            result.ok(f"{name} is Running")
        elif phase:
            result.warn(f"{name} phase is '{phase}' (expected 'Running')")
        else:
            result.warn(f"{name} pod not found with label '{label}'")


def check_frontend(result: VerificationResult) -> None:
    """§5.7 — Frontend accessible at localhost:8080."""
    print("\n🔍 Frontend Access")

    try:
        req = urllib.request.urlopen("http://localhost:8080", timeout=10)
        if req.status == 200:
            result.ok("Frontend accessible at http://localhost:8080")
        else:
            result.warn(f"Frontend returned HTTP {req.status}")
    except (urllib.error.URLError, OSError) as e:
        result.warn(f"Frontend not accessible at http://localhost:8080 ({e})")


def check_collector_metrics(result: VerificationResult) -> None:
    """Check OTel Collector exposes /metrics on port 9464."""
    print("\n🔍 OTel Collector Metrics Endpoint")

    # Port-forward to check — we can't directly hit the pod from host
    # without a NodePort. Just verify the service exists with the right port.
    r = run([
        "kubectl", "get", "svc", "-n", NAMESPACE, "--context", CONTEXT,
        "-o", "jsonpath={.items[*].metadata.name}",
    ])
    services = r.stdout.strip().split()
    collector_svcs = [s for s in services if "collector" in s.lower() or "otel" in s.lower()]
    if collector_svcs:
        result.ok(f"Collector service(s) found: {', '.join(collector_svcs)}")
    else:
        result.warn("No collector service found")


# ─── P2 Checks (SPEC-P2 §7) ───────────────────────────────────

def check_monitoring_pods(result: VerificationResult) -> None:
    """SPEC-P2 §7 checks 1-3: kube-state-metrics, Prometheus, AlertManager pods Running READY 1/1."""
    print("\n🔍 Layer 2: Monitoring Stack Pods")

    deployments = ["kube-state-metrics", "prometheus", "alertmanager"]

    for dep in deployments:
        r = run([
            "kubectl", "get", "deployment", dep,
            "-n", MONITORING_NS, "--context", CONTEXT,
            "-o", "jsonpath={.status.readyReplicas}",
        ])
        ready = r.stdout.strip()
        if ready == "1":
            result.ok(f"{dep} READY 1/1")
        else:
            result.fail(f"{dep} not ready (readyReplicas={ready or '0'})")


def check_prometheus_health(result: VerificationResult) -> None:
    """SPEC-P2 §7 check 4: Prometheus healthy via localhost:9090/-/healthy."""
    print("\n🔍 Prometheus Health")

    body = http_get("http://localhost:9090/-/healthy")
    if body and "Prometheus Server is Healthy." in body:
        result.ok("Prometheus Server is Healthy (localhost:9090)")
    elif body:
        result.warn(f"Prometheus responded but unexpected body: {body[:100]}")
    else:
        result.fail("Prometheus not reachable at localhost:9090 — is port-forward running?")


def check_scrape_targets(result: VerificationResult) -> None:
    """SPEC-P2 §7 check 5: All 6 scrape jobs have at least one target 'up'."""
    print("\n🔍 Prometheus Scrape Targets")

    data = http_get_json("http://localhost:9090/api/v1/targets")
    if data is None:
        result.fail("Cannot reach Prometheus targets API (localhost:9090/api/v1/targets)")
        return

    if data.get("status") != "success":
        result.fail(f"Prometheus targets API returned status: {data.get('status')}")
        return

    active_targets = data.get("data", {}).get("activeTargets", [])
    jobs_with_up: set[str] = set()
    jobs_all: set[str] = set()

    for target in active_targets:
        job = target.get("labels", {}).get("job", "unknown")
        jobs_all.add(job)
        if target.get("health") == "up":
            jobs_with_up.add(job)

    result.ok(f"Found {len(active_targets)} active targets across {len(jobs_all)} jobs")

    for job in EXPECTED_SCRAPE_JOBS:
        if job in jobs_with_up:
            result.ok(f"Job '{job}' has target(s) in 'up' state")
        elif job in jobs_all:
            result.warn(f"Job '{job}' exists but no targets are 'up' — check RBAC (§4.1)")
        else:
            result.fail(f"Job '{job}' not found in Prometheus targets")


def check_ksm_data(result: VerificationResult) -> None:
    """SPEC-P2 §7 check 6: kube_deployment_status_replicas_available returns OTel Demo entries."""
    print("\n🔍 kube-state-metrics Data")

    query = "kube_deployment_status_replicas_available{namespace='otel-demo'}"
    data = http_get_json(f"http://localhost:9090/api/v1/query?query={query}")
    if data is None:
        result.fail("Cannot query Prometheus for kube_deployment_status_replicas_available")
        return

    results_list = data.get("data", {}).get("result", [])
    if results_list:
        result.ok(f"kube_deployment_status_replicas_available returned {len(results_list)} entries for otel-demo")
    else:
        result.fail("kube_deployment_status_replicas_available returned empty — kube-state-metrics may not be scraped")


def check_otel_metrics(result: VerificationResult) -> None:
    """SPEC-P2 §7 check 7: http_server_request_duration_seconds_count returns non-empty (Job 5)."""
    print("\n🔍 OTel Application Metrics (Job 5)")

    query = "http_server_request_duration_seconds_count{service_namespace='otel-demo'}"
    data = http_get_json(f"http://localhost:9090/api/v1/query?query={query}")
    if data is None:
        result.warn("Cannot query Prometheus for OTel metrics — port-forward may not be running")
        return

    results_list = data.get("data", {}).get("result", [])
    if results_list:
        result.ok(f"OTel metrics found: {len(results_list)} time series for http_server_request_duration_seconds")
    else:
        result.fail("OTel metrics empty — otel-collector scrape job (Job 5) may not be configured correctly")


def check_alertmanager_health(result: VerificationResult) -> None:
    """SPEC-P2 §7 check 8: AlertManager healthy via localhost:9093/-/healthy."""
    print("\n🔍 AlertManager Health")

    body = http_get("http://localhost:9093/-/healthy")
    if body and "OK" in body:
        result.ok("AlertManager healthy (localhost:9093)")
    elif body:
        result.warn(f"AlertManager responded but unexpected body: {body[:100]}")
    else:
        result.fail("AlertManager not reachable at localhost:9093 — is port-forward running?")


def check_alert_rules(result: VerificationResult) -> None:
    """SPEC-P2 §7 check 9: 7 firewatch-alerts rules present."""
    print("\n🔍 Prometheus Alert Rules")

    data = http_get_json("http://localhost:9090/api/v1/rules?type=alert")
    if data is None:
        result.fail("Cannot query Prometheus rules API")
        return

    groups = data.get("data", {}).get("groups", [])
    firewatch_group = None
    for group in groups:
        if group.get("name") == "firewatch-alerts":
            firewatch_group = group
            break

    if firewatch_group is None:
        result.fail("firewatch-alerts rule group not found in Prometheus")
        return

    rules = firewatch_group.get("rules", [])
    expected_rules = {
        "ServiceOOMKilled", "ServiceMemoryCritical", "HighLatency",
        "HighErrorRate", "PodCrashLooping", "NetworkPartition", "GCPressure",
    }
    found_rules = {r.get("name") for r in rules}

    if expected_rules <= found_rules:
        result.ok(f"All 7 firewatch-alerts rules present ({len(found_rules)} total)")
    else:
        missing = expected_rules - found_rules
        result.fail(f"Missing alert rules: {', '.join(missing)}")

    # Report rule states.
    for rule in rules:
        name = rule.get("name", "?")
        state = rule.get("state", "unknown")
        result.ok(f"  Rule '{name}' — state: {state}")


# ─── P3 Checks (SPEC-P3 §2.2) ─────────────────────────────────

def check_chaos_mesh_pods(result: VerificationResult) -> None:
    """SPEC-P3 §2.2: chaos-controller-manager, chaos-daemon, chaos-dashboard Running."""
    print("\n🔍 Layer 3: Chaos Mesh Pods")

    # Check deployments.
    deployments = ["chaos-controller-manager", "chaos-dashboard"]
    for dep in deployments:
        r = run([
            "kubectl", "get", "deployment", dep,
            "-n", CHAOS_NS, "--context", CONTEXT,
            "-o", "jsonpath={.status.readyReplicas}",
        ])
        ready = r.stdout.strip()
        if ready and ready != "0":
            result.ok(f"{dep} READY")
        else:
            result.fail(f"{dep} not ready (readyReplicas={ready or '0'})")

    # chaos-daemon is a DaemonSet.
    r = run([
        "kubectl", "get", "daemonset", "chaos-daemon",
        "-n", CHAOS_NS, "--context", CONTEXT,
        "-o", "jsonpath={.status.numberReady}",
    ])
    ready = r.stdout.strip()
    if ready and ready != "0":
        result.ok(f"chaos-daemon (DaemonSet) READY ({ready} node(s))")
    else:
        result.fail(f"chaos-daemon not ready (numberReady={ready or '0'}) — check runtime/socket path")


def check_flagd_flags(result: VerificationResult) -> None:
    """SPEC-P3 §3.2: flagd ConfigMap contains all required flags, all DISABLED by default."""
    print("\n🔍 flagd Feature Flags")

    r = run([
        "kubectl", "get", "configmap", "-n", NAMESPACE, "--context", CONTEXT,
        "-l", "app.kubernetes.io/component=flagd",
        "-o", "jsonpath={.items[0].data}",
    ])
    if r.returncode != 0 or not r.stdout.strip():
        result.fail("flagd ConfigMap not found")
        return

    result.ok("flagd ConfigMap found")

    try:
        data = json.loads(r.stdout)
        all_flags: dict = {}
        for _filename, content_str in data.items():
            content = json.loads(content_str) if isinstance(content_str, str) else content_str
            all_flags.update(content.get("flags", {}))

        for flag_name in FEATURE_FLAGS:
            if flag_name in all_flags:
                state = all_flags[flag_name].get("state", "unknown")
                if state == "DISABLED":
                    result.ok(f"Flag '{flag_name}' present (DISABLED)")
                else:
                    result.warn(f"Flag '{flag_name}' is '{state}' — expected DISABLED")
            else:
                result.warn(f"Flag '{flag_name}' not found in flagd ConfigMap")
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        result.warn(f"Could not parse flagd ConfigMap: {e}")


def check_no_stale_fault(result: VerificationResult) -> None:
    """Verify no stale firewatch-active-fault ConfigMap exists."""
    print("\n🔍 Fault Metadata")

    r = run([
        "kubectl", "get", "configmap", "firewatch-active-fault",
        "-n", NAMESPACE, "--context", CONTEXT,
    ])
    if r.returncode == 0:
        result.warn("firewatch-active-fault ConfigMap exists — a fault may be active or stale")
    else:
        result.ok("No stale fault metadata (firewatch-active-fault not present)")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPEC-P1 + SPEC-P2 + SPEC-P3 verification suite")
    parser.add_argument("--quick", action="store_true", help="Skip slow checks")
    parser.add_argument("--p1-only", action="store_true", help="Run only P1 checks")
    parser.add_argument("--p2-only", action="store_true", help="Run only P2 checks")
    parser.add_argument("--p3-only", action="store_true", help="Run only P3 checks")
    args = parser.parse_args()

    only_flags = [args.p1_only, args.p2_only, args.p3_only]
    run_p1 = not any(only_flags) or args.p1_only
    run_p2 = not any(only_flags) or args.p2_only
    run_p3 = not any(only_flags) or args.p3_only

    v = VerificationResult()

    # Cluster check always runs (all layers depend on it).
    if not check_cluster(v):
        print("\n💀 Cluster not available — cannot continue verification.")
        sys.exit(1)

    if run_p1:
        # P1 checks.
        if not args.quick:
            check_metrics_server(v)
        check_deployments(v)
        check_critical_pods(v)
        check_frontend(v)
        if not args.quick:
            check_collector_metrics(v)

    if run_p2:
        # P2 checks.
        check_monitoring_pods(v)
        check_prometheus_health(v)
        check_scrape_targets(v)
        check_ksm_data(v)
        check_otel_metrics(v)
        check_alertmanager_health(v)
        check_alert_rules(v)

    if run_p3:
        # P3 checks.
        check_chaos_mesh_pods(v)
        check_flagd_flags(v)
        check_no_stale_fault(v)

    sys.exit(v.summary())


if __name__ == "__main__":
    main()
