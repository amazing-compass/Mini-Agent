"""Tests for Agent._generate_with_overflow_recovery.

These regression tests use a FakeRouter so no real traffic is needed.
The goal is to pin the exact behavior Codex flagged: a ContextOverflow
raised by the router must *actually* trigger compression, even when the
agent's local `_estimate_tokens()` happens to be below `token_limit`
(most commonly because large tool schemas are invisible to that
estimator).

Phase 3 updated the Agent constructor to take `router=` directly, so
the fake now speaks the router's `call` / `internal_call` surface.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mini_agent.agent import Agent
from mini_agent.llm.ha.errors import ContextOverflowError
from mini_agent.schema import LLMResponse, Message


class FakeRouter:
    """Scriptable router: first call overflows, second call succeeds.

    Only implements the parts Agent touches: `call()` and the
    `internal_call()` bypass used by L4 summaries.
    """

    def __init__(self, *, overflow_times: int = 1) -> None:
        self._generate_calls = 0
        self._overflow_times = overflow_times
        self.internal_calls = 0

    async def call(self, messages, tools=None):
        self._generate_calls += 1
        if self._generate_calls <= self._overflow_times:
            raise ContextOverflowError("router pre-flight: no healthy node fits")
        return LLMResponse(
            content="done",
            thinking=None,
            tool_calls=None,
            finish_reason="stop",
            usage=None,
        )

    async def internal_call(self, messages, tools=None):
        self.internal_calls += 1
        return LLMResponse(
            content=(
                "## Completed Work\n- tests ran\n\n"
                "## Active Files\n- none\n\n"
                "## Key Findings\n- none\n\n"
                "## Pending / TODO\n- nothing"
            ),
            thinking=None,
            tool_calls=None,
            finish_reason="stop",
            usage=None,
        )

    @property
    def generate_calls(self) -> int:
        return self._generate_calls


# Back-compat alias so existing assertions naming FakeLLMClient keep reading cleanly.
FakeLLMClient = FakeRouter


def _make_agent(workspace: str, token_limit: int, fake: FakeRouter) -> Agent:
    return Agent(
        router=fake,  # duck-typed, Agent only reads `.call` / `.internal_call`
        system_prompt="You are a test agent.",
        tools=[],
        max_steps=1,
        workspace_dir=workspace,
        token_limit=token_limit,
    )


def _seed_live_messages(agent: Agent, rounds: int = 4) -> None:
    """Populate agent.live_messages with N user/assistant round pairs so
    `_maybe_run_compaction` has something to fold."""
    for i in range(rounds):
        agent.live_messages.append(Message(role="user", content=f"user turn {i}"))
        agent.live_messages.append(
            Message(role="assistant", content=f"assistant turn {i}")
        )


async def test_overflow_recovery_runs_l4_even_when_local_estimate_fits() -> None:
    """Regression: Codex P2.

    When the router overflows but agent's `_estimate_tokens() <= token_limit`,
    the old code would skip `_maybe_run_compaction()` and retry the same prompt.
    The new path must call `_maybe_run_compaction()` unconditionally on recovery.
    """
    with tempfile.TemporaryDirectory() as ws:
        fake = FakeLLMClient(overflow_times=1)
        # token_limit is deliberately huge so `_estimate_tokens()` is
        # way under — the old gate `if _estimate_tokens() > token_limit`
        # would have been False, skipping the summary call.
        agent = _make_agent(ws, token_limit=100_000_000, fake=fake)
        _seed_live_messages(agent, rounds=4)

        result = await agent._generate_with_overflow_recovery(tool_list=[])

        assert result.content == "done"
        # Summary invoked exactly once → internal_call was used by
        # _run_cache_aligned_summary.
        assert fake.internal_calls == 1, (
            f"expected 1 internal_call for cache-aligned summary, got {fake.internal_calls}"
        )
        # Two generate attempts: the overflowing one + the successful retry.
        assert fake.generate_calls == 2


async def test_overflow_recovery_third_overflow_propagates() -> None:
    """If compression + emergency-truncation + retry still overflows, surface it.

    IMPROVEMENT_04 §3.6 defines a 3-attempt recovery:
        call -> forced compact -> call -> emergency truncate -> call.
    If all three attempts overflow the agent has nothing left, so we
    propagate.
    """
    with tempfile.TemporaryDirectory() as ws:
        fake = FakeLLMClient(overflow_times=3)  # all three attempts overflow
        agent = _make_agent(ws, token_limit=100_000_000, fake=fake)
        _seed_live_messages(agent, rounds=4)

        with pytest.raises(ContextOverflowError):
            await agent._generate_with_overflow_recovery(tool_list=[])

        # Exactly three attempts — no silent loop.
        assert fake.generate_calls == 3


async def test_overflow_recovery_happy_path_no_compression_needed() -> None:
    """If the first `generate` succeeds, no compression and no retry."""
    with tempfile.TemporaryDirectory() as ws:
        fake = FakeLLMClient(overflow_times=0)
        agent = _make_agent(ws, token_limit=100_000_000, fake=fake)
        _seed_live_messages(agent, rounds=4)

        result = await agent._generate_with_overflow_recovery(tool_list=[])

        assert result.content == "done"
        assert fake.generate_calls == 1
        assert fake.internal_calls == 0  # L4 never ran


async def test_safe_generate_is_the_public_entry_point_for_drivers() -> None:
    """Regression for Codex P2: ACP (and any other Agent-owning driver) must
    have a public way to invoke the LLM with overflow recovery. Phase 2
    exposes `Agent.safe_generate` for exactly this reason.

    Prior to the fix, ACP called `agent.llm.generate(...)` directly and
    was NOT covered by `_generate_with_overflow_recovery`, so long
    sessions or large tool schemas would surface `ContextOverflowError`
    as a hard crash.
    """
    with tempfile.TemporaryDirectory() as ws:
        fake = FakeLLMClient(overflow_times=1)
        agent = _make_agent(ws, token_limit=100_000_000, fake=fake)
        _seed_live_messages(agent, rounds=4)

        # The public `safe_generate` should behave identically to the
        # internal helper — overflow triggers L4, retry succeeds.
        result = await agent.safe_generate(tool_list=[])
        assert result.content == "done"
        assert fake.internal_calls == 1  # L4 ran
        assert fake.generate_calls == 2
