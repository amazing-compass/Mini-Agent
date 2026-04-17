"""Tests for Phase 2 ModelRouter.call and .internal_call (three-bucket path).

These tests use a small fake LLMClient so no real traffic is required.
The goal is to pin the bucket-classification logic, the breaker's
on_attempt / record_* wiring, and the internal_call bypass semantics.
"""

from __future__ import annotations

import pytest

from mini_agent.llm.ha.errors import (
    AllNodesFailedError,
    AuthError,
    BadRequestError,
    ContextOverflowError,
    NoAvailableNodeError,
    RateLimitError,
    TransientError,
)
from mini_agent.llm.ha.health import CLOSED, HALF_OPEN, OPEN, SimpleBreaker
from mini_agent.llm.ha.models import ModelNode
from mini_agent.llm.ha.pool import ModelPool
from mini_agent.llm.ha.router import ModelRouter
from mini_agent.schema import Message


class FakeClient:
    """Records each `generate` call + returns a scripted outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)  # list of value-or-Exception
        self.calls: list[dict] = []

    async def generate(self, messages, tools=None, *, max_tokens=None):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        if not self.outcomes:
            raise RuntimeError("FakeClient ran out of scripted outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_node(node_id, priority=100, context_window=128_000, max_output=4096, enabled=True):
    return ModelNode(
        node_id=node_id,
        provider="anthropic",
        protocol_family="anthropic",
        api_key="sk-test",
        api_base="https://example.test",
        model="m",
        priority=priority,
        context_window=context_window,
        max_output_tokens=max_output,
        enabled=enabled,
    )


def _make(router_nodes, clients, failure_threshold=3, cooldown=60):
    pool = ModelPool(router_nodes)
    for nid, client in clients.items():
        pool.set_client(nid, client)
    breaker = SimpleBreaker(failure_threshold=failure_threshold, cooldown_seconds=cooldown)
    return ModelRouter(pool, breaker), breaker


# ---------------------------------------------------------------------------
# Router.call — bucket classification + failover
# ---------------------------------------------------------------------------


async def test_call_routes_to_highest_priority_healthy_fits() -> None:
    high = make_node("high", priority=100)
    low = make_node("low", priority=50)
    clients = {
        "high": FakeClient(["resp-high"]),
        "low": FakeClient(["resp-low"]),
    }
    router, _ = _make([high, low], clients)

    out = await router.call([Message(role="user", content="hi")], tools=None)
    assert out == "resp-high"
    assert clients["high"].calls and not clients["low"].calls


async def test_call_fails_over_on_transient_error() -> None:
    high = make_node("high", priority=100)
    low = make_node("low", priority=50)
    clients = {
        "high": FakeClient([TransientError("503")]),
        "low": FakeClient(["backup-ok"]),
    }
    router, breaker = _make([high, low], clients)
    out = await router.call([Message(role="user", content="hi")], tools=None)
    assert out == "backup-ok"
    # high's failure recorded; low's success recorded
    assert breaker.get("high").consecutive_failures == 1
    assert breaker.get("low").consecutive_successes == 1


async def test_call_does_not_fail_over_on_bad_request() -> None:
    """BadRequestError is a program bug — it must NOT silently hop nodes."""
    high = make_node("high", priority=100)
    low = make_node("low", priority=50)
    clients = {
        "high": FakeClient([BadRequestError("bad schema")]),
        "low": FakeClient(["never-called"]),
    }
    router, breaker = _make([high, low], clients)
    with pytest.raises(BadRequestError):
        await router.call([Message(role="user", content="hi")], tools=None)
    assert not clients["low"].calls
    # BadRequest must NOT count against node health.
    assert breaker.get("high").consecutive_failures == 0


async def test_call_propagates_context_overflow_without_failover() -> None:
    """Event-time ContextOverflow: raise to agent, don't switch nodes."""
    high = make_node("high", priority=100)
    low = make_node("low", priority=50)
    clients = {
        "high": FakeClient([ContextOverflowError("too long")]),
        "low": FakeClient(["never-called"]),
    }
    router, breaker = _make([high, low], clients)
    with pytest.raises(ContextOverflowError):
        await router.call([Message(role="user", content="hi")], tools=None)
    assert not clients["low"].calls
    # Capacity errors must NOT count against node health.
    assert breaker.get("high").consecutive_failures == 0


async def test_call_raises_context_overflow_preflight_when_no_node_fits() -> None:
    """If every healthy node is too small, raise ContextOverflowError (agent must compress)."""
    small = make_node("tiny", priority=100, context_window=1000)
    clients = {"tiny": FakeClient(["never-called"])}
    router, _ = _make([small], clients)

    # Fabricate a payload guaranteed to bust a 1k window.
    huge = [Message(role="user", content="x " * 5000)]
    with pytest.raises(ContextOverflowError):
        await router.call(huge, tools=None)
    assert not clients["tiny"].calls


async def test_call_raises_all_nodes_failed_when_all_unhealthy() -> None:
    """All candidates circuit-open → AllNodesFailedError, not ContextOverflow."""
    node = make_node("only", priority=100)
    clients = {"only": FakeClient(["never-called"])}
    router, breaker = _make([node], clients, failure_threshold=1, cooldown=60)
    # Force OPEN: one failure at threshold=1 + long cooldown.
    breaker.record_failure("only", __import__("mini_agent.llm.ha.errors", fromlist=["ErrorCategory"]).ErrorCategory.TRANSIENT, Exception("boom"))
    with pytest.raises(AllNodesFailedError):
        await router.call([Message(role="user", content="hi")], tools=None)
    assert not clients["only"].calls


async def test_call_raises_no_available_node_when_pool_empty_enabled() -> None:
    node = make_node("off", priority=100, enabled=False)
    clients = {}
    router, _ = _make([node], clients)
    with pytest.raises(NoAvailableNodeError):
        await router.call([Message(role="user", content="hi")], tools=None)


async def test_call_computes_dynamic_max_tokens() -> None:
    """Router should ask the node for min(max_output_tokens, available) tokens."""
    node = make_node("n", context_window=4000, max_output=2048)
    clients = {"n": FakeClient(["ok"])}
    router, _ = _make([node], clients)

    await router.call([Message(role="user", content="hi")], tools=None)
    call = clients["n"].calls[0]
    # Context window is small (4000). Estimate is tiny; available ≈ window - margin.
    # actual = min(2048, available).
    assert call["max_tokens"] is not None
    assert call["max_tokens"] <= 2048


async def test_fit_check_honors_small_max_output_tokens() -> None:
    """Regression: a node with max_output_tokens < MIN_USEFUL_OUTPUT (2048)
    must not be pre-flight-rejected when the request would actually fit
    under the smaller output budget. The fit check must use
    `min(MIN_USEFUL_OUTPUT, node.max_output_tokens)` so that it stays
    consistent with the main loop's `min(node.max_output_tokens, available)`.
    """
    # Window sized so the request fits with a 1k output budget but NOT
    # with the default 2k floor.
    # estimate(msg) + 1024 + 1024 margin ≈ X → context_window = X + small slack
    msg = [Message(role="user", content="hi there")]
    # Force a known estimate envelope via tiny message.
    node = make_node("small-out", context_window=3000, max_output=1024)
    clients = {"small-out": FakeClient(["ok"])}
    router, _ = _make([node], clients)
    # Should NOT raise ContextOverflowError — fit must allow this node.
    out = await router.call(msg, tools=None)
    assert out == "ok"
    assert clients["small-out"].calls[0]["max_tokens"] <= 1024


async def test_call_drives_breaker_on_attempt_on_cooldown_probe() -> None:
    """Open-with-cooldown-elapsed nodes should transition to half-open via on_attempt."""
    node = make_node("n", priority=100)
    clients = {"n": FakeClient(["ok"])}
    router, breaker = _make([node], clients, failure_threshold=1, cooldown=0)

    # Crash once to open the breaker; cooldown=0 means immediately passable.
    from mini_agent.llm.ha.errors import ErrorCategory

    breaker.record_failure("n", ErrorCategory.TRANSIENT, Exception("x"))
    assert breaker.get("n").circuit_state == OPEN
    assert breaker.is_passable("n") is True  # cooldown elapsed

    # A fresh call should probe (on_attempt) → half-open → success → closed.
    await router.call([Message(role="user", content="hi")], tools=None)
    assert breaker.get("n").circuit_state == CLOSED


# ---------------------------------------------------------------------------
# Router.internal_call
# ---------------------------------------------------------------------------


async def test_internal_call_uses_is_serving_not_is_passable() -> None:
    """Open-with-cooldown-elapsed must NOT be eligible for internal_call."""
    node = make_node("n", priority=100)
    clients = {"n": FakeClient(["never-called"])}
    router, breaker = _make([node], clients, failure_threshold=1, cooldown=0)

    from mini_agent.llm.ha.errors import ErrorCategory

    breaker.record_failure("n", ErrorCategory.TRANSIENT, Exception("x"))
    # cooldown=0 → passable True, but serving False — internal_call must refuse.
    assert breaker.is_passable("n") is True
    assert breaker.is_serving("n") is False

    with pytest.raises(NoAvailableNodeError):
        await router.internal_call([Message(role="user", content="hi")])


async def test_internal_call_does_not_record_success_or_failure() -> None:
    node = make_node("n", priority=100)
    ok_client = FakeClient(["summary-ok"])
    router, breaker = _make([node], {"n": ok_client})
    initial_successes = breaker.get("n").total_successes

    await router.internal_call([Message(role="user", content="hi")])
    # No record_success side-effect.
    assert breaker.get("n").total_successes == initial_successes


async def test_internal_call_does_not_trigger_on_attempt() -> None:
    """internal_call should never push state machine forward."""
    node = make_node("n", priority=100)
    clients = {"n": FakeClient(["summary-ok"])}
    router, breaker = _make([node], clients)
    # Breaker starts closed. internal_call is a plain passthrough.
    await router.internal_call([Message(role="user", content="hi")])
    assert breaker.get("n").circuit_state == CLOSED


async def test_internal_call_failure_does_not_failover() -> None:
    high = make_node("high", priority=100)
    low = make_node("low", priority=50)
    clients = {
        "high": FakeClient([TransientError("503")]),
        "low": FakeClient(["never-called"]),
    }
    router, breaker = _make([high, low], clients)
    with pytest.raises(TransientError):
        await router.internal_call([Message(role="user", content="hi")])
    assert not clients["low"].calls
    # And no health accounting either.
    assert breaker.get("high").consecutive_failures == 0
