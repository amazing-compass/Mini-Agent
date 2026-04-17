"""Tests for ModelRouter.

These tests simulate per-node call outcomes via a small fake call site — no
real LLM traffic is required. The goal is to cover failover behavior,
health tracking, and non-switchable-error short-circuiting.
"""

from __future__ import annotations

import pytest

from mini_agent.llm.ha.errors import ErrorCategory, PoolExhaustedError
from mini_agent.llm.ha.health import HealthRegistry
from mini_agent.llm.ha.models import ModelNode
from mini_agent.llm.ha.pool import ModelPool
from mini_agent.llm.ha.router import ModelRouter


def make_node(node_id: str, priority: int = 100, enabled: bool = True) -> ModelNode:
    return ModelNode(
        node_id=node_id,
        provider="anthropic",
        protocol_family="anthropic",
        api_key="sk-test",
        api_base="https://example.test",
        model="test-model",
        priority=priority,
        enabled=enabled,
    )


def make_router(nodes: list[ModelNode], failure_threshold: int = 3) -> tuple[ModelRouter, HealthRegistry]:
    pool = ModelPool(nodes)
    registry = HealthRegistry(failure_threshold=failure_threshold)
    router = ModelRouter(pool, registry)
    return router, registry


class _HTTPStatusError(Exception):
    def __init__(self, status_code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.status_code = status_code


async def test_primary_success_selects_primary() -> None:
    router, registry = make_router([make_node("a", 100), make_node("b", 50)])

    calls: list[str] = []

    async def fn(node: ModelNode) -> str:
        calls.append(node.node_id)
        return f"hello from {node.node_id}"

    result, decision = await router.execute(fn)
    assert result == "hello from a"
    assert decision.selected_node_id == "a"
    assert decision.fallback_level == 0
    assert calls == ["a"]
    assert registry.get("a").total_successes == 1


async def test_falls_over_on_transient_error() -> None:
    router, registry = make_router([make_node("a", 100), make_node("b", 50)])

    calls: list[str] = []

    async def fn(node: ModelNode) -> str:
        calls.append(node.node_id)
        if node.node_id == "a":
            raise _HTTPStatusError(503, "gateway down")
        return "backup"

    failovers: list[tuple[str, str, ErrorCategory]] = []
    router.on_failover = lambda f, n, exc, cat: failovers.append((f.node_id, n.node_id, cat))

    result, decision = await router.execute(fn)
    assert result == "backup"
    assert decision.selected_node_id == "b"
    assert decision.fallback_level == 1
    assert calls == ["a", "b"]
    assert failovers == [("a", "b", ErrorCategory.TRANSIENT)]
    assert registry.get("a").total_failures == 1
    assert registry.get("a").consecutive_failures == 1
    assert registry.get("b").total_successes == 1


async def test_raises_immediately_on_malformed_request() -> None:
    """REQUEST_MALFORMED (400) must NOT cascade across nodes — it's a program bug."""
    router, registry = make_router([make_node("a"), make_node("b")])

    calls: list[str] = []

    async def fn(node: ModelNode) -> str:
        calls.append(node.node_id)
        raise _HTTPStatusError(400, "invalid tool schema")

    with pytest.raises(_HTTPStatusError) as excinfo:
        await router.execute(fn)
    assert excinfo.value.status_code == 400
    assert calls == ["a"]  # node b was NOT tried
    # Fix #1: caller-side errors must NOT poison node health.
    assert registry.get("a").consecutive_failures == 0
    assert registry.get("a").total_failures == 0


async def test_capacity_errors_do_not_poison_health() -> None:
    """Repeated context-overflow 400s must not sideline the node."""
    router, registry = make_router(
        [make_node("a", priority=100), make_node("b", priority=50)],
        failure_threshold=2,
    )

    async def fail_capacity(node: ModelNode) -> str:
        raise _HTTPStatusError(400, "maximum context length exceeded")

    # Trigger five capacity errors on the primary — more than enough to
    # cross any failure_threshold.
    for _ in range(5):
        with pytest.raises(_HTTPStatusError):
            await router.execute(fail_capacity)

    assert registry.get("a").consecutive_failures == 0
    assert registry.get("a").is_healthy is True

    # A subsequent valid request must still land on the high-priority node.
    async def ok(node: ModelNode) -> str:
        return node.node_id

    result, decision = await router.execute(ok)
    assert result == "a"
    assert decision.selected_node_id == "a"


async def test_404_triggers_failover_not_immediate_raise() -> None:
    """Fix P3: 404 is a node-local config problem, NOT a malformed request.

    The router must try the next node instead of aborting like it does for
    REQUEST_MALFORMED.
    """
    router, registry = make_router([make_node("a", 100), make_node("b", 50)])

    calls: list[str] = []

    async def fn(node: ModelNode) -> str:
        calls.append(node.node_id)
        if node.node_id == "a":
            raise _HTTPStatusError(404, "model MiniMax-M2.1 not found on this account")
        return "served by backup"

    result, decision = await router.execute(fn)
    assert result == "served by backup"
    assert decision.selected_node_id == "b"
    assert decision.fallback_level == 1
    assert calls == ["a", "b"]
    assert registry.get("a").last_error_category == ErrorCategory.NODE_UNAVAILABLE


async def test_auth_errors_still_record_failure() -> None:
    """Auth is switchable AND attributable to the node — record_failure must fire."""
    router, registry = make_router([make_node("a"), make_node("b")])

    async def fn(node: ModelNode) -> str:
        if node.node_id == "a":
            raise _HTTPStatusError(401, "bad key")
        return "ok"

    await router.execute(fn)
    # Fix #1 contract: SWITCHABLE errors still accrue against node health.
    assert registry.get("a").consecutive_failures == 1
    assert registry.get("a").last_error_category == ErrorCategory.AUTH


async def test_failover_on_auth_then_success() -> None:
    router, registry = make_router([make_node("a", 100), make_node("b", 50)])

    async def fn(node: ModelNode) -> str:
        if node.node_id == "a":
            raise _HTTPStatusError(401, "bad key")
        return "ok"

    result, decision = await router.execute(fn)
    assert result == "ok"
    assert decision.selected_node_id == "b"
    assert registry.get("a").last_error_category == ErrorCategory.AUTH


async def test_all_nodes_fail_raises_pool_exhausted() -> None:
    router, _ = make_router([make_node("a"), make_node("b")])

    async def fn(node: ModelNode) -> str:
        raise _HTTPStatusError(503, f"dead on {node.node_id}")

    with pytest.raises(PoolExhaustedError) as excinfo:
        await router.execute(fn)

    err = excinfo.value
    assert len(err.attempts) == 2
    attempted = [nid for nid, _, _ in err.attempts]
    assert attempted == ["a", "b"]
    assert err.last_category == ErrorCategory.TRANSIENT
    assert isinstance(err.last_exception, _HTTPStatusError)


async def test_priority_ordering_prefers_higher_priority() -> None:
    # Add nodes in reverse priority order to confirm router sorts them.
    router, _ = make_router(
        [make_node("low", priority=10), make_node("high", priority=100), make_node("mid", priority=50)]
    )

    seen: list[str] = []

    async def fn(node: ModelNode) -> str:
        seen.append(node.node_id)
        if node.node_id != "mid":
            raise _HTTPStatusError(503)
        return "mid-ok"

    result, decision = await router.execute(fn)
    assert result == "mid-ok"
    # Order should be: high (priority 100), mid (50), low (10).
    assert seen == ["high", "mid"]
    assert decision.fallback_level == 1


async def test_unhealthy_nodes_are_ordered_last_but_still_tried() -> None:
    router, registry = make_router(
        [make_node("primary", priority=100), make_node("backup", priority=50)],
        failure_threshold=2,
    )

    # Poison primary's health so it's marked unhealthy before the call.
    registry.get("primary").record_failure(ErrorCategory.TRANSIENT, Exception("poison"))
    registry.get("primary").record_failure(ErrorCategory.TRANSIENT, Exception("poison"))
    assert registry.get("primary").is_healthy is False

    seen: list[str] = []

    async def fn(node: ModelNode) -> str:
        seen.append(node.node_id)
        return f"ok-{node.node_id}"

    result, decision = await router.execute(fn)
    # Healthy backup should be preferred over unhealthy primary.
    assert result == "ok-backup"
    assert decision.selected_node_id == "backup"
    assert seen == ["backup"]


async def test_unknown_strategy_rejected() -> None:
    pool = ModelPool([make_node("a")])
    registry = HealthRegistry()
    with pytest.raises(ValueError, match="Unsupported routing strategy"):
        ModelRouter(pool, registry, strategy="round-robin")
