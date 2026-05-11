"""Data classes used by the cache-aware compaction policy.

These types are pure data containers: no I/O, no LLM access, no side
effects. The policy in :mod:`mini_agent.compaction.policy` takes a
:class:`CompactionSnapshot` and returns a :class:`CompactionDecision`.
The Agent owns the integration glue around them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..schema import ContextSummary, Message


@dataclass(frozen=True)
class ModelPricing:
    """Per-1M-token prices for input / cache-read / cache-write / output.

    Units are USD per 1,000,000 tokens. Concrete values come from
    :mod:`mini_agent.compaction.cache_policy`; the DP formula treats
    them uniformly so a degraded fallback (cache_read == input) just
    nullifies the cache discount instead of producing nonsense.
    """

    input: float
    cache_read: float
    cache_write: float
    output: float


@dataclass
class CompactionSnapshot:
    """Read-only view of the Agent state that the policy needs.

    The policy is a pure function over this snapshot — the Agent builds
    it from its live state, the policy returns a decision. Constructing
    this object should be cheap; ``live_messages`` is passed by
    reference because the policy does not mutate it.
    """

    live_messages: list["Message"]
    current_summary: "ContextSummary | None"
    system_token_count: int
    tools_token_count: int
    pricing: ModelPricing
    user_turn_count: int
    llm_call_count: int
    compact_count: int
    api_input_token_estimate: int
    max_context: int


@dataclass
class CompactionDecision:
    """Output of :meth:`CompactionPolicy.decide`.

    ``drop_message_count`` is an index into ``live_messages``: drop
    ``live_messages[:drop_message_count]``, keep the rest.
    ``keep_round_count`` is the number of user-rounds retained; it's
    useful for logging but the index is authoritative.
    """

    should_compact: bool
    keep_round_count: int
    drop_message_count: int
    reason: str
    net_benefit: float
    forced: bool
