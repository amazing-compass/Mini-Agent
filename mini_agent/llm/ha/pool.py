"""Model node pool — the authoritative list of candidate endpoints.

Phase 2 extends the Phase 1 ModelPool with a per-node `LLMClientBase`
cache. Clients are built eagerly (at construction time) when the pool
owner supplies a `build_client` factory, which matches the design §4.3
decision to avoid lazy creation for predictable startup failures. For
back-compat the old lazy-only construction still works — callers may
register clients later via `set_client`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .models import ModelNode

if TYPE_CHECKING:
    from ..base import LLMClientBase


class ModelPool:
    """Immutable container of model nodes keyed by node_id."""

    def __init__(
        self,
        nodes: list[ModelNode],
        build_client: Callable[[ModelNode], "LLMClientBase"] | None = None,
    ) -> None:
        if not nodes:
            raise ValueError("ModelPool requires at least one node")

        seen: set[str] = set()
        for node in nodes:
            if node.node_id in seen:
                raise ValueError(f"Duplicate node_id in pool: {node.node_id!r}")
            seen.add(node.node_id)

        self._nodes: dict[str, ModelNode] = {n.node_id: n for n in nodes}
        self._clients: dict[str, LLMClientBase] = {}

        if build_client is not None:
            for node in nodes:
                if not node.enabled:
                    continue
                self._clients[node.node_id] = build_client(node)

    def get(self, node_id: str) -> ModelNode:
        if node_id not in self._nodes:
            raise KeyError(f"Unknown node_id: {node_id!r}")
        return self._nodes[node_id]

    def all(self) -> list[ModelNode]:
        return list(self._nodes.values())

    def enabled(self) -> list[ModelNode]:
        return [n for n in self._nodes.values() if n.enabled]

    def all_context_windows(self) -> list[int]:
        """Enabled nodes' declared context windows — used by cli.py at
        startup to derive the agent's `token_limit` (min × 0.8)."""
        return [n.context_window for n in self._nodes.values() if n.enabled]

    # ---- client management ------------------------------------------------

    def set_client(self, node_id: str, client: "LLMClientBase") -> None:
        """Register or replace the client for a node. Useful when a caller
        wants to inject a mock for testing, or to defer client creation
        when `build_client` wasn't available at pool-construction time."""
        if node_id not in self._nodes:
            raise KeyError(f"Unknown node_id: {node_id!r}")
        self._clients[node_id] = client

    def get_client(self, node_id: str) -> "LLMClientBase":
        if node_id not in self._nodes:
            raise KeyError(f"Unknown node_id: {node_id!r}")
        client = self._clients.get(node_id)
        if client is None:
            raise RuntimeError(
                f"No client registered for node {node_id!r}; "
                "did you pass `build_client` to ModelPool or call set_client()?"
            )
        return client

    def has_client(self, node_id: str) -> bool:
        return node_id in self._clients

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return isinstance(node_id, str) and node_id in self._nodes
