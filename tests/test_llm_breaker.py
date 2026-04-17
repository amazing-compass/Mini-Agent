"""Tests for the Phase 2 3-state circuit breaker (SimpleBreaker).

Covers the CQS surface:
- is_passable / is_serving / on_attempt semantics
- closed → open transition on consecutive failures
- open → half-open transition gated by on_attempt + cooldown
- half-open → closed on success
- half-open → open (and cooldown refresh) on failure
"""

from __future__ import annotations

import time

import pytest

from mini_agent.llm.ha.errors import ErrorCategory
from mini_agent.llm.ha.health import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    NodeHealth,
    SimpleBreaker,
)


def _fail(health: NodeHealth, n: int = 1) -> None:
    for _ in range(n):
        health.record_failure(ErrorCategory.TRANSIENT, Exception("boom"))


# ---------------------------------------------------------------------------
# NodeHealth
# ---------------------------------------------------------------------------


def test_new_health_is_closed_and_passable() -> None:
    h = NodeHealth("n", failure_threshold=3, cooldown_seconds=60)
    assert h.circuit_state == CLOSED
    assert h.is_passable() is True
    assert h.is_serving() is True
    assert h.is_healthy is True


def test_failure_threshold_opens_the_breaker() -> None:
    h = NodeHealth("n", failure_threshold=3, cooldown_seconds=60)
    _fail(h, 2)
    assert h.circuit_state == CLOSED
    _fail(h)
    assert h.circuit_state == OPEN
    # open → is_serving is False, is_passable also False until cooldown
    assert h.is_serving() is False
    assert h.is_passable() is False


def test_open_becomes_passable_after_cooldown_but_not_serving() -> None:
    h = NodeHealth("n", failure_threshold=2, cooldown_seconds=0)  # instant cooldown
    _fail(h, 2)
    assert h.circuit_state == OPEN
    # Cooldown already elapsed → passable=True (eligible for probe) but
    # is_serving stays False until on_attempt transitions to half-open.
    assert h.is_passable() is True
    assert h.is_serving() is False


def test_on_attempt_transitions_open_to_half_open() -> None:
    h = NodeHealth("n", failure_threshold=2, cooldown_seconds=0)
    _fail(h, 2)
    h.on_attempt()
    assert h.circuit_state == HALF_OPEN
    # Phase 2 exclusivity: once on_attempt claimed the probe slot, concurrent
    # callers must see the node as NOT passable / NOT serving until the probe
    # resolves (record_success / record_failure).
    assert h.is_serving() is False
    assert h.is_passable() is False


def test_on_attempt_is_noop_when_not_open() -> None:
    h = NodeHealth("n", failure_threshold=3, cooldown_seconds=0)
    h.on_attempt()  # closed — should stay closed
    assert h.circuit_state == CLOSED
    _fail(h, 3)
    h.on_attempt()
    assert h.circuit_state == HALF_OPEN
    assert h._probe_in_flight is True
    h.on_attempt()  # second on_attempt on already-half-open — no state change
    assert h.circuit_state == HALF_OPEN
    assert h._probe_in_flight is True


def test_half_open_success_closes_breaker_and_resets_counters() -> None:
    h = NodeHealth("n", failure_threshold=2, cooldown_seconds=0)
    _fail(h, 2)
    h.on_attempt()
    assert h.circuit_state == HALF_OPEN
    assert h.is_passable() is False  # probe in flight
    h.record_success()
    assert h.circuit_state == CLOSED
    assert h.consecutive_failures == 0
    assert h.cooldown_until is None
    assert h.is_passable() is True  # probe slot released


def test_half_open_failure_reopens_immediately_and_refreshes_cooldown() -> None:
    h = NodeHealth("n", failure_threshold=3, cooldown_seconds=0)
    # Cross the threshold, then probe.
    _fail(h, 3)
    h.on_attempt()
    assert h.circuit_state == HALF_OPEN
    # A half-open failure must re-open, even though consecutive_failures
    # might not matter here — the design mandates an explicit transition.
    initial_cooldown = h.cooldown_until
    h.record_failure(ErrorCategory.TRANSIENT, Exception("still broken"))
    assert h.circuit_state == OPEN
    assert h.cooldown_until is not None
    # Cooldown got refreshed.
    if initial_cooldown is not None:
        assert h.cooldown_until >= initial_cooldown


def test_success_closes_a_breaker_that_was_open_directly() -> None:
    """Direct record_success on an open breaker should recover (defensive).

    The normal path is on_attempt → success → closed, but some test/direct
    callers skip on_attempt. A success means the node works — don't leave
    the breaker stuck open with counters zeroed but state wrong.
    """
    h = NodeHealth("n", failure_threshold=2, cooldown_seconds=60)
    _fail(h, 2)
    assert h.circuit_state == OPEN
    h.record_success()
    assert h.circuit_state == CLOSED


# ---------------------------------------------------------------------------
# SimpleBreaker
# ---------------------------------------------------------------------------


def test_simple_breaker_lazily_creates_nodes() -> None:
    b = SimpleBreaker(failure_threshold=2, cooldown_seconds=60)
    assert b.is_passable("unseen-node") is True  # defaults to healthy
    snap = b.snapshots()
    assert "unseen-node" in snap
    assert snap["unseen-node"].circuit_state == CLOSED


def test_simple_breaker_flow_end_to_end() -> None:
    b = SimpleBreaker(failure_threshold=2, cooldown_seconds=0)
    # cross threshold
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("x"))
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("x"))
    assert b.is_serving("a") is False  # open, no probe yet
    assert b.is_passable("a") is True  # cooldown=0, eligible
    b.on_attempt("a")  # now half-open, probe locked
    # Phase 2 exclusivity: while the probe is in flight, both predicates
    # are False so concurrent callers don't flood a still-broken node.
    assert b.is_serving("a") is False
    assert b.is_passable("a") is False
    b.record_success("a")  # fully recovered, probe slot released
    assert b.is_serving("a") is True
    assert b.is_passable("a") is True
    snap = b.snapshots()["a"]
    assert snap.circuit_state == CLOSED
    assert snap.consecutive_failures == 0


def test_cooldown_actually_blocks_passage() -> None:
    """A real cooldown window must keep `is_passable` False until it elapses."""
    b = SimpleBreaker(failure_threshold=2, cooldown_seconds=0.5)
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("x"))
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("x"))
    assert b.is_passable("a") is False  # cooldown not elapsed
    time.sleep(0.55)
    assert b.is_passable("a") is True


def test_half_open_probe_is_exclusive_against_concurrent_callers() -> None:
    """Regression for the concurrency hole Codex flagged.

    When ACP shares one LLMClient across sessions, two async tasks can
    both see an OPEN+cooldown-elapsed node as `is_passable=True`. The
    first caller wins the transition via `on_attempt`; any later caller
    that arrives between that transition and `record_*` must see the
    node as NOT passable so it doesn't stampede a still-broken node
    during its probe window.
    """
    b = SimpleBreaker(failure_threshold=2, cooldown_seconds=0)
    # Push node into OPEN + cooldown elapsed.
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("x"))
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("x"))
    # Before on_attempt: both callers would see the node as passable.
    assert b.is_passable("a") is True

    # First caller takes the probe slot.
    b.on_attempt("a")
    # Second / third / Nth caller that arrives AFTER must skip this
    # node — the probe slot is occupied.
    assert b.is_passable("a") is False
    # internal_call-style callers also stay out.
    assert b.is_serving("a") is False

    # The probe resolves (success or failure), slot released.
    b.record_success("a")
    assert b.is_passable("a") is True


def test_half_open_probe_is_exclusive_on_probe_failure_too() -> None:
    """record_failure must also release the probe slot and open again cleanly."""
    b = SimpleBreaker(failure_threshold=1, cooldown_seconds=0)
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("x"))
    assert b.is_passable("a") is True  # cooldown elapsed

    b.on_attempt("a")  # probe slot claimed
    assert b.is_passable("a") is False

    # Probe fails → node re-opens, cooldown refreshed, slot released.
    b.record_failure("a", ErrorCategory.TRANSIENT, Exception("y"))
    # The slot is released, but the node is OPEN again with a fresh cooldown=0
    # so another caller may attempt immediately. That's intentional: the
    # exclusivity covers the PROBE WINDOW, not the post-failure open phase.
    snap = b.snapshots()["a"]
    assert snap.circuit_state == OPEN
