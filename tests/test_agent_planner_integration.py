"""End-to-end integration tests for the Agent × PlanningManager wiring."""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from mini_agent.agent import Agent
from mini_agent.llm.ha import ModelRouter
from mini_agent.planning import (
    PLAN_REMINDER_INTERVAL,
    PlanningManager,
    TodoWriteTool,
)
from mini_agent.schema import FunctionCall, LLMResponse, ToolCall


# ---------------------------------------------------------------------------
# Scaffolding (mirrors test_agent_permission_pipeline.py)
# ---------------------------------------------------------------------------


def _tool_call(tool_name: str, args: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=tool_name, arguments=args),
    )


def _llm_tool_response(tool_name: str, args: dict, call_id: str = "call_1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[_tool_call(tool_name, args, call_id)],
        finish_reason="tool_use",
    )


def _llm_final_response(content: str = "done") -> LLMResponse:
    return LLMResponse(content=content, tool_calls=None, finish_reason="stop")


def _scripted_router(responses: list[LLMResponse]) -> ModelRouter:
    router = MagicMock(spec=ModelRouter)
    router.call = AsyncMock(side_effect=list(responses))
    router.internal_call = AsyncMock()
    return router


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def _make_agent(temp_workspace: str) -> tuple[Agent, PlanningManager]:
    planner = PlanningManager()
    agent = Agent(
        router=MagicMock(spec=ModelRouter),
        system_prompt="sys",
        tools=[TodoWriteTool(planner)],
        workspace_dir=temp_workspace,
        planning_manager=planner,
    )
    return agent, planner


# ---------------------------------------------------------------------------
# render_for_provider injects plan section
# ---------------------------------------------------------------------------


class TestRenderForProvider:
    def test_no_plan_section_when_empty(self, temp_workspace: str):
        agent, _planner = _make_agent(temp_workspace)
        rendered = agent.render_for_provider()
        system_content = rendered[0].content
        assert "## Current Plan" not in system_content

    def test_plan_section_appears_once_plan_is_written(self, temp_workspace: str):
        agent, planner = _make_agent(temp_workspace)
        planner.update([{"content": "Do thing", "status": "pending"}])
        rendered = agent.render_for_provider()
        system_content = rendered[0].content
        assert "## Current Plan" in system_content
        assert "Do thing" in system_content

    def test_reminder_rendered_when_stale(self, temp_workspace: str):
        agent, planner = _make_agent(temp_workspace)
        planner.update([{"content": "Task", "status": "pending"}])
        # Fast-forward the stale counter.
        planner.state.rounds_since_update = PLAN_REMINDER_INTERVAL + 2
        rendered = agent.render_for_provider()
        assert "<reminder>" in rendered[0].content

    def test_agent_without_planner_renders_normally(self, temp_workspace: str):
        """Sanity: agents built without a PlanningManager render identically
        to pre-feature agents — no Plan section, ever."""
        agent = Agent(
            router=MagicMock(spec=ModelRouter),
            system_prompt="sys",
            tools=[],
            workspace_dir=temp_workspace,
        )
        rendered = agent.render_for_provider()
        assert "## Current Plan" not in rendered[0].content


# ---------------------------------------------------------------------------
# todo_write drives state via the tool loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_todo_write_tool_call_updates_planning_state(temp_workspace: str):
    router = _scripted_router(
        [
            _llm_tool_response(
                "todo_write",
                {
                    "items": [
                        {"content": "A", "status": "in_progress", "activeForm": "Doing A"},
                        {"content": "B", "status": "pending"},
                    ]
                },
            ),
            _llm_final_response(),
        ]
    )
    planner = PlanningManager()
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[TodoWriteTool(planner)],
        workspace_dir=temp_workspace,
        planning_manager=planner,
    )
    agent.add_user_message("plan it")

    await agent.run()

    assert len(planner.state.items) == 2
    assert planner.state.items[0].status == "in_progress"
    assert planner.state.items[0].active_form == "Doing A"
    # Step 1 (todo_write): update() reset counter; step_touched_plan=True
    # so no tick. Step 2 (plain answer, no tool_calls): ticks once — this
    # is the Codex-reported fix so direct-answer turns still count toward
    # stale-plan detection.
    assert planner.state.rounds_since_update == 1


# ---------------------------------------------------------------------------
# Stale counter ticks for non-planning steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_counter_ticks_on_non_planning_steps(temp_workspace: str):
    """After the plan is written, every subsequent step without
    ``todo_write`` should increment rounds_since_update — including
    steps that end with a plain assistant answer (no tool calls)."""
    router = _scripted_router(
        [
            # Step 1: write initial plan.
            _llm_tool_response(
                "todo_write",
                {"items": [{"content": "Task", "status": "pending"}]},
            ),
            # Step 2: plain assistant text (no tool calls) → ticks.
            _llm_final_response("idle 1"),
        ]
    )
    planner = PlanningManager()
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[TodoWriteTool(planner)],
        workspace_dir=temp_workspace,
        planning_manager=planner,
    )
    agent.add_user_message("plan and then chill")

    await agent.run()

    # Step 1 reset the counter (plan write). Step 2 was a direct answer
    # and ticked once via the early-return path in agent.run.
    assert planner.state.rounds_since_update == 1


@pytest.mark.asyncio
async def test_stale_counter_increments_after_tool_steps(temp_workspace: str):
    """Two tool-using steps AFTER the plan was written should tick twice."""

    # Tool that always succeeds but isn't `todo_write`.
    from mini_agent.tools.base import Tool, ToolResult

    class NoopTool(Tool):
        @property
        def name(self) -> str:
            return "noop"

        @property
        def description(self) -> str:
            return "do nothing"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **_kw) -> ToolResult:
            return ToolResult(success=True, content="noop")

    router = _scripted_router(
        [
            # Step 1: write plan.
            _llm_tool_response(
                "todo_write",
                {"items": [{"content": "Task", "status": "pending"}]},
                call_id="c1",
            ),
            # Step 2: noop call → tick.
            _llm_tool_response("noop", {}, call_id="c2"),
            # Step 3: noop call → tick.
            _llm_tool_response("noop", {}, call_id="c3"),
            # Step 4: final text.
            _llm_final_response(),
        ]
    )
    planner = PlanningManager()
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[TodoWriteTool(planner), NoopTool()],
        workspace_dir=temp_workspace,
        planning_manager=planner,
    )
    agent.add_user_message("go")

    await agent.run()

    # Step 1 (todo_write): reset, no tick.
    # Step 2 (noop): tick → 1
    # Step 3 (noop): tick → 2
    # Step 4 (final text, no tool_calls): tick via early-return → 3
    assert planner.state.rounds_since_update == 3, (
        f"Expected three ticks, got {planner.state.rounds_since_update}"
    )


# ---------------------------------------------------------------------------
# Codex-reported bug: direct-answer turns must tick the stale counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_counter_ticks_on_direct_answer_turn(temp_workspace: str):
    """Regression test for the Codex P2 bug.

    Scenario: the plan already exists, and the user sends a follow-up
    message the agent can answer without any tool calls. The stale
    counter MUST advance — otherwise the reminder never fires on
    conversational exchanges.
    """
    planner = PlanningManager()
    planner.update([{"content": "Finish the thing", "status": "in_progress"}])
    # Simulate that a prior run already reset counter to 0 (as update does).
    assert planner.state.rounds_since_update == 0

    router = _scripted_router([_llm_final_response("here is my answer")])
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[TodoWriteTool(planner)],
        workspace_dir=temp_workspace,
        planning_manager=planner,
    )
    agent.add_user_message("what do you think of the weather?")

    await agent.run()

    assert planner.state.rounds_since_update == 1, (
        "Direct-answer turn should tick the stale counter"
    )


@pytest.mark.asyncio
async def test_reminder_fires_after_direct_answer_turns(temp_workspace: str):
    """After N >= PLAN_REMINDER_INTERVAL direct-answer turns the reminder
    should be produced by ``planning_manager.reminder()`` and surfaced
    via ``agent.render_for_provider()``."""
    planner = PlanningManager()
    planner.update([{"content": "Some task", "status": "pending"}])
    agent = Agent(
        router=None,  # unused — we call render_for_provider only
        system_prompt="sys",
        tools=[TodoWriteTool(planner)],
        workspace_dir=temp_workspace,
        planning_manager=planner,
    )

    # Simulate PLAN_REMINDER_INTERVAL direct-answer turns by driving
    # agent.run through scripted final-responses.
    for _ in range(PLAN_REMINDER_INTERVAL):
        agent.router = _scripted_router([_llm_final_response("ok")])  # type: ignore[attr-defined]
        agent.add_user_message("just chatting")
        await agent.run()

    assert planner.state.rounds_since_update >= PLAN_REMINDER_INTERVAL
    reminder_section = agent.render_for_provider()[0].content
    assert "<reminder>" in reminder_section


# ---------------------------------------------------------------------------
# Plan section survives across render calls (persistent state, not tool_result)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_visible_in_subsequent_render_after_context_reset(
    temp_workspace: str,
):
    """Simulating /clear-style message reset SHOULD NOT lose the plan at
    the Agent level — clearing the plan is the CLI's responsibility.
    """
    router = _scripted_router(
        [
            _llm_tool_response(
                "todo_write",
                {"items": [{"content": "Task", "status": "pending"}]},
            ),
            _llm_final_response(),
        ]
    )
    planner = PlanningManager()
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[TodoWriteTool(planner)],
        workspace_dir=temp_workspace,
        planning_manager=planner,
    )
    agent.add_user_message("plan")

    await agent.run()
    assert planner.state.items, "plan should exist after run"

    # Simulate messages-only reset (what agent.messages setter supports).
    agent.messages = [agent.messages[0]]
    # Plan state is independent of messages — still there.
    assert planner.state.items

    # Now simulate the CLI's /clear side-effect.
    planner.clear()
    rendered = agent.render_for_provider()
    assert "## Current Plan" not in rendered[0].content
