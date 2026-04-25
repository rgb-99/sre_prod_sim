"""SPEC-P4 §10.3 — Dependency graph traversal for trace_dependencies.

Re-exports the canonical graph from k8s.dependency_graph and adds BFS
traversal up to 3 hops for upstream/downstream discovery.
"""

from __future__ import annotations

from collections import deque

from k8s.dependency_graph import (
    APPLICATION_SERVICES,
    DEPENDENCY_GRAPH,
    INFRASTRUCTURE_SERVICES,
)


def bfs_upstream(service: str, max_hops: int = 3) -> list[str]:
    """All callers of *service* within *max_hops* via BFS."""
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque()

    # Seed: find all direct callers
    for caller, callees in DEPENDENCY_GRAPH.items():
        if service in callees and caller != service:
            queue.append((caller, 1))

    while queue:
        node, depth = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        if depth < max_hops:
            for caller, callees in DEPENDENCY_GRAPH.items():
                if node in callees and caller not in visited:
                    queue.append((caller, depth + 1))

    return sorted(visited)


def bfs_downstream(service: str, max_hops: int = 3) -> list[str]:
    """All dependencies of *service* within *max_hops* via BFS."""
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque()

    for dep in DEPENDENCY_GRAPH.get(service, []):
        if dep != service:
            queue.append((dep, 1))

    while queue:
        node, depth = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        if depth < max_hops:
            for dep in DEPENDENCY_GRAPH.get(node, []):
                if dep not in visited:
                    queue.append((dep, depth + 1))

    return sorted(visited)
