# ✅
"""Per-(protocol_family, model) pricing lookup for the DP compaction policy.

The price table here is illustrative and pegged to 2026-05 reference
values (DeepSeek V4 Pro/Flash promo, Claude 4.x family, MiniMax-M2.5,
gpt-4o). Operators should reconfirm against the provider's official
pricing page before relying on the DP for cost decisions.

The lookup keys are ``(protocol_family, model)``: a model that runs on
both Anthropic-compatible and OpenAI-compatible endpoints can carry
different cache semantics on each surface, and the protocol_family
distinguishes them. Unknown combinations fall back to
``_DEFAULT_PRICING`` which has ``cache_read == cache_write == input``
so the DP loses cache-discount sensitivity but can still recommend
compaction on pure "fewer tokens" grounds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import ModelPricing

if TYPE_CHECKING:
    from ..llm.ha.models import ModelNode

# 模型价格表 + 查价器


# Reference prices as of 2026-05 — see docstring for the source-of-truth
# caveat. Keep entries grouped by protocol_family so it's obvious where
# a new model needs to land.
# K - V
# K 是 (protocol_family, model) 元组，V 是 ModelPricing 对象
_PRICING_TABLE: dict[tuple[str, str], ModelPricing] = {
    # DeepSeek V4 (automatic Context Caching; no explicit cache_control).
    # 2026-05 promo: hit / miss / output = 0.003625 / 0.435 / 0.87 USD/1M.
    ("anthropic", "deepseek-v4-pro"):   ModelPricing(0.435, 0.003625, 0.435, 0.87),
    ("openai",    "deepseek-v4-pro"):   ModelPricing(0.435, 0.003625, 0.435, 0.87),
    ("anthropic", "deepseek-v4-flash"): ModelPricing(0.14,  0.0028,   0.14,  0.28),
    ("openai",    "deepseek-v4-flash"): ModelPricing(0.14,  0.0028,   0.14,  0.28),

    # Anthropic official endpoint (supports explicit cache_control).
    ("anthropic", "claude-opus-4-7"):   ModelPricing(15.0, 1.50, 18.75, 75.0),
    ("anthropic", "claude-sonnet-4-6"): ModelPricing(3.0,  0.30, 3.75,  15.0),
    ("anthropic", "claude-haiku-4-5"):  ModelPricing(0.80, 0.08, 1.00,  4.0),

    # MiniMax — cache semantics on each endpoint are not confirmed in
    # operator docs; pricing kept symmetric so the DP falls back to the
    # "no cache discount" branch unless the node explicitly opts in.
    ("anthropic", "MiniMax-M2.5"):      ModelPricing(0.30, 0.30, 0.30,  1.20),
    ("openai",    "MiniMax-M2.5"):      ModelPricing(0.30, 0.30, 0.30,  1.20),

    # OpenAI (auto-caches its own prefixes; no explicit marker).
    ("openai", "gpt-4o"):               ModelPricing(2.50, 1.25, 2.50, 10.0),
}


# Fallback — cache_read == cache_write == input means the DP loses the
# cache-discount term but still benefits from "fewer tokens" savings,
# which is the only honest thing to say for an unknown provider.
_DEFAULT_PRICING = ModelPricing(3.0, 3.0, 3.0, 15.0)

# CachePolicy 查价器
# 输入一个 ModelNode 对象，输出一个 ModelPricing 对象
class CachePolicy:
    """Lookup helper for ``ModelPricing`` keyed by node identity."""

    @staticmethod
    def pricing_for_node(node: "ModelNode | None") -> ModelPricing:
        """Return pricing for the node, falling back to a conservative default.

        Note: this returns *theoretical* hit/miss prices. Whether the
        node actually qualifies for cache discounts depends on its
        ``supports_explicit_cache_control`` and
        ``supports_automatic_context_cache`` flags. The Agent's snapshot
        builder applies the degradation; this function only handles the
        table lookup.
        """
        if node is None:
            return _DEFAULT_PRICING
        key = (
            getattr(node, "protocol_family", ""),
            getattr(node, "model", ""),
        )
        return _PRICING_TABLE.get(key, _DEFAULT_PRICING)
