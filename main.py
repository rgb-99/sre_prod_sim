"""prod_sim — Production simulation environment.

Quick start:
    uv run setup-cluster       # Layer 0: KinD cluster + metrics-server
    uv run deploy-otel         # Layer 1: OTel Demo via Helm
    uv run deploy-monitoring   # Layer 2: Prometheus + AlertManager + kube-state-metrics
    uv run start-portforwards  # Layer 2: Expose Prometheus (9090) & AlertManager (9093)
    uv run deploy-chaos-mesh   # Layer 3: Chaos Mesh fault injection
    uv run start-bridge        # Layer 4: LiveK8sEnv bridge server (port 8002)
    uv run verify-stack        # Run all verification checks (P1 + P2)
    uv run stop-portforwards   # Stop port-forwards
    uv run teardown            # Remove OTel Demo (--full to destroy cluster)
"""
