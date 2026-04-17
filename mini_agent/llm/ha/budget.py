"""Per-request token budgeting (read-only).

`TokenBudget` lives on the router's side of the agent/router boundary —
it only estimates and answers "does this request fit in that node?".
All **compression** decisions (L1/L2/L4) stay in the agent; the router
never calls TokenBudget to mutate `messages`.

Estimation uses tiktoken's cl100k_base encoder, which covers GPT-4,
Claude, and most MiniMax models within a 5-15% error band. That error
is the reason design §5.7 mandates an *event-time* ContextOverflow
fallback even though we do pre-flight fits: the router can be off by a
few thousand tokens on exotic models, and we'd rather let provider
400-responses correct us than over-tighten the fit predicate.
"""

from __future__ import annotations

import json
from typing import Any

try:
    import tiktoken

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover — tiktoken missing/broken
    _ENCODER = None


# Per-message overhead (role header, separators, delimiters). Matches the
# figure the agent uses for its own pre-compression estimate so the two
# layers don't disagree wildly.
_MESSAGE_OVERHEAD_TOKENS = 4
_TOOL_OVERHEAD_TOKENS = 16


def _encode_len(text: str) -> int:
    if not text:
        return 0
    if _ENCODER is None:
        # Crude fallback: ~2.5 chars per token.
        return max(1, int(len(text) / 2.5))
    return len(_ENCODER.encode(text))


def _token_len_of(obj: Any) -> int:
    """Best-effort token count for an arbitrary JSON-serializable object."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return _encode_len(obj)
    try:
        serialized = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        serialized = str(obj)
    return _encode_len(serialized)


class TokenBudget:
    """Stateless estimator — all methods are classmethods."""

    @classmethod
    def estimate(cls, messages: list[Any], tools: list[Any] | None = None) -> int:
        """Rough input-side token count for `messages` + tool schemas.

        Accepts both `Message` objects (from `mini_agent.schema`) and the
        raw-dict form that provider clients emit. Router passes Messages;
        tests can pass either.
        """
        total = 0
        for msg in messages or []:
            total += cls._message_tokens(msg) + _MESSAGE_OVERHEAD_TOKENS
        for tool in tools or []:
            total += cls._tool_tokens(tool) + _TOOL_OVERHEAD_TOKENS
        return total

    @classmethod
    def fits(
        cls,
        messages: list[Any],
        tools: list[Any] | None,
        node: Any,
        *,
        expected_output: int,
        safety_margin: int = 1024,
    ) -> bool:
        """Would this request fit in `node.context_window`?

        `expected_output` is the minimum output headroom we insist on —
        a node that technically has 500 tokens of headroom "fits" by the
        numbers but wouldn't let the model say anything useful.
        """
        estimate = cls.estimate(messages, tools)
        budget_needed = estimate + expected_output + safety_margin
        context_window = getattr(node, "context_window", 0) or 0
        return budget_needed <= context_window

    # ---- helpers --------------------------------------------------------

    @classmethod
    def _message_tokens(cls, msg: Any) -> int:
        # Message (pydantic) path
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        content = getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        thinking = getattr(msg, "thinking", None) if not isinstance(msg, dict) else msg.get("thinking")
        tool_calls = getattr(msg, "tool_calls", None) if not isinstance(msg, dict) else msg.get("tool_calls")
        name = getattr(msg, "name", None) if not isinstance(msg, dict) else msg.get("name")

        total = 0
        if role:
            total += _encode_len(role)
        if name:
            total += _encode_len(name)

        if isinstance(content, str):
            total += _encode_len(content)
        elif isinstance(content, list):
            for block in content:
                total += _token_len_of(block)
        elif content is not None:
            total += _token_len_of(content)

        if thinking:
            total += _encode_len(str(thinking))

        if tool_calls:
            # tool_calls is a list of ToolCall objects or dicts.
            for tc in tool_calls:
                total += _token_len_of(_tool_call_as_dict(tc))

        return total

    @classmethod
    def _tool_tokens(cls, tool: Any) -> int:
        # Tool may be a Tool object with .to_schema(), a dict already in
        # Anthropic/OpenAI format, or something opaque. Serialize for
        # estimation only — never mutate.
        schema: Any
        if hasattr(tool, "to_schema"):
            try:
                schema = tool.to_schema()
            except Exception:
                schema = {"name": getattr(tool, "name", "")}
        elif isinstance(tool, dict):
            schema = tool
        else:
            schema = {"repr": repr(tool)}
        return _token_len_of(schema)


def _tool_call_as_dict(tc: Any) -> dict:
    """Best-effort conversion of a ToolCall to a dict for token counting."""
    if isinstance(tc, dict):
        return tc
    fn = getattr(tc, "function", None)
    return {
        "id": getattr(tc, "id", ""),
        "type": getattr(tc, "type", "function"),
        "function": {
            "name": getattr(fn, "name", "") if fn is not None else "",
            "arguments": getattr(fn, "arguments", {}) if fn is not None else {},
        },
    }
