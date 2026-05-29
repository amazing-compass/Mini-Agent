# ✅
"""DP-driven compaction decision policy.

The policy is a pure function over :class:`CompactionSnapshot`:

    decision = CompactionPolicy().decide(snapshot, forced=False)

The DP formula has four components (see IMPROVEMENT_04 §3.4):

    NetBenefit(k) = future_savings
                  - prefix_rewrite_cost
                  - summary_call_cost
                  - information_loss_cost

We enumerate user-round boundaries ``k`` and pick the ``k`` that
maximises ``NetBenefit``; if every ``k`` yields ``<= 0`` we no-op. A
``forced=True`` call (overflow recovery or HARD_THRESHOLD) skips the DP
entirely and keeps only the most recent user-round.

Critical v1 detail: ``P_summary_input == P_input`` (NOT
``max(P_cache_write, P_input)``). Reason: v1 summary calls do not attach
BP #4 on dropped messages, so an Anthropic cache miss on dropped is
billed at input rate, not cache_write rate.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import tiktoken

from .models import CompactionDecision, CompactionSnapshot

if TYPE_CHECKING:
    from ..schema import Message


class CompactionPolicy:
    """Stateless DP policy. Safe to share across requests."""

    # ---- Tunable constants (kept conservative; see §3.4) ----------------
    # 全部是类级常量
    BASELINE_E = 20           # 预期剩余用户轮次的锚点值
    DEFAULT_L = 5             # 每个用户轮次默认的 LLM 调用次数
    BETA = 0.3                # 信息损失权重（公式里 distortion 项的系数）
    R_DECAY = 0.9             # 重复压缩时的几何衰减因子
    SUMMARY_SIZE = 500        # 摘要输出的 token 预算
    L_INSTR = 200             # SUMMARY_INSTRUCTION 的 token 成本
    MIN_DROP_TOKENS = 1000    # 小于这个量不值得压缩，跳过
    HARD_THRESHOLD = 0.90     # 上下文超过 90% 强制压缩

    def __init__(self) -> None:
        # Encoder is cached on the instance so repeated calls don't
        # re-instantiate cl100k_base. tiktoken is happy to be reused.
        self._encoder = None   # 懒加载的 tiktoken 编码器

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(
        self,
        snapshot: CompactionSnapshot,
        *,
        forced: bool = False,
    ) -> CompactionDecision:
        """Pick the best user-round split point (or no-op)."""
        # Step 1: forced path (overflow recovery or HARD_THRESHOLD).
        if forced or snapshot.api_input_token_estimate > (
            self.HARD_THRESHOLD * snapshot.max_context
        ):
            return self._force_compact(snapshot, forced_by_caller=forced)

        # Step 2: enumerate candidate boundaries.
        boundaries = self._user_round_boundaries(snapshot.live_messages)
        if len(boundaries) < 2:
            return self._noop("nothing_to_drop")

        # Step 3: derive expected-remaining-LLM-calls R.
        E = max(self.BASELINE_E - snapshot.user_turn_count, self.BASELINE_E // 2)
        if snapshot.user_turn_count > 0:
            L = snapshot.llm_call_count / snapshot.user_turn_count
        else:
            L = self.DEFAULT_L
        R = max(E * L, 1)

        # Step 4: pricing.
        P_in = snapshot.pricing.input
        P_cache_r = snapshot.pricing.cache_read
        P_cache_w = snapshot.pricing.cache_write
        P_out = snapshot.pricing.output
        # Dropped messages don't carry BP #4 in v1, so a cache miss on
        # them is billed at input rate, not cache_write rate.
        P_summary = P_in

        V = snapshot.system_token_count + snapshot.tools_token_count
        S = self.SUMMARY_SIZE
        c_next = snapshot.compact_count + 1
        distortion_factor = self.BETA * (1 - self.R_DECAY ** c_next)

        # Step 5: walk every plausible boundary.
        best: tuple[int, float, int] | None = None  # (drop_count, net_benefit, keep_round_count)
        n_boundaries = len(boundaries)

        for round_idx in range(1, n_boundaries):
            k = boundaries[round_idx]
            dropped = snapshot.live_messages[:k]
            kept = snapshot.live_messages[k:]
            H = self._token_count(dropped)
            K = self._token_count(kept)

            if H < self.MIN_DROP_TOKENS:
                continue

            avg = H / max(len(dropped), 1)

            future_savings = (R * P_cache_r * max(H - S, 0)) / 1_000_000
            rewrite_cost = ((S + K) * (P_cache_w - P_cache_r)) / 1_000_000
            summary_cost = (
                P_summary * (V + H) + P_in * self.L_INSTR + P_out * S
            ) / 1_000_000
            distortion = (distortion_factor * R * avg * P_in) / 1_000_000

            net = future_savings - rewrite_cost - summary_cost - distortion
            keep_round_count = n_boundaries - round_idx

            if best is None or net > best[1]:
                best = (k, net, keep_round_count)

        if best is None or best[1] <= 0:
            return self._noop("no_benefit")

        return CompactionDecision(
            should_compact=True,
            keep_round_count=best[2],
            drop_message_count=best[0],
            reason="net_positive",
            net_benefit=best[1],
            forced=False,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _force_compact(
        self,
        snapshot: CompactionSnapshot,
        *,
        forced_by_caller: bool,
    ) -> CompactionDecision:
        """Ignore NetBenefit and keep only the most recent user-round.

        ``forced_by_caller=True`` => the caller (overflow recovery) said
        so; ``False`` => we hit the HARD_THRESHOLD branch ourselves and
        decided to force.
        """
        boundaries = self._user_round_boundaries(snapshot.live_messages)
        if len(boundaries) < 2:
            return self._noop("nothing_to_drop")
        k = boundaries[-1]
        reason = "force_overflow" if forced_by_caller else "force_threshold"
        return CompactionDecision(
            should_compact=True,
            keep_round_count=1,
            drop_message_count=k,
            reason=reason,
            net_benefit=0.0,
            forced=True,
        )

    # ✅
    # 找到所有 role == "user" 的位置
    @staticmethod
    def _user_round_boundaries(messages: list["Message"]) -> list[int]:
        """Indices in ``messages`` where each user-round starts.

        We deliberately filter on ``role == "user"`` only. ``role="tool"``
        messages also live inside a round but are NOT user-round
        boundaries — splitting there would tear apart a tool_use/
        tool_result pair, which Anthropic-strict endpoints reject.
        """
        return [i for i, m in enumerate(messages) if m.role == "user"]

    # ✅
    def _encoder_get(self):
        enc = self._encoder
        if enc is None:
            enc = tiktoken.get_encoding("cl100k_base")
            self._encoder = enc
        return enc

    # ✅
    def _encode_len(self, value: object) -> int:
        """Best-effort token length for v1 DP estimates."""
        if value is None:
            return 0
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return len(self._encoder_get().encode(text))

    def _token_count(self, messages: list["Message"]) -> int:
        """Approximate token count for a slice of live_messages.

        Covers content / thinking / tool_calls / tool_call_id / name,
        plus a small per-message overhead. Provider tokenizers differ
        from cl100k_base by a few percent; the DP only needs relative
        comparisons, so absolute precision is intentionally sacrificed
        for speed.
        """
        total = 0
        for msg in messages:
            total += 4  # role + JSON framing overhead
            total += self._encode_len(msg.role)
            total += self._encode_len(msg.content)
            total += self._encode_len(msg.thinking)
            total += self._encode_len(msg.tool_calls)
            total += self._encode_len(msg.tool_call_id)
            total += self._encode_len(msg.name)
        return total

    # ✅不压缩
    @staticmethod
    def _noop(reason: str) -> CompactionDecision:
        return CompactionDecision(
            should_compact=False,
            keep_round_count=0,
            drop_message_count=0,
            reason=reason,
            net_benefit=0.0,
            forced=False,
        )
