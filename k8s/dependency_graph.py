"""Static service dependency graph for the OpenTelemetry Demo application.

This is the single source of truth for service-call topology, used by:
- The bridge's trace_dependencies action (SPEC-P4)
- The dependency_graph field in observation from /reset and /step
- The agent's cascade propagation understanding

Edges are directed: A → B means A calls B.
Per SPEC-P1 §6.3, update this manually if OTel Demo adds/removes services.
"""

from __future__ import annotations

# The 15 application services the agent can target with actions.
APPLICATION_SERVICES: frozenset[str] = frozenset({
    "frontend",
    "frontend-proxy",
    "cart",
    "checkout",
    "currency",
    "email",
    "payment",
    "shipping",
    "quote",
    "ad",
    "recommendation",
    "product-catalog",
    "accounting",
    "fraud-detection",
    "image-provider",
})

# Infrastructure services: visible in observations but NOT targetable by agent actions.
INFRASTRUCTURE_SERVICES: frozenset[str] = frozenset({
    "otel-collector",
    "flagd",
    "valkey-cart",
    "kafka",
    "loadgenerator",
})

ALL_SERVICES: frozenset[str] = APPLICATION_SERVICES | INFRASTRUCTURE_SERVICES

# Directed adjacency list — (caller → callee).
# Infrastructure edges are included so the agent can reason about cache/queue deps.
DEPENDENCY_GRAPH: dict[str, list[str]] = {
    "frontend-proxy": ["frontend"],
    "frontend": [
        "cart",
        "product-catalog",
        "recommendation",
        "checkout",
        "ad",
        "currency",
        "shipping",
        "image-provider",
    ],
    "cart": ["valkey-cart"],
    "checkout": [
        "cart",
        "payment",
        "email",
        "shipping",
        "currency",
        "product-catalog",
        "kafka",
    ],
    "recommendation": ["product-catalog"],
    "shipping": ["quote"],
    "accounting": ["kafka"],
    "fraud-detection": ["kafka"],
}


def get_upstream(service: str) -> list[str]:
    """Return services that call *service* (its upstream callers)."""
    return [caller for caller, callees in DEPENDENCY_GRAPH.items() if service in callees]


def get_downstream(service: str) -> list[str]:
    """Return services that *service* calls (its downstream dependencies)."""
    return list(DEPENDENCY_GRAPH.get(service, []))


def is_targetable(service: str) -> bool:
    """True if the agent can target this service with remediation actions."""
    return service in APPLICATION_SERVICES
