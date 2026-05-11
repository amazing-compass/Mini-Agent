"""Unit tests for :class:`mini_agent.compaction.cache_policy.CachePolicy`.

The policy is a thin lookup helper; these tests cover:
- ``(protocol_family, model)`` keying (same model, different protocol → different pricing entry possible)
- DeepSeek hit/miss prices are preserved regardless of explicit-cache flag
- Unknown models fall back to the conservative default
- ``None`` node falls back gracefully
"""

from __future__ import annotations

from mini_agent.compaction import CachePolicy, ModelPricing
from mini_agent.compaction.cache_policy import (
    _DEFAULT_PRICING,
    _PRICING_TABLE,
)
from mini_agent.llm.ha.models import ModelNode


def _node(
    *,
    protocol_family: str,
    model: str,
    supports_explicit_cache_control: bool = False,
    supports_automatic_context_cache: bool = False,
) -> ModelNode:
    return ModelNode(
        node_id="t",
        provider=protocol_family,
        protocol_family=protocol_family,
        api_key="sk",
        api_base="https://example.test",
        model=model,
        supports_explicit_cache_control=supports_explicit_cache_control,
        supports_automatic_context_cache=supports_automatic_context_cache,
    )


def test_pricing_for_node_uses_protocol_family_and_model():
    """The same model name on different protocols can resolve to its
    own entry — the key is ``(protocol_family, model)``, not just
    ``model``."""
    deepseek_anthropic = _node(
        protocol_family="anthropic",
        model="deepseek-v4-pro",
        supports_automatic_context_cache=True,
    )
    deepseek_openai = _node(
        protocol_family="openai",
        model="deepseek-v4-pro",
        supports_automatic_context_cache=True,
    )
    p1 = CachePolicy.pricing_for_node(deepseek_anthropic)
    p2 = CachePolicy.pricing_for_node(deepseek_openai)
    # Both registered entries → both non-default.
    assert p1 is not _DEFAULT_PRICING
    assert p2 is not _DEFAULT_PRICING


def test_deepseek_pricing_uses_hit_miss_prices_without_explicit_cache_control():
    """DeepSeek nodes carry ``supports_explicit_cache_control=False``
    but still have a real hit/miss price entry that the DP must use —
    automatic Context Caching gives them a cache discount even without
    explicit markers."""
    node = _node(
        protocol_family="anthropic",
        model="deepseek-v4-pro",
        supports_explicit_cache_control=False,
        supports_automatic_context_cache=True,
    )
    pricing = CachePolicy.pricing_for_node(node)
    # Hit price strictly less than miss price → real cache discount.
    assert pricing.cache_read < pricing.input
    # Promo as of 2026-05: cache_read = 0.003625 USD / 1M.
    assert pricing.cache_read == 0.003625


def test_pricing_for_node_falls_back_for_unknown_model():
    """Unknown (protocol, model) tuple returns the conservative default
    where cache_read == cache_write == input."""
    node = _node(protocol_family="anthropic", model="totally-fictional-model-9000")
    pricing = CachePolicy.pricing_for_node(node)
    assert pricing == _DEFAULT_PRICING
    assert pricing.cache_read == pricing.input
    assert pricing.cache_write == pricing.input


def test_pricing_for_node_handles_none_node():
    """No primary node (empty pool) must not crash."""
    pricing = CachePolicy.pricing_for_node(None)
    assert pricing == _DEFAULT_PRICING


def test_minimax_pricing_is_symmetric_so_dp_naturally_degrades():
    """MiniMax entries have ``cache_read == cache_write == input`` so
    even though the lookup succeeds, the DP loses cache-discount
    sensitivity (we don't have confirmed cache semantics for MiniMax)."""
    pricing = CachePolicy.pricing_for_node(
        _node(protocol_family="anthropic", model="MiniMax-M2.5")
    )
    assert pricing.cache_read == pricing.input
    assert pricing.cache_write == pricing.input


def test_anthropic_official_models_have_cache_write_surcharge():
    """Claude-family entries should have cache_write > input (the 25%
    cache_write surcharge), confirming explicit-cache pricing model."""
    pricing = CachePolicy.pricing_for_node(
        _node(protocol_family="anthropic", model="claude-sonnet-4-6")
    )
    assert pricing.cache_write > pricing.input
    assert pricing.cache_read < pricing.input


def test_pricing_table_keys_are_lowercase_protocol_families():
    """Sanity check: the lookup uses raw ModelNode fields, so the
    table keys must match the lowercase protocol_family convention."""
    for (protocol, _model) in _PRICING_TABLE:
        assert protocol == protocol.lower(), (
            f"protocol_family key {protocol!r} must be lowercase"
        )


def test_modelpricing_is_a_value_type():
    """ModelPricing is a frozen dataclass — equal pricing dicts must
    compare equal. (Sanity check around the dataclass(frozen=True)
    declaration.)"""
    p1 = ModelPricing(1.0, 0.1, 1.25, 5.0)
    p2 = ModelPricing(1.0, 0.1, 1.25, 5.0)
    assert p1 == p2
