"""Tests for ModelPool and NodeHealth/HealthRegistry."""

from __future__ import annotations

import pytest

from mini_agent.llm.ha.errors import ErrorCategory
from mini_agent.llm.ha.health import HealthRegistry, NodeHealth
from mini_agent.llm.ha.models import ModelNode
from mini_agent.llm.ha.pool import ModelPool


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


# --- ModelPool ---------------------------------------------------------------


def test_pool_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one node"):
        ModelPool([])


def test_pool_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="Duplicate node_id"):
        ModelPool([make_node("a"), make_node("a")])


def test_pool_get_and_enabled_filters() -> None:
    pool = ModelPool([make_node("a"), make_node("b", enabled=False), make_node("c")])
    assert pool.get("a").node_id == "a"
    assert "a" in pool
    assert "zzz" not in pool
    assert {n.node_id for n in pool.enabled()} == {"a", "c"}
    assert {n.node_id for n in pool.all()} == {"a", "b", "c"}


def test_pool_get_unknown_raises_keyerror() -> None:
    pool = ModelPool([make_node("a")])
    with pytest.raises(KeyError):
        pool.get("missing")


# --- NodeHealth --------------------------------------------------------------


def test_health_starts_healthy() -> None:
    health = NodeHealth("n1", failure_threshold=3)
    assert health.is_healthy is True
    snap = health.snapshot()
    assert snap.consecutive_failures == 0
    assert snap.is_healthy is True


def test_health_becomes_unhealthy_after_threshold() -> None:
    health = NodeHealth("n1", failure_threshold=3)
    for _ in range(2):
        health.record_failure(ErrorCategory.TRANSIENT, Exception("boom"))
    assert health.is_healthy is True  # 2 < 3

    health.record_failure(ErrorCategory.TRANSIENT, Exception("boom"))
    assert health.is_healthy is False
    snap = health.snapshot()
    assert snap.consecutive_failures == 3
    assert snap.total_failures == 3
    assert snap.last_error_category == "transient"
    assert "boom" in (snap.last_error_message or "")


def test_health_success_resets_consecutive_failures() -> None:
    health = NodeHealth("n1", failure_threshold=2)
    health.record_failure(ErrorCategory.TRANSIENT, Exception("x"))
    health.record_failure(ErrorCategory.TRANSIENT, Exception("x"))
    assert health.is_healthy is False

    health.record_success()
    assert health.is_healthy is True
    snap = health.snapshot()
    assert snap.consecutive_failures == 0
    assert snap.consecutive_successes == 1
    assert snap.last_error_category is None


def test_health_error_message_is_truncated() -> None:
    health = NodeHealth("n1")
    long_msg = "x" * 1000
    health.record_failure(ErrorCategory.UNKNOWN, Exception(long_msg))
    snap = health.snapshot()
    assert snap.last_error_message is not None
    assert len(snap.last_error_message) <= 200


# --- HealthRegistry ---------------------------------------------------------


def test_registry_lazily_creates_health() -> None:
    reg = HealthRegistry(failure_threshold=2)
    h1 = reg.get("n1")
    h1_again = reg.get("n1")
    assert h1 is h1_again  # same instance

    h1.record_failure(ErrorCategory.TRANSIENT, Exception("x"))
    snapshots = reg.snapshots()
    assert "n1" in snapshots
    assert snapshots["n1"].consecutive_failures == 1
