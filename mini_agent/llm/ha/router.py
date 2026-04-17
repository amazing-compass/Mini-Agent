"""Model router — selects a node, failover-orchestrates, and enforces the
design's responsibility separation between agent (compression) and
router (request-level budgeting + breaker).

Phase 2 adds:
- `call(messages, tools)` — full-featured business path: three-bucket
  `(healthy × fits)` classification, priority ordering, on_attempt
  transition, record_success/record_failure accounting, event-time
  ContextOverflow fallthrough.
- `internal_call(messages, tools)` — strict read-only bypass for the
  agent's L4 summary: picks the highest-priority **serving** node
  (closed / half-open), no on_attempt, no record_*, no failover, no
  fits pre-check. Failures bubble up so the agent can degrade to "no
  summary" without poisoning health.
- `execute(fn)` (Phase 1 API) is preserved for backward compatibility
  with `LLMClient`.

The router never compresses `messages`. ContextOverflowError is always
raised back to the agent — whether it came from the pre-flight bucket
classification ("healthy but doesn't fit") or from provider response
("estimator under-counted"). The agent owns the L1/L2/L4 retry loop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..base import LLMClientBase
from .budget import TokenBudget
from .errors import (
    AllNodesFailedError,
    AuthError,
    BadRequestError,
    ContextOverflowError,
    ErrorCategory,
    LLMError,
    NoAvailableNodeError,
    NodeUnavailableError,
    PoolExhaustedError,
    RateLimitError,
    TransientError,
    classify_error,
    is_node_switchable,
)
from .health import HealthRegistry, SimpleBreaker
from .models import ModelNode, RoutingDecision
from .pool import ModelPool

logger = logging.getLogger(__name__)

T = TypeVar("T")

OnRequestStart = Callable[[ModelNode, int], None]
OnFailover = Callable[[ModelNode, ModelNode, Exception, ErrorCategory], None]

# Pre-flight fits uses this as the minimum output headroom — a node that
# can only output 500 tokens "fits" numerically but isn't useful. Design §7.4.
MIN_USEFUL_OUTPUT = 2048
SAFETY_MARGIN = 1024


class ModelRouter:
    """Picks a model node for each request and handles failover."""

    SUPPORTED_STRATEGIES = ("priority",)

    def __init__(
        self,
        pool: ModelPool,
        health_registry: HealthRegistry | SimpleBreaker,
        strategy: str = "priority",
    ) -> None:
        if strategy not in self.SUPPORTED_STRATEGIES:
            raise ValueError(
                f"Unsupported routing strategy {strategy!r}; "
                f"supported: {self.SUPPORTED_STRATEGIES}"
            )
        self.pool = pool
        # `health_registry` is the Phase 1 name; Phase 2 treats it as a
        # SimpleBreaker (HealthRegistry is a subclass, so either works).
        self.breaker: SimpleBreaker = health_registry
        # Keep the old attribute name reachable so callers written against
        # Phase 1 still work.
        self.health_registry = health_registry
        self.strategy = strategy

        self.on_request_start: OnRequestStart | None = None
        self.on_failover: OnFailover | None = None

        self.last_routing_decision: RoutingDecision | None = None

    # ------------------------------------------------------------------
    # Phase 2 primary API: .call / .internal_call
    # ------------------------------------------------------------------

    async def call(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
    ) -> Any:
        """Business-path request with full breaker + bucket routing.

        Returns the `LLMResponse` from the first successful node. Raises:
        - `NoAvailableNodeError` — pool is empty / all disabled
        - `ContextOverflowError` — at least one healthy node exists, but
          none can fit `messages` (pre-flight path); OR a node raised it
          at event time. Agent must compress and retry.
        - `AllNodesFailedError` — every healthy+fitting candidate failed
          with a switchable error; there is nothing left to try.
        - `BadRequestError` — a client raised it; it's a program bug,
          don't failover, don't compress.
        """
        all_nodes = self.pool.enabled()
        if not all_nodes:
            raise NoAvailableNodeError("node pool is empty or all disabled")

        # ---- Step 1: three-bucket classification ----------------------
        healthy_and_fits: list[ModelNode] = []
        healthy_no_fit: list[ModelNode] = []
        unhealthy: list[ModelNode] = []

        for node in all_nodes:
            passable = self.breaker.is_passable(node.node_id)
            # The pre-flight check must stay consistent with the real
            # request the main loop will issue below — otherwise a node
            # configured with `max_output_tokens < MIN_USEFUL_OUTPUT`
            # would be rejected up front even though the request could
            # actually succeed. Use the smaller of the two as the
            # headroom we insist on.
            fit_output_budget = min(MIN_USEFUL_OUTPUT, node.max_output_tokens)
            fits = TokenBudget.fits(
                messages,
                tools,
                node,
                expected_output=fit_output_budget,
                safety_margin=SAFETY_MARGIN,
            )
            if passable and fits:
                healthy_and_fits.append(node)
            elif passable:
                healthy_no_fit.append(node)
            else:
                unhealthy.append(node)

        # ---- Step 2: nothing fits-and-healthy — pick an error ---------
        if not healthy_and_fits:
            if healthy_no_fit:
                # Agent's responsibility — compress and retry.
                raise ContextOverflowError(
                    "no healthy node fits current messages; agent should compress"
                )
            # Every node is circuit-broken. Compression can't rescue this.
            raise AllNodesFailedError(
                message=f"all {len(all_nodes)} candidate node(s) are currently unhealthy",
                attempts=[],
            )

        # ---- Step 3: main loop — priority ordering --------------------
        ordered = sorted(
            healthy_and_fits,
            key=lambda n: (-n.priority, n.node_id),
        )
        candidate_ids = [n.node_id for n in ordered]
        attempts: list[tuple[str, ErrorCategory, Exception]] = []

        for level, node in enumerate(ordered):
            if self.on_request_start is not None:
                try:
                    self.on_request_start(node, level)
                except Exception:
                    logger.exception("on_request_start callback raised; continuing")

            # Compute the real per-request max_tokens budget.
            estimate = TokenBudget.estimate(messages, tools)
            available = node.context_window - estimate - SAFETY_MARGIN
            actual_max_tokens = max(1, min(node.max_output_tokens, available))

            client = self.pool.get_client(node.node_id)

            # Transition open+cooldown-elapsed → half-open *right* before
            # the request fires. This is the one place that mutates state.
            self.breaker.on_attempt(node.node_id)

            try:
                response = await client.generate(
                    messages,
                    tools,
                    max_tokens=actual_max_tokens,
                )
            except ContextOverflowError:
                # Event-time capacity: provider told us we were wrong
                # about fits. Don't switch nodes, don't compress — that's
                # the agent's job. Don't record_failure (the node is
                # fine; our estimator wasn't).
                raise
            except BadRequestError:
                # Program bug — do not mask by trying the next node. Do
                # not record_failure (it's not a node problem).
                raise
            except (TransientError, RateLimitError, AuthError, NodeUnavailableError) as exc:
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))
                self.breaker.record_failure(node.node_id, category, exc)
                self._notify_failover(ordered, level, exc, category)
                continue
            except LLMError as exc:
                # Other LLMError subclasses we didn't anticipate: treat
                # as switchable transient-ish (stay on the safe side).
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))
                self.breaker.record_failure(node.node_id, category, exc)
                self._notify_failover(ordered, level, exc, category)
                continue
            except Exception as exc:
                # SDK-native exception that escaped normalization — classify
                # via the duck-typed fallback and decide whether to switch.
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))
                if not is_node_switchable(category):
                    # A capacity error that skipped our normalizer still
                    # belongs to the agent; re-raise.
                    if category == ErrorCategory.CAPACITY:
                        raise ContextOverflowError(str(exc)) from exc
                    raise
                self.breaker.record_failure(node.node_id, category, exc)
                self._notify_failover(ordered, level, exc, category)
                continue

            # Success.
            self.breaker.record_success(node.node_id)
            self.last_routing_decision = RoutingDecision(
                selected_node_id=node.node_id,
                candidate_node_ids=candidate_ids,
                fallback_level=level,
                reason=(
                    f"success on node {node.node_id!r} at fallback_level={level}"
                    if level > 0
                    else f"success on primary node {node.node_id!r}"
                ),
            )
            return response

        # Every healthy-and-fits candidate failed with switchable errors.
        raise AllNodesFailedError(attempts=attempts)

    async def internal_call(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
    ) -> Any:
        """Read-only bypass for the agent's L4 summary.

        Strict semantics (design §5.9):
        - filter by `is_serving()` — open nodes (including cooldown-elapsed)
          are skipped; the probe slot belongs to the business main loop
        - no `on_attempt()` — we never push the state machine
        - no `record_success` / `record_failure` — summary outcomes don't
          represent business-node health
        - no failover — a summary that can't land simply fails
        - no fits pre-check, no ContextOverflow fallback
        """
        candidates = [n for n in self.pool.enabled() if self.breaker.is_serving(n.node_id)]
        if not candidates:
            raise NoAvailableNodeError("no serving node available for internal call")

        node = max(candidates, key=lambda n: (n.priority, -ord(n.node_id[0]) if n.node_id else 0))
        # Break ties deterministically on node_id ascending.
        top_priority = node.priority
        tied = [n for n in candidates if n.priority == top_priority]
        if len(tied) > 1:
            node = min(tied, key=lambda n: n.node_id)

        client = self.pool.get_client(node.node_id)
        # Use the node's declared max_output_tokens as the summary budget;
        # the L4 prompt is modest and compression summaries are short.
        return await client.generate(
            messages,
            tools,
            max_tokens=node.max_output_tokens,
        )

    # ------------------------------------------------------------------
    # Phase 1 compatibility surface: .select_candidates / .execute
    # ------------------------------------------------------------------

    def select_candidates(self) -> list[ModelNode]:
        """Phase 1 API — healthy-by-priority first, unhealthy-by-priority last.

        Kept because `LLMClient.generate` (the pre-Phase-2 facade) still
        drives its own failover via this ordering. New code should use
        `.call()` which does the three-bucket classification itself.
        """
        enabled = self.pool.enabled()

        def sort_key(node: ModelNode) -> tuple[int, int, str]:
            passable = self.breaker.is_passable(node.node_id)
            return (0 if passable else 1, -node.priority, node.node_id)

        return sorted(enabled, key=sort_key)

    async def execute(
        self,
        fn: Callable[[ModelNode], Awaitable[T]],
    ) -> tuple[T, RoutingDecision]:
        """Phase 1 API — invoke `fn` on successive candidates until one succeeds.

        Preserved for `LLMClient.generate` (which we keep in Phase 2 so
        ACP doesn't break). The bucket-aware path is `.call()`.
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

            # Drive the breaker — on_attempt / record_success / record_failure
            # so Phase 1 call sites get the new 3-state semantics for free.
            self.breaker.on_attempt(node.node_id)

            try:
                result = await fn(node)
            except Exception as exc:
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))

                if not is_node_switchable(category):
                    # Program bug or capacity problem — never masked by
                    # failover, never charged to node health.
                    logger.error(
                        "Node %s failed with non-switchable error %s; not failing over: %s",
                        node.node_id,
                        category.value,
                        exc,
                    )
                    raise

                self.breaker.record_failure(node.node_id, category, exc)

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

            self.breaker.record_success(node.node_id)
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
            self.last_routing_decision = decision
            return result, decision

        raise PoolExhaustedError(attempts=attempts)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _notify_failover(
        self,
        ordered: list[ModelNode],
        failed_level: int,
        exc: Exception,
        category: ErrorCategory,
    ) -> None:
        if self.on_failover is None:
            return
        remaining = ordered[failed_level + 1 :]
        if not remaining:
            return
        try:
            self.on_failover(ordered[failed_level], remaining[0], exc, category)
        except Exception:
            logger.exception("on_failover callback raised; continuing")
