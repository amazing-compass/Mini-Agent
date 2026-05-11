"""Integration tests for the IMPROVEMENT_04 cache-aware compaction.

These tests use a scriptable fake router (no live API). They cover:
- ingest-time truncation in ``_add_tool_message``
- ``live_messages`` invariant: messages already appended are never
  byte-level mutated by the normal pipeline
- ``_render_system_blocks`` breakpoint placement (BP #1 / BP #2 promotion)
- ``_maybe_run_compaction`` replacing ``current_summary``
- ``_run_cache_aligned_summary`` forwarding tools to ``internal_call``
- ``_generate_with_overflow_recovery`` triggering forced compaction
- summary-failure handling (normal path defers, forced path falls back)
- ``/clear`` resetting all compaction state
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mini_agent.agent import Agent
from mini_agent.compaction import CompactionDecision, CompactionPolicy
from mini_agent.llm.ha import ModelRouter
from mini_agent.llm.ha.errors import ContextOverflowError
from mini_agent.schema import FunctionCall, LLMResponse, Message, ToolCall


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class ScriptedRouter:
    """Minimal fake router with .call / .internal_call / .peek_primary_node."""

    def __init__(
        self,
        *,
        call_responses: list[Any] | None = None,
        internal_responses: list[Any] | None = None,
    ) -> None:
        self.call_responses: list[Any] = list(call_responses or [])
        self.internal_responses: list[Any] = list(internal_responses or [])
        self.calls: list[dict] = []
        self.internal_calls: list[dict] = []

    async def call(self, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.call_responses:
            return LLMResponse(content="done", finish_reason="stop")
        nxt = self.call_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def internal_call(self, messages, tools=None):
        self.internal_calls.append({"messages": list(messages), "tools": tools})
        if not self.internal_responses:
            return LLMResponse(
                content=(
                    "## Completed Work\n- did stuff\n\n"
                    "## Active Files\n- f.py\n\n"
                    "## Key Findings\n- found things\n\n"
                    "## Pending / TODO\n- more"
                ),
                finish_reason="stop",
            )
        nxt = self.internal_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def peek_primary_node(self):
        return None  # exercises the pricing fallback path


def _heavy_round(text: str, payload: int = 2_000) -> list[Message]:
    """Build a user-round whose assistant carries a chunky payload."""
    words = " ".join(f"w{i:04d}" for i in range(payload))
    return [
        Message(role="user", content=text),
        Message(role="assistant", content=words),
    ]


def _make_agent(workspace: str, router, *, token_limit: int = 100_000_000) -> Agent:
    return Agent(
        router=router,
        system_prompt="You are a test agent.",
        tools=[],
        max_steps=1,
        workspace_dir=workspace,
        token_limit=token_limit,
    )


# ---------------------------------------------------------------------
# Ingest-time truncation
# ---------------------------------------------------------------------


def test_ingest_truncation_for_oversized_tool_result():
    """Tool results larger than MAX_TOOL_RESULT_CHARS get truncated at
    append time so the cache hash sees the truncated bytes from the
    first request onwards."""
    from mini_agent.tools.base import ToolResult

    with tempfile.TemporaryDirectory() as ws:
        agent = _make_agent(ws, MagicMock())
        oversized = "x" * (agent.MAX_TOOL_RESULT_CHARS + 50_000)
        result = ToolResult(success=True, content=oversized)
        agent._add_tool_message("call_1", "bash", result)

        msg = agent.live_messages[-1]
        assert msg.role == "tool"
        # The stored content is shorter than the original, but still
        # quite a lot of context — head + tail + marker.
        assert len(msg.content) < len(oversized)
        assert "[truncated" in msg.content
        # Marker reports the original size for debuggability.
        assert str(len(oversized)) in msg.content


def test_ingest_truncation_skips_below_threshold():
    from mini_agent.tools.base import ToolResult

    with tempfile.TemporaryDirectory() as ws:
        agent = _make_agent(ws, MagicMock())
        small = "x" * 100
        agent._add_tool_message("c", "bash", ToolResult(success=True, content=small))
        assert agent.live_messages[-1].content == small


# ---------------------------------------------------------------------
# Cache invariant: live_messages never mutated by the normal pipeline
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_in_place_mutation_on_old_messages():
    """Run a few steps of the compaction pipeline and confirm that the
    bytes of an old message present at step 1 are still byte-identical
    at the end."""
    with tempfile.TemporaryDirectory() as ws:
        router = ScriptedRouter()
        agent = _make_agent(ws, router)
        # Seed 4 user-rounds with chunky payloads.
        for i in range(4):
            for m in _heavy_round(f"u{i}", payload=1_000):
                agent.live_messages.append(m)
        snapshot_msg = agent.live_messages[0]
        snapshot_content = snapshot_msg.content

        # Run a forced compaction (DP would say no-op at this size, so
        # forced=True ensures something actually happens).
        await agent._maybe_run_compaction(tool_list=[], forced=True)

        # Either the message was moved into dropped (and is no longer
        # in live_messages) OR it's still there byte-identical.
        if snapshot_msg in agent.live_messages:
            assert snapshot_msg.content == snapshot_content


# ---------------------------------------------------------------------
# _render_system_blocks: BP placement
# ---------------------------------------------------------------------


def test_render_system_blocks_no_bp2_when_no_pinned_no_summary():
    """Early-session shape: only BP #1 on the base block."""
    with tempfile.TemporaryDirectory() as ws:
        agent = _make_agent(ws, MagicMock())
        blocks = agent._render_system_blocks()
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_render_system_blocks_bp2_promoted_to_pinned_when_no_summary():
    """Pinned but no summary → pinned block gets promoted to BP #2."""
    with tempfile.TemporaryDirectory() as ws:
        agent = _make_agent(ws, MagicMock())
        agent._pin_note("category", "remember this")
        blocks = agent._render_system_blocks()
        assert len(blocks) == 2
        # First block (base) has BP #1.
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        # Pinned block also has cache_control (BP #2 promotion).
        pinned = blocks[1]
        assert pinned.get("cache_control") == {"type": "ephemeral"}
        assert "Pinned Context" in pinned["text"]


def test_render_system_blocks_correct_breakpoint_placement_with_summary():
    """Pinned + summary present → BP #2 lands on the summary block,
    pinned does NOT carry cache_control (would be duplicate)."""
    from mini_agent.schema import ContextSummary

    with tempfile.TemporaryDirectory() as ws:
        agent = _make_agent(ws, MagicMock())
        agent._pin_note("category", "remember this")
        agent.current_summary = ContextSummary(
            covered_rounds=[1],
            user_goals=["goal"],
            completed_work=["did stuff"],
            active_files=[],
            key_findings=[],
            pending_todo=[],
            raw_text="summary body",
        )
        blocks = agent._render_system_blocks()
        # Order: base, pinned, summary, [plan if present]
        assert "## Pinned Context" in blocks[1]["text"]
        assert "## Historical Summary" in blocks[2]["text"]
        # BP #1 on base
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        # Pinned (no summary-tie) must NOT have BP — that's BP #2 promotion
        # being skipped because the summary takes BP #2 instead.
        assert "cache_control" not in blocks[1]
        # BP #2 on summary
        assert blocks[2]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------
# Compaction replaces current_summary
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_replaces_current_summary():
    """A successful forced compaction must set current_summary and
    move dropped messages out of live_messages."""
    with tempfile.TemporaryDirectory() as ws:
        router = ScriptedRouter()
        agent = _make_agent(ws, router)
        for i in range(4):
            for m in _heavy_round(f"u{i}", payload=1_000):
                agent.live_messages.append(m)

        assert agent.current_summary is None
        await agent._maybe_run_compaction(tool_list=[], forced=True)

        assert agent.current_summary is not None
        # Compact count incremented.
        assert agent.compact_count == 1
        # internal_call was invoked exactly once for the summary.
        assert len(router.internal_calls) == 1
        # live_messages must still contain at least the kept user-round.
        kept_user_msgs = [m for m in agent.live_messages if m.role == "user"]
        assert len(kept_user_msgs) >= 1


@pytest.mark.asyncio
async def test_cache_aligned_summary_call_forwards_tools():
    """``router.internal_call`` must be called WITH ``tools``. (The v0
    bug the spec explicitly fixes.)"""
    with tempfile.TemporaryDirectory() as ws:
        router = ScriptedRouter()
        agent = _make_agent(ws, router)
        # Stub tools dict so list(self.tools.values()) is non-empty.
        from mini_agent.tools.base import Tool, ToolResult

        class _StubTool(Tool):
            @property
            def name(self) -> str:
                return "stub"

            @property
            def description(self) -> str:
                return "stub"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ToolResult(success=True, content="ok")

        agent.tools["stub"] = _StubTool()

        for i in range(4):
            for m in _heavy_round(f"u{i}", payload=1_000):
                agent.live_messages.append(m)

        await agent._maybe_run_compaction(tool_list=list(agent.tools.values()), forced=True)
        # internal_call invoked with tools forwarded.
        assert len(router.internal_calls) == 1
        passed_tools = router.internal_calls[0]["tools"]
        assert passed_tools is not None
        # The list contains our stub tool object.
        assert any(getattr(t, "name", None) == "stub" for t in passed_tools)


@pytest.mark.asyncio
async def test_user_goals_deterministic_merge_lands_in_raw_text():
    """Regression: prior user_goals must reach the prompt's ``## User Goals``
    section even when the LLM drops them during a merge call.

    Bug shape: ``_parse_structured_summary`` used to build ``raw_text``
    from new-dropped goals only, then ``_maybe_run_compaction`` set
    ``current_summary.user_goals`` via ``model_copy`` to the merged list.
    The field got fixed but ``raw_text`` stayed stale — and
    ``_render_system_blocks`` sends ``raw_text`` to the model, so the
    prior goal silently disappeared from the next prompt.

    Now the merge happens inside the construction helper, so the
    rendered ``raw_text`` and the structured ``user_goals`` field are
    derived from the same merged list.
    """
    from mini_agent.schema import ContextSummary

    with tempfile.TemporaryDirectory() as ws:
        # LLM response that omits "goal A" entirely — simulating drift
        # where the model only repeats new goals in a User Goals section
        # (or skips the section altogether).
        llm_drift_content = (
            "## User Goals\n"
            "- goal B\n\n"
            "## Completed Work\n"
            "- did something\n\n"
            "## Active Files\n"
            "- f.py\n\n"
            "## Key Findings\n"
            "- learned things\n\n"
            "## Pending / TODO\n"
            "- more work"
        )
        router = ScriptedRouter(
            internal_responses=[
                LLMResponse(content=llm_drift_content, finish_reason="stop"),
            ],
        )
        agent = _make_agent(ws, router)

        # Pre-populate current_summary with a prior goal that the LLM
        # is about to "forget".
        agent.current_summary = ContextSummary(
            covered_rounds=[1],
            user_goals=["goal A"],
            completed_work=["earlier work"],
            active_files=[],
            key_findings=[],
            pending_todo=[],
            raw_text="## User Goals\n- goal A\n\n## Completed Work\n- earlier work",
        )

        # dropped: one user message "goal B" + a couple of
        # assistant/tool messages so the policy has something real to
        # operate on.
        agent.live_messages = [
            Message(role="user", content="goal B"),
            Message(role="assistant", content="working on goal B"),
            Message(role="user", content="follow-up tail"),
            Message(role="assistant", content="ok"),
        ]

        # Force a positive decision so we exercise the LLM-summary path
        # regardless of DP economics on this small fixture.
        def _force_decision(snapshot, *, forced=False):
            return CompactionDecision(
                should_compact=True,
                keep_round_count=1,
                drop_message_count=2,  # drop the "goal B" round
                reason="net_positive",
                net_benefit=1.0,
                forced=False,
            )
        agent.compaction_policy.decide = _force_decision  # type: ignore[method-assign]

        await agent._maybe_run_compaction(tool_list=[], forced=False)

        # 1. Structured field carries the merged list, prior-then-new order.
        assert agent.current_summary is not None
        assert agent.current_summary.user_goals == ["goal A", "goal B"]

        # 2 + 3. raw_text — the bytes that actually reach the model via
        # _render_system_blocks — must include BOTH goals.
        raw = agent.current_summary.raw_text
        assert "goal A" in raw, (
            f"prior 'goal A' missing from raw_text; LLM drift not corrected. raw_text={raw!r}"
        )
        assert "goal B" in raw

        # Sanity: the rendered system blocks (what actually leaves the
        # process) include the merged goals too.
        rendered = agent.render_for_provider()
        system_text = "".join(
            block.get("text", "")
            for block in rendered[0].content
            if isinstance(block, dict)
        )
        assert "goal A" in system_text
        assert "goal B" in system_text


# ---------------------------------------------------------------------
# Summary failure paths
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_failure_normal_path_defers_compact():
    """Normal-path summary failure → keep dropped, try again next step."""
    with tempfile.TemporaryDirectory() as ws:
        router = ScriptedRouter(
            internal_responses=[RuntimeError("provider down")],
        )
        agent = _make_agent(ws, router)
        for i in range(4):
            for m in _heavy_round(f"u{i}", payload=1_000):
                agent.live_messages.append(m)

        original_count = len(agent.live_messages)

        # Force a positive decision via monkeypatch (otherwise DP may no-op).
        def _force_decision(snapshot, *, forced=False):
            return CompactionDecision(
                should_compact=True,
                keep_round_count=1,
                drop_message_count=2,
                reason="net_positive",
                net_benefit=1.0,
                forced=False,
            )
        agent.compaction_policy.decide = _force_decision  # type: ignore[method-assign]

        await agent._maybe_run_compaction(tool_list=[], forced=False)

        # Defer: no current_summary, live_messages unchanged.
        assert agent.current_summary is None
        assert len(agent.live_messages) == original_count
        assert agent.compact_count == 0


@pytest.mark.asyncio
async def test_summary_failure_forced_path_uses_deterministic_fallback():
    """Forced-path summary failure → deterministic fallback summary and drop."""
    with tempfile.TemporaryDirectory() as ws:
        router = ScriptedRouter(
            internal_responses=[RuntimeError("provider down")],
        )
        agent = _make_agent(ws, router)
        for i in range(4):
            for m in _heavy_round(f"u{i}", payload=1_000):
                agent.live_messages.append(m)

        await agent._maybe_run_compaction(tool_list=[], forced=True)

        # Forced-path: a deterministic fallback summary lands.
        assert agent.current_summary is not None
        # User goals are preserved verbatim from dropped messages.
        assert any("u0" == g or "u0" in g for g in agent.current_summary.user_goals)
        assert agent.compact_count == 1


# ---------------------------------------------------------------------
# Overflow recovery
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_overflow_recovery_triggers_forced_compact():
    """A pre-flight ContextOverflowError must trigger forced DP compaction
    and retry, leading to a successful response."""
    with tempfile.TemporaryDirectory() as ws:
        router = ScriptedRouter(
            call_responses=[
                ContextOverflowError("preflight"),
                LLMResponse(content="success after compact", finish_reason="stop"),
            ],
        )
        agent = _make_agent(ws, router)
        for i in range(4):
            for m in _heavy_round(f"u{i}", payload=1_000):
                agent.live_messages.append(m)

        resp = await agent._generate_with_overflow_recovery(tool_list=[])
        assert resp.content == "success after compact"
        # Forced compaction summary was issued.
        assert len(router.internal_calls) == 1
        # Two main calls: overflow + successful retry.
        assert len(router.calls) == 2


# ---------------------------------------------------------------------
# /clear semantics
# ---------------------------------------------------------------------


def test_clear_resets_all_compaction_state():
    """``messages = [agent.messages[0]]`` is the CLI /clear contract.
    All compaction-tracking fields must reset."""
    from mini_agent.schema import ContextSummary

    with tempfile.TemporaryDirectory() as ws:
        agent = _make_agent(ws, MagicMock())
        agent.add_user_message("a")
        agent.add_user_message("b")
        agent.compact_count = 5
        agent.llm_call_count = 12
        agent.user_turn_count = 2
        agent.api_total_tokens = 9999
        agent.current_summary = ContextSummary(
            covered_rounds=[1],
            user_goals=["x"],
            completed_work=[],
            active_files=[],
            key_findings=[],
            pending_todo=[],
            raw_text="...",
        )

        # /clear: keep only the system message.
        rendered = agent.render_for_provider()
        agent.messages = [rendered[0]]

        assert agent.live_messages == []
        assert agent.current_summary is None
        assert agent.compact_count == 0
        assert agent.llm_call_count == 0
        assert agent.user_turn_count == 0
        assert agent.last_usage is None
        assert agent.api_total_tokens == 0
        # Pinned notes survive /clear by spec design.
        # (We didn't pin any in this test, but verify the contract.)
        assert agent.pinned_notes == []


# ---------------------------------------------------------------------
# Render-for-provider shape (Anthropic list-of-blocks)
# ---------------------------------------------------------------------


def test_render_for_provider_emits_list_shape_system_content():
    """System content must be a ``list[dict]`` so cache_control can
    attach per block. String content was the v0 shape; v1 is structured."""
    with tempfile.TemporaryDirectory() as ws:
        agent = _make_agent(ws, MagicMock())
        rendered = agent.render_for_provider()
        assert rendered[0].role == "system"
        assert isinstance(rendered[0].content, list)
        for block in rendered[0].content:
            assert isinstance(block, dict)
            assert "type" in block
