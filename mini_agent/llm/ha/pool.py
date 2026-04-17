"""Model node pool — the authoritative list of candidate endpoints."""

from __future__ import annotations

from .models import ModelNode


class ModelPool:
    """Immutable container of model nodes keyed by node_id."""

    def __init__(self, nodes: list[ModelNode]) -> None:
        if not nodes:
            raise ValueError("ModelPool requires at least one node")

        seen: set[str] = set()
        for node in nodes:
            if node.node_id in seen:
                raise ValueError(f"Duplicate node_id in pool: {node.node_id!r}")
            seen.add(node.node_id)

        self._nodes: dict[str, ModelNode] = {n.node_id: n for n in nodes}

    def get(self, node_id: str) -> ModelNode:
        if node_id not in self._nodes:
            raise KeyError(f"Unknown node_id: {node_id!r}")
        return self._nodes[node_id]

    def all(self) -> list[ModelNode]:
        return list(self._nodes.values())

    def enabled(self) -> list[ModelNode]:
        return [n for n in self._nodes.values() if n.enabled]

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return isinstance(node_id, str) and node_id in self._nodes
