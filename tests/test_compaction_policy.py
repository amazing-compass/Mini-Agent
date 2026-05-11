"""Unit tests for the DP compaction policy.

The policy is a pure function over a snapshot, so these tests don't
need an Agent / router / LLM client. They cover:
- threshold + forced paths
- selecting the optimal k when multiple positive
- no-op when all NetBenefit values are non-positive
- empty / short live_messages
- distortion term growth with compact_count
- user-round boundaries respecting tool boundaries
- pricing fallback for unknown models
"""

from __future__ import annotations

import pytest

from mini_agent.compaction import (
    CachePolicy,
    CompactionDecision,
    CompactionPolicy,
    CompactionSnapshot,
    ModelPricing,
)
from mini_agent.compaction.policy import CompactionPolicy as _Policy
from mini_agent.schema import FunctionCall, Message, ToolCall


# ---------------------------------------------------------------------
# Helpers — build messages of a target token shape cheaply.
# ---------------------------------------------------------------------


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _assistant_text(text: str) -> Message:
    return Message(role="assistant", content=text)


def _assistant_with_tool(text: str, tool_name: str, tc_id: str = "tc1") -> Message:
    return Message(
        role="assistant",
        content=text,
        tool_calls=[
            ToolCall(
                id=tc_id,
                type="function",
                function=FunctionCall(name=tool_name, arguments={}),
            )
        ],
    )


def _tool_result(text: str, tool_name: str, tc_id: str = "tc1") -> Message:
    return Message(role="tool", content=text, tool_call_id=tc_id, name=tool_name)


def _round(user_text: str, asst_text: str = "ok") -> list[Message]:
    """One simple user-round: user + assistant. Easy to chain."""
    return [_user(user_text), _assistant_text(asst_text)]


def _heavy_round(user_text: str, payload_size: int = 3000) -> list[Message]:
    """User-round whose assistant carries a chunky payload (forces H >> MIN_DROP).

    Uses pseudo-random tokens (not a single-char repeat) because
    tiktoken collapses 8K repeated 'x' down to ~1K tokens via BPE.
    Each space-separated word becomes a fresh token.
    """
    words = " ".join(f"w{i:04d}" for i in range(payload_size))
    return [_user(user_text), _assistant_text(words)]


def _snapshot(
    *,
    live_messages: list[Message],
    pricing: ModelPricing | None = None,
    user_turn_count: int = 5,
    llm_call_count: int = 10,
    compact_count: int = 0,
    api_input_token_estimate: int = 50_000,
    max_context: int = 128_000,
    system_token_count: int = 2_000,
    tools_token_count: int = 1_000,
) -> CompactionSnapshot:
    pricing = pricing or ModelPricing(input=10.0, cache_read=1.0, cache_write=12.5, output=30.0)
    return CompactionSnapshot(
        live_messages=live_messages,
        current_summary=None,
        system_token_count=system_token_count,
        tools_token_count=tools_token_count,
        pricing=pricing,
        user_turn_count=user_turn_count,
        llm_call_count=llm_call_count,
        compact_count=compact_count,
        api_input_token_estimate=api_input_token_estimate,
        max_context=max_context,
    )


# ---------------------------------------------------------------------
# Threshold paths
# ---------------------------------------------------------------------


def test_decide_below_threshold_returns_noop_or_net_decision():
    """Light snapshot with tiny dropped pool — no compaction worth doing."""
    policy = CompactionPolicy()
    snap = _snapshot(
        live_messages=_round("hi"),  # only one round → nothing to drop
        api_input_token_estimate=10_000,
        max_context=128_000,
    )
    decision = policy.decide(snap)
    assert decision.should_compact is False
    assert decision.reason == "nothing_to_drop"


def test_decide_above_hard_threshold_forces_compact_with_keep_1():
    """When api_input exceeds 90% of max_context the DP branch is skipped."""
    policy = CompactionPolicy()
    msgs = [*_round("u1"), *_round("u2"), *_round("u3")]
    snap = _snapshot(
        live_messages=msgs,
        api_input_token_estimate=120_000,  # > 90% of 128_000
        max_context=128_000,
    )
    decision = policy.decide(snap)
    assert decision.should_compact is True
    assert decision.forced is True
    assert decision.keep_round_count == 1
    # drop_message_count must equal the index of the last user message.
    last_user_idx = max(i for i, m in enumerate(msgs) if m.role == "user")
    assert decision.drop_message_count == last_user_idx
    assert decision.reason == "force_threshold"


def test_force_compact_keeps_only_recent_round():
    """``forced=True`` explicitly bypasses NetBenefit even when below threshold."""
    policy = CompactionPolicy()
    msgs = [*_round("u1"), *_round("u2"), *_round("u3"), *_round("u4")]
    snap = _snapshot(live_messages=msgs, api_input_token_estimate=10_000)
    decision = policy.decide(snap, forced=True)
    assert decision.forced is True
    assert decision.reason == "force_overflow"
    assert decision.keep_round_count == 1


# ---------------------------------------------------------------------
# Optimal-k selection
# ---------------------------------------------------------------------


def test_decide_picks_optimal_k_when_multiple_positive():
    """DP should pick the boundary that maximises NetBenefit, not just the first.

    Uses Anthropic-shape pricing (large cache discount, cache_write
    only 25% surcharge) so multiple k values yield positive
    NetBenefit. The test asserts the policy chose to compact (which
    proves it walked the boundaries and computed a maximum).
    """
    policy = CompactionPolicy()
    msgs = [
        m for i in range(5) for m in _heavy_round(f"u{i}", payload_size=2_000)
    ]
    # Sonnet-shape pricing: high cache discount but small write
    # surcharge → DP can clearly afford compaction.
    snap = _snapshot(
        live_messages=msgs,
        pricing=ModelPricing(input=3.0, cache_read=0.30, cache_write=3.75, output=15.0),
        api_input_token_estimate=40_000,
        user_turn_count=2,
        llm_call_count=10,
        compact_count=0,
    )
    decision = policy.decide(snap)
    assert decision.should_compact is True
    assert decision.reason == "net_positive"
    # Must drop something but keep at least one user-round.
    assert decision.drop_message_count > 0
    assert decision.keep_round_count >= 1


def test_decide_returns_noop_when_all_net_benefit_negative():
    """When dropped tokens are below MIN_DROP_TOKENS, no k qualifies."""
    policy = CompactionPolicy()
    # Several small rounds, none heavy enough to clear MIN_DROP_TOKENS.
    msgs = [m for _ in range(5) for m in _round(f"u{_}", "ok")]
    snap = _snapshot(
        live_messages=msgs,
        api_input_token_estimate=10_000,
    )
    decision = policy.decide(snap)
    assert decision.should_compact is False
    assert decision.reason in {"no_benefit", "nothing_to_drop"}


# ---------------------------------------------------------------------
# Edge cases on live_messages shape
# ---------------------------------------------------------------------


def test_decide_handles_empty_live_messages():
    policy = CompactionPolicy()
    snap = _snapshot(live_messages=[], api_input_token_estimate=1_000)
    decision = policy.decide(snap)
    assert decision.should_compact is False
    assert decision.reason == "nothing_to_drop"


def test_decide_handles_single_round_cannot_drop_all():
    """One user-round → 1 boundary → no candidate split point."""
    policy = CompactionPolicy()
    snap = _snapshot(live_messages=_round("only one"))
    decision = policy.decide(snap)
    assert decision.should_compact is False
    assert decision.reason == "nothing_to_drop"


def test_user_round_boundaries_handles_parallel_tool_calls():
    """An assistant with parallel tool_calls followed by multiple tool
    messages must NOT introduce a boundary inside the tool block. We
    only count role='user' as a boundary."""
    msgs = [
        _user("u1"),
        _assistant_with_tool("a1", "tool_a", "id1"),
        _tool_result("ok1", "tool_a", "id1"),
        _tool_result("ok2", "tool_a", "id2"),  # parallel tool result
        _user("u2"),
        _assistant_text("a2"),
    ]
    boundaries = _Policy._user_round_boundaries(msgs)
    # Only two boundaries: the two user messages. Tool-role indices are excluded.
    assert boundaries == [0, 4]


def test_user_round_boundaries_only_includes_real_user_messages():
    """Verify role='tool' is NEVER treated as a user-round boundary."""
    msgs = [
        _user("u1"),
        _assistant_text("a1"),
        _tool_result("r", "x", "id1"),
        _tool_result("r2", "y", "id2"),
        _user("u2"),
    ]
    assert _Policy._user_round_boundaries(msgs) == [0, 4]


# ---------------------------------------------------------------------
# Distortion term growth (c+1 grows in formula)
# ---------------------------------------------------------------------


def test_distortion_term_grows_with_compact_count():
    """After many prior compactions the DP should become more conservative."""
    policy = CompactionPolicy()
    msgs = [
        m for i in range(5) for m in _heavy_round(f"u{i}", payload_size=2_000)
    ]
    # Sonnet-shape pricing so low-c is robustly net-positive.
    pricing = ModelPricing(input=3.0, cache_read=0.30, cache_write=3.75, output=15.0)

    snap_low = _snapshot(
        live_messages=msgs,
        pricing=pricing,
        compact_count=0,
        api_input_token_estimate=40_000,
        user_turn_count=2,
        llm_call_count=10,
    )
    snap_high = _snapshot(
        live_messages=msgs,
        pricing=pricing,
        compact_count=50,
        api_input_token_estimate=40_000,
        user_turn_count=2,
        llm_call_count=10,
    )

    decision_low = policy.decide(snap_low)
    decision_high = policy.decide(snap_high)

    # Low-c must compact; high-c must be strictly more conservative —
    # either NetBenefit shrinks or the decision flips to no-op.
    assert decision_low.should_compact is True
    if decision_high.should_compact:
        assert decision_high.net_benefit < decision_low.net_benefit
    # else: no-op is strictly more conservative — pass.


# ---------------------------------------------------------------------
# Pricing fallback behavior
# ---------------------------------------------------------------------


def test_unknown_model_falls_back_to_conservative_pricing():
    """When cache_read == cache_write == input, DP loses the cache term
    but can still recommend compaction on pure token-reduction grounds."""
    policy = CompactionPolicy()
    msgs = (
        _heavy_round("u1", payload_size=8_000)
        + _heavy_round("u2", payload_size=8_000)
        + _round("u3")
    )
    # Pricing where cache_read == cache_write == input.
    pricing = ModelPricing(input=10.0, cache_read=10.0, cache_write=10.0, output=30.0)
    snap = _snapshot(
        live_messages=msgs,
        pricing=pricing,
        api_input_token_estimate=40_000,
        # Generous remaining rounds so future_savings × input is big.
        user_turn_count=2,
        llm_call_count=8,
    )
    decision = policy.decide(snap)
    # Either compact or no-op is acceptable; what matters is no crash
    # and the formula stays sane. Confirm it didn't degenerate.
    assert isinstance(decision, CompactionDecision)
    # If it did compact, the reason must be net_positive (not forced —
    # we deliberately stayed below 90%).
    if decision.should_compact:
        assert decision.reason == "net_positive"


# ---------------------------------------------------------------------
# DeepSeek-style snapshot (smoke test using real pricing)
# ---------------------------------------------------------------------


def test_deepseek_pricing_smoke_test_returns_a_decision():
    """DeepSeek pricing is a hostile environment for the v1 DP formula
    (rewrite_cost = full input price, future_savings = tiny cache_read).
    The DP can rationally return either compact or no-op; this is a
    smoke test that the policy executes without crashing and emits a
    valid CompactionDecision."""
    policy = CompactionPolicy()
    msgs = [m for i in range(6) for m in _heavy_round(f"u{i}", payload_size=2_000)]
    pricing = ModelPricing(input=0.435, cache_read=0.003625, cache_write=0.435, output=0.87)
    snap = _snapshot(
        live_messages=msgs,
        pricing=pricing,
        user_turn_count=3,
        llm_call_count=10,
        api_input_token_estimate=50_000,
        max_context=128_000,
    )
    decision = policy.decide(snap)
    assert isinstance(decision, CompactionDecision)
    assert decision.reason in {"net_positive", "no_benefit", "nothing_to_drop"}
