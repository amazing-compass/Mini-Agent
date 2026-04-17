"""Model router — selects a node and orchestrates failover across the pool.

Phase 1 strategy: `priority`. Healthy nodes ordered by descending priority
are tried first; only if all healthy nodes fail do we fall back to
currently-unhealthy nodes (so recovery still happens passively when the
last-resort node starts succeeding again).

The router deliberately does NOT implement in-node retry — that lives in
each provider client via `RetryConfig`. The separation matches doc §5.1
(retry vs failover are two distinct layers).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .errors import ErrorCategory, PoolExhaustedError, classify_error, is_node_switchable
from .health import HealthRegistry
from .models import ModelNode, RoutingDecision
from .pool import ModelPool

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Type aliases for observability callbacks.
OnRequestStart = Callable[[ModelNode, int], None]
OnFailover = Callable[[ModelNode, ModelNode, Exception, ErrorCategory], None]


class ModelRouter:
    """Picks a model node for each request and handles failover."""

    SUPPORTED_STRATEGIES = ("priority",)

    def __init__(
        self,
        pool: ModelPool,
        health_registry: HealthRegistry,
        strategy: str = "priority",
    ) -> None:
        if strategy not in self.SUPPORTED_STRATEGIES:
            raise ValueError(
                f"Unsupported routing strategy {strategy!r}; "
                f"supported: {self.SUPPORTED_STRATEGIES}"
            )
        self.pool = pool
        self.health_registry = health_registry
        self.strategy = strategy

        self.on_request_start: OnRequestStart | None = None
        self.on_failover: OnFailover | None = None

    def select_candidates(self) -> list[ModelNode]:
        """Ordered candidates: healthy-by-priority first, then unhealthy-by-priority.

        Unhealthy nodes stay eligible as a last resort so that:
        - a brief hiccup doesn't permanently sideline a node (Phase 2 will
          formalize this via half-open probes), and
        - if every node is unhealthy, we still try something rather than
          fail outright before any request.
        """
        enabled = self.pool.enabled()

        def sort_key(node: ModelNode) -> tuple[int, int, str]:
            health = self.health_registry.get(node.node_id)
            # Healthy bucket first (0), unhealthy bucket last (1).
            # Higher priority wins within a bucket, so negate.
            return (0 if health.is_healthy else 1, -node.priority, node.node_id)

        return sorted(enabled, key=sort_key)

    async def execute(
        self,
        fn: Callable[[ModelNode], Awaitable[T]],
    ) -> tuple[T, RoutingDecision]:
        """Invoke `fn` on successive candidate nodes until one succeeds.

        `fn` is expected to itself perform node-level retries (we intentionally
        don't duplicate retry here). On exception, the router unwraps
        `RetryExhaustedError`, classifies the underlying error, records
        health, and either fails over or raises immediately for
        non-switchable errors.
        """
        candidates = self.select_candidates()
        if not candidates:
            raise PoolExhaustedError(attempts=[])

        candidate_ids = [n.node_id for n in candidates]
        attempts: list[tuple[str, ErrorCategory, Exception]] = []

        for level, node in enumerate(candidates):
            if self.on_request_start is not None:
                try:
                    self.on_request_start(node, level)
                except Exception:
                    logger.exception("on_request_start callback raised; continuing")

            try:
                result = await fn(node)
            except Exception as exc:
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))

                if not is_node_switchable(category):
                    # Non-switchable errors (REQUEST_MALFORMED, CAPACITY) are
                    # caller-side problems, not node health signals. Skip
                    # record_failure so repeated schema bugs or context
                    # overflows don't poison the node's health and sideline
                    # it from future valid requests.
                    logger.error(
                        "Node %s failed with non-switchable error %s; not failing over: %s",
                        node.node_id,
                        category.value,
                        exc,
                    )
                    raise

                # Switchable error: attributable to this node — record it.
                self.health_registry.get(node.node_id).record_failure(category, exc)

                remaining = candidates[level + 1 :]
                if not remaining:
                    logger.error(
                        "Node %s failed with %s (%s); no more candidates",
                        node.node_id,
                        category.value,
                        exc,
                    )
                    raise PoolExhaustedError(attempts=attempts) from exc

                next_node = remaining[0]
                logger.warning(
                    "Node %s failed with %s (%s); failing over to %s (fallback_level=%d)",
                    node.node_id,
                    category.value,
                    exc,
                    next_node.node_id,
                    level + 1,
                )
                if self.on_failover is not None:
                    try:
                        self.on_failover(node, next_node, exc, category)
                    except Exception:
                        logger.exception("on_failover callback raised; continuing")
                continue

            self.health_registry.get(node.node_id).record_success()
            decision = RoutingDecision(
                selected_node_id=node.node_id,
                candidate_node_ids=candidate_ids,
                fallback_level=level,
                reason=(
                    f"success on node {node.node_id!r} at fallback_level={level}"
                    if level > 0
                    else f"success on primary node {node.node_id!r}"
                ),
            )
            return result, decision

        # Unreachable: the loop either returns or raises on the final attempt.
        raise PoolExhaustedError(attempts=attempts)
