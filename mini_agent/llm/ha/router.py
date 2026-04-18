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
- `execute(fn)` (Phase 1 API) is preserved for callers that need to
  drive custom per-node lambdas directly (tests, ad-hoc scripts).

The router never compresses `messages`. ContextOverflowError is always
raised back to the agent — whether it came from the pre-flight bucket
classification ("healthy but doesn't fit") or from provider response
("estimator under-counted"). The agent owns the L1/L2/L4 retry loop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import copy

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
        *,
        cross_family_fallback: bool = False,
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
        # Phase 3: opt-in cross-family failover (design §6).
        self.cross_family_fallback = cross_family_fallback

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

        # ---- Step 3: group by protocol family, then run the main loop ----
        ordered = sorted(
            healthy_and_fits,
            key=lambda n: (-n.priority, n.node_id),
        )
        primary_family = ordered[0].protocol_family
        same_family = [n for n in ordered if n.protocol_family == primary_family]

        # Cross-family candidates are ONLY considered when the operator
        # opted in via `routing.cross_family_fallback`. Design §6 + §13.7
        # step 1 of Block B.
        cross_family: list[ModelNode] = []
        if self.cross_family_fallback:
            cross_family = [n for n in ordered if n.protocol_family != primary_family]
            # Per-family priority order is preserved from `ordered`.

        candidate_ids = [n.node_id for n in ordered]
        attempts: list[tuple[str, ErrorCategory, Exception]] = []

        # ---- Step 3a: try primary family with the original messages ----
        level_base = 0
        response = await self._try_candidates(
            same_family,
            messages,
            tools,
            level_base=level_base,
            candidate_ids=candidate_ids,
            attempts=attempts,
        )
        if response is not None:
            return response

        # ---- Step 3b: cross-family hop (optional) ----
        if not cross_family:
            raise AllNodesFailedError(attempts=attempts)

        # Different protocol family → drop `thinking` (Anthropic-only
        # semantic block) and any orphan tool_use/tool_result pairs,
        # and enforce capability gates (supports_tools if the caller
        # actually passed tools). Design §6.1 diff 2, 4, 5.
        target_family = cross_family[0].protocol_family
        adapted_messages = self._prepare_messages_for_family(messages, target_family)
        capable_cross = [
            n for n in cross_family
            if self._is_capable_for(n, adapted_messages, tools)
        ]
        if not capable_cross:
            logger.warning(
                "cross_family_fallback enabled but no %s-family node satisfies "
                "capability gate for this request",
                target_family,
            )
            raise AllNodesFailedError(attempts=attempts)

        response = await self._try_candidates(
            capable_cross,
            adapted_messages,
            tools,
            level_base=len(same_family),
            candidate_ids=candidate_ids,
            attempts=attempts,
        )
        if response is not None:
            return response

        raise AllNodesFailedError(attempts=attempts)

    async def _try_candidates(
        self,
        candidates: list[ModelNode],
        messages: list[Any],
        tools: list[Any] | None,
        *,
        level_base: int,
        candidate_ids: list[str],
        attempts: list[tuple[str, ErrorCategory, Exception]],
    ) -> Any:
        """Drive the main loop over one ordered candidate list.

        Returns the successful response, or `None` if every candidate
        failed with a switchable error (so the caller can decide whether
        to hop to the other family).

        ContextOverflow / BadRequest propagate out unchanged — those are
        never cross-family retryable.
        """
        for idx, node in enumerate(candidates):
            level = level_base + idx
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
                # Event-time capacity belongs to the agent — don't try
                # a different node, don't compress; the agent's L1/L2/L4
                # path is the right recovery.
                raise
            except BadRequestError:
                # Program bug — do not mask by trying the next node.
                raise
            except (TransientError, RateLimitError, AuthError, NodeUnavailableError) as exc:
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))
                self.breaker.record_failure(node.node_id, category, exc)
                self._notify_failover(candidates, idx, exc, category)
                continue
            except LLMError as exc:
                # Other LLMError subclasses we didn't anticipate.
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))
                self.breaker.record_failure(node.node_id, category, exc)
                self._notify_failover(candidates, idx, exc, category)
                continue
            except Exception as exc:
                # SDK-native exception that escaped normalization.
                category = classify_error(exc)
                attempts.append((node.node_id, category, exc))
                if not is_node_switchable(category):
                    if category == ErrorCategory.CAPACITY:
                        raise ContextOverflowError(str(exc)) from exc
                    raise
                self.breaker.record_failure(node.node_id, category, exc)
                self._notify_failover(candidates, idx, exc, category)
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

        return None

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

        Kept so tests and external callers can drive custom per-node
        lambdas via `.execute(fn)`. New code should use `.call()` which
        does the three-bucket classification itself.
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

        Retained for ad-hoc callers/tests that want to drive a custom
        per-node coroutine. The production path is `.call()`.
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

    # ------------------------------------------------------------------
    # Phase 3: cross-protocol-family helpers (design §6)
    # ------------------------------------------------------------------

    def _prepare_messages_for_family(
        self,
        messages: list[Any],
        target_family: str,
    ) -> list[Any]:
        """Return a **copy** of messages safe to send to `target_family`.

        Two transformations, always applied on a cross-family hop:

        1. **Strip `thinking`** (design §6.1 diff 2). Anthropic's
           `thinking` content-block has no OpenAI analog; replaying it
           to the other family either errors out or bleeds reasoning
           into the visible content.

        2. **Drop orphan tool_calls / tool_results** (design §6.1 diffs
           4, 5). If an assistant message claims `tool_calls=[tc1, tc2]`
           but only tc1's `tool` result follows, OpenAI and Anthropic
           both reject the payload with a 400. We drop the assistant's
           orphan tool_call entries (keeping matched ones), and drop any
           `tool` messages whose `tool_call_id` has no upstream tool_use.

        Messages are deep-copied so the caller's view (and future same-
        family attempts on unchanged messages) is untouched.
        """
        prepared: list[Any] = []
        # First pass: collect the set of tool_call ids that appear in
        # any assistant message; we'll use it to filter orphan tool
        # results.
        known_tool_call_ids: set[str] = set()
        for msg in messages:
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    tc_id = getattr(tc, "id", None)
                    if tc_id:
                        known_tool_call_ids.add(tc_id)

        # Second pass: collect the set of tool_call_ids that actually
        # have a matching `tool` result following them.
        answered_tool_call_ids: set[str] = set()
        for msg in messages:
            if getattr(msg, "role", None) == "tool":
                tc_id = getattr(msg, "tool_call_id", None)
                if tc_id:
                    answered_tool_call_ids.add(tc_id)

        # Third pass: build the prepared list.
        for msg in messages:
            role = getattr(msg, "role", None)
            # Drop orphan tool results (no matching tool_use upstream).
            if role == "tool":
                tc_id = getattr(msg, "tool_call_id", None)
                if tc_id and tc_id not in known_tool_call_ids:
                    continue
                prepared.append(copy.deepcopy(msg))
                continue

            new_msg = copy.deepcopy(msg)
            # Strip `thinking` unconditionally (it's the Anthropic-only
            # block; OpenAI-side `reasoning_details` will be regenerated
            # on the next assistant turn from the new provider).
            if hasattr(new_msg, "thinking"):
                try:
                    new_msg.thinking = None
                except (AttributeError, TypeError):
                    pass  # frozen/__slots__/immutable: deep-copied, harmless
                except Exception as e:
                    # Validation errors (Pydantic) land here: if stripping
                    # actually failed we must NOT silently forward the
                    # unstripped `thinking` block to the other family.
                    logger.warning(
                        "Failed to strip 'thinking' on %s: %s",
                        type(new_msg).__name__, e,
                    )

            # Filter assistant.tool_calls: keep only those with a
            # matching tool result downstream. Orphans get dropped.
            tool_calls = getattr(new_msg, "tool_calls", None)
            if tool_calls:
                kept = [
                    tc for tc in tool_calls
                    if getattr(tc, "id", None) in answered_tool_call_ids
                ]
                try:
                    new_msg.tool_calls = kept or None
                except (AttributeError, TypeError):
                    pass
                except Exception as e:
                    logger.warning(
                        "Failed to filter tool_calls on %s: %s",
                        type(new_msg).__name__, e,
                    )
                # If we dropped every tool_call AND the assistant has no
                # content either, the message becomes useless — drop it.
                content = getattr(new_msg, "content", None)
                if not kept and not content:
                    continue

            prepared.append(new_msg)

        return prepared

    def _is_capable_for(
        self,
        node: ModelNode,
        messages: list[Any],
        tools: list[Any] | None,
    ) -> bool:
        """Capability gate applied per candidate for cross-family hops.

        Three checks (design §6.2):
        - `supports_tools` — if the caller passed tools, the target
          must advertise tool support. Same guard if the adapted
          messages still reference tool_calls after orphan cleanup.
        - `context_window` is already enforced by the upstream fits
          bucket, so not re-checked here.
        - `supports_thinking` is not gated: we strip `thinking` before
          the hop, so the target doesn't need to understand it.
        """
        if tools:
            if not node.supports_tools:
                return False

        # If messages include tool_calls (kept after orphan cleanup),
        # the target must support tools even if the caller passed
        # `tools=None` for this turn (the prior assistant turn is still
        # in the transcript).
        for msg in messages:
            if getattr(msg, "tool_calls", None):
                if not node.supports_tools:
                    return False
                break

        return True
