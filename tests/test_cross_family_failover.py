"""Tests for Phase 3 cross-protocol-family failover (Block B).

These tests pin the new behavior added by `routing.cross_family_fallback`:

1. **Default OFF**: same-family exhaustion still raises AllNodesFailedError
   — no silent cross-family hop.
2. **Opt-in ON**: after every Anthropic node fails with a switchable
   error, the router tries OpenAI-family nodes, sending a *transformed*
   message list that has `thinking` stripped and orphan `tool_use`
   blocks cleaned up (design §6.1 diffs 2, 4, 5).
3. **Capability gate**: cross-family candidates that don't satisfy the
   request's capability requirements (e.g. `supports_tools=False` when
   tools are passed) are filtered out.
4. **Preflight invariants preserved**: pre-flight ContextOverflow and
   event-time BadRequest still behave as before — cross-family logic
   does NOT mask them.
"""

from __future__ import annotations

import pytest

from mini_agent.llm.ha.errors import (
    AllNodesFailedError,
    AuthError,
    BadRequestError,
    ContextOverflowError,
    TransientError,
)
from mini_agent.llm.ha.health import SimpleBreaker
from mini_agent.llm.ha.models import ModelNode
from mini_agent.llm.ha.pool import ModelPool
from mini_agent.llm.ha.router import ModelRouter
from mini_agent.schema import FunctionCall, Message, ToolCall


class _Recording:
    """Scriptable fake client that records the exact messages it saw."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def generate(self, messages, tools=None, *, max_tokens=None):
        self.calls.append(
            {"messages": list(messages), "tools": tools, "max_tokens": max_tokens}
        )
        if not self.outcomes:
            raise RuntimeError("ran out of scripted outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _node(
    nid: str,
    *,
    family: str,
    priority: int,
    supports_tools: bool = True,
    supports_thinking: bool = True,
    context_window: int = 128_000,
) -> ModelNode:
    return ModelNode(
        node_id=nid,
        provider=family,
        protocol_family=family,
        api_key="sk-test",
        api_base="https://example.test",
        model=f"m-{nid}",
        priority=priority,
        context_window=context_window,
        max_output_tokens=4096,
        supports_tools=supports_tools,
        supports_thinking=supports_thinking,
    )


def _build_router(nodes, clients, *, cross_family_fallback: bool):
    pool = ModelPool(nodes)
    for nid, client in clients.items():
        pool.set_client(nid, client)
    breaker = SimpleBreaker()
    return ModelRouter(
        pool, breaker, cross_family_fallback=cross_family_fallback
    ), breaker


# ---------------------------------------------------------------------------
# 1. Default OFF — same-family exhaustion raises AllNodesFailedError
# ---------------------------------------------------------------------------


async def test_cross_family_disabled_by_default_even_with_mixed_pool() -> None:
    a = _node("a", family="anthropic", priority=100)
    o = _node("o", family="openai", priority=80)
    clients = {
        "a": _Recording([TransientError("503")]),
        "o": _Recording(["never-called"]),
    }
    router, _ = _build_router([a, o], clients, cross_family_fallback=False)

    with pytest.raises(AllNodesFailedError):
        await router.call([Message(role="user", content="hi")], tools=None)
    assert clients["a"].calls  # anthropic was tried
    assert not clients["o"].calls  # openai was NOT tried — default OFF


# ---------------------------------------------------------------------------
# 2. Opt-in ON — hop happens, messages are transformed
# ---------------------------------------------------------------------------


async def test_cross_family_hop_after_same_family_exhausted() -> None:
    """Two Anthropic nodes fail → OpenAI node receives the adapted prompt."""
    a1 = _node("a1", family="anthropic", priority=100)
    a2 = _node("a2", family="anthropic", priority=90)
    o = _node("o", family="openai", priority=50)
    clients = {
        "a1": _Recording([TransientError("503")]),
        "a2": _Recording([AuthError("401")]),
        "o": _Recording(["openai-rescue"]),
    }
    router, breaker = _build_router([a1, a2, o], clients, cross_family_fallback=True)

    out = await router.call([Message(role="user", content="hello")], tools=None)
    assert out == "openai-rescue"
    # All three were tried in order.
    assert clients["a1"].calls
    assert clients["a2"].calls
    assert clients["o"].calls
    # Breaker accounting: both anthropic failures recorded, openai success recorded.
    assert breaker.get("a1").total_failures == 1
    assert breaker.get("a2").total_failures == 1
    assert breaker.get("o").total_successes == 1


async def test_cross_family_strips_thinking_blocks() -> None:
    """Thinking has no OpenAI analog — must be stripped before the hop."""
    a = _node("a", family="anthropic", priority=100)
    o = _node("o", family="openai", priority=50)
    clients = {
        "a": _Recording([TransientError("503")]),
        "o": _Recording(["ok"]),
    }
    router, _ = _build_router([a, o], clients, cross_family_fallback=True)

    messages = [
        Message(role="user", content="question"),
        Message(
            role="assistant",
            content="answer",
            thinking="SECRET REASONING",
        ),
    ]
    await router.call(messages, tools=None)

    seen_by_openai = clients["o"].calls[0]["messages"]
    # The openai node must not have seen any `thinking` payload.
    for msg in seen_by_openai:
        assert getattr(msg, "thinking", None) in (None, "")
    # The original messages list is untouched (caller view).
    assert messages[1].thinking == "SECRET REASONING"


async def test_cross_family_drops_orphan_tool_calls() -> None:
    """An assistant message with an unanswered tool_call would crash the
    other family — drop the orphan before crossing."""
    a = _node("a", family="anthropic", priority=100)
    o = _node("o", family="openai", priority=50)
    clients = {
        "a": _Recording([TransientError("503")]),
        "o": _Recording(["ok"]),
    }
    router, _ = _build_router([a, o], clients, cross_family_fallback=True)

    orphan_call = ToolCall(
        id="tc-orphan",
        type="function",
        function=FunctionCall(name="calculator", arguments={"a": 1, "b": 2}),
    )
    matched_call = ToolCall(
        id="tc-matched",
        type="function",
        function=FunctionCall(name="echo", arguments={"text": "hi"}),
    )
    messages = [
        Message(role="user", content="go"),
        Message(
            role="assistant",
            content="calling tools",
            tool_calls=[matched_call, orphan_call],
        ),
        # Only one tool result — `tc-orphan` has no answer, so it must
        # be dropped on the cross-family hop.
        Message(
            role="tool",
            content="hi",
            tool_call_id="tc-matched",
            name="echo",
        ),
    ]
    await router.call(messages, tools=None)

    seen = clients["o"].calls[0]["messages"]
    # Find the assistant message in the adapted view.
    adapted_assistant = next(m for m in seen if getattr(m, "role", None) == "assistant")
    assert adapted_assistant.tool_calls is not None
    adapted_ids = [tc.id for tc in adapted_assistant.tool_calls]
    assert adapted_ids == ["tc-matched"], (
        f"orphan tool_call should have been dropped; got {adapted_ids}"
    )


async def test_cross_family_drops_orphan_tool_result() -> None:
    """Symmetric case: tool result whose tool_use was never generated
    must be dropped too (otherwise the other family rejects it)."""
    a = _node("a", family="anthropic", priority=100)
    o = _node("o", family="openai", priority=50)
    clients = {
        "a": _Recording([TransientError("503")]),
        "o": _Recording(["ok"]),
    }
    router, _ = _build_router([a, o], clients, cross_family_fallback=True)

    messages = [
        Message(role="user", content="q"),
        Message(role="assistant", content="answer"),  # no tool_calls
        Message(
            role="tool",
            content="stale result",
            tool_call_id="tc-nobody-asked",
            name="noop",
        ),
    ]
    await router.call(messages, tools=None)

    seen = clients["o"].calls[0]["messages"]
    # No tool messages should be in the adapted list.
    assert not any(getattr(m, "role", None) == "tool" for m in seen)


# ---------------------------------------------------------------------------
# 3. Capability gate
# ---------------------------------------------------------------------------


async def test_cross_family_hop_blocked_when_target_lacks_tool_support() -> None:
    """If the caller passed `tools=[...]` but every cross-family node
    says `supports_tools=False`, the router must NOT blindly hop."""
    a = _node("a", family="anthropic", priority=100, supports_tools=True)
    o = _node("o", family="openai", priority=50, supports_tools=False)
    clients = {
        "a": _Recording([TransientError("503")]),
        "o": _Recording(["never-called"]),
    }
    router, _ = _build_router([a, o], clients, cross_family_fallback=True)

    tools = [{"name": "x", "description": "", "input_schema": {"type": "object"}}]
    with pytest.raises(AllNodesFailedError):
        await router.call([Message(role="user", content="hi")], tools=tools)

    assert not clients["o"].calls  # capability gate stopped the hop


async def test_cross_family_hop_proceeds_when_target_supports_tools() -> None:
    a = _node("a", family="anthropic", priority=100, supports_tools=True)
    o = _node("o", family="openai", priority=50, supports_tools=True)
    clients = {
        "a": _Recording([TransientError("503")]),
        "o": _Recording(["rescue-with-tools"]),
    }
    router, _ = _build_router([a, o], clients, cross_family_fallback=True)

    tools = [{"name": "x", "description": "", "input_schema": {"type": "object"}}]
    out = await router.call([Message(role="user", content="hi")], tools=tools)
    assert out == "rescue-with-tools"


# ---------------------------------------------------------------------------
# 4. ContextOverflow / BadRequest invariants preserved
# ---------------------------------------------------------------------------


async def test_cross_family_does_not_mask_event_time_context_overflow() -> None:
    """Event-time ContextOverflow must still raise to agent, not hop."""
    a = _node("a", family="anthropic", priority=100)
    o = _node("o", family="openai", priority=50)
    clients = {
        "a": _Recording([ContextOverflowError("too long")]),
        "o": _Recording(["never-called"]),
    }
    router, breaker = _build_router([a, o], clients, cross_family_fallback=True)

    with pytest.raises(ContextOverflowError):
        await router.call([Message(role="user", content="hi")], tools=None)
    assert not clients["o"].calls
    # Capacity must not count against node health.
    assert breaker.get("a").consecutive_failures == 0


async def test_cross_family_does_not_mask_bad_request() -> None:
    """BadRequest is a program bug — must raise, not silently cross families."""
    a = _node("a", family="anthropic", priority=100)
    o = _node("o", family="openai", priority=50)
    clients = {
        "a": _Recording([BadRequestError("schema")]),
        "o": _Recording(["never-called"]),
    }
    router, _ = _build_router([a, o], clients, cross_family_fallback=True)

    with pytest.raises(BadRequestError):
        await router.call([Message(role="user", content="hi")], tools=None)
    assert not clients["o"].calls
