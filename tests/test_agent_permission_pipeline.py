"""End-to-end integration tests for the Agent × Permission pipeline.

The LLM is fully mocked: we hand the agent a ``MagicMock(spec=ModelRouter)``
whose ``.call`` returns a scripted sequence of responses. This lets us
drive the tool loop deterministically and observe:

- Which tools actually executed (by inspecting workspace state / mocks).
- What ended up in ``agent.live_messages`` as tool results.
- The string form of ``ToolResult.error`` when the gate denied execution.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mini_agent.agent import Agent
from mini_agent.llm.ha import ModelRouter
from mini_agent.permissions import (
    PermissionManager,
    PermissionRule,
)
from mini_agent.schema import FunctionCall, LLMResponse, ToolCall
from mini_agent.tools.base import Tool, ToolResult
from mini_agent.tools.file_tools import ReadTool, WriteTool


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _tool_call(tool_name: str, args: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=tool_name, arguments=args),
    )


def _llm_tool_response(tool_name: str, args: dict, call_id: str = "call_1") -> LLMResponse:
    """Response that asks for a single tool call."""
    return LLMResponse(
        content="",
        tool_calls=[_tool_call(tool_name, args, call_id)],
        finish_reason="tool_use",
    )


def _llm_final_response(content: str = "done") -> LLMResponse:
    """Response that terminates the loop (no tool calls)."""
    return LLMResponse(content=content, tool_calls=None, finish_reason="stop")


def _scripted_router(responses: list[LLMResponse]) -> ModelRouter:
    """Mock router whose .call yields the scripted responses in order."""
    router = MagicMock(spec=ModelRouter)
    router.call = AsyncMock(side_effect=list(responses))
    router.internal_call = AsyncMock()
    return router


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class RecordingTool(Tool):
    """Minimal tool that records invocations for later assertions."""

    def __init__(self, tool_name: str = "recording_tool"):
        self._name = tool_name
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Records all invocations"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    async def execute(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(success=True, content="recorded")


def _last_tool_result_message(agent: Agent):
    tool_msgs = [m for m in agent.live_messages if m.role == "tool"]
    assert tool_msgs, "expected at least one tool message"
    return tool_msgs[-1]


# ---------------------------------------------------------------------------
# Plan mode denies write tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_mode_denies_write_file(temp_workspace: str):
    router = _scripted_router(
        [
            _llm_tool_response(
                "write_file",
                {"path": "hello.txt", "content": "hi"},
            ),
            _llm_final_response(),  # After deny, agent loops once more.
        ]
    )
    write_tool = WriteTool(workspace_dir=temp_workspace)

    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[write_tool],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="plan"),
    )
    agent.add_user_message("write hello.txt please")

    await agent.run()

    # File must NOT have been created — gate ran before tool.
    assert not (Path(temp_workspace) / "hello.txt").exists()

    # Tool result should say "Permission denied".
    last = _last_tool_result_message(agent)
    assert "Permission denied" in last.content
    assert "Plan mode" in last.content


# ---------------------------------------------------------------------------
# Bash validator denies sudo severely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_mode_denies_sudo_via_bash_validator(temp_workspace: str):
    bash_calls = []

    class FakeBash(Tool):
        @property
        def name(self): return "bash"

        @property
        def description(self): return "run shell"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"command": {"type": "string"}}}

        async def execute(self, command: str = "") -> ToolResult:
            bash_calls.append(command)
            return ToolResult(success=True, content=f"ran: {command}")

    router = _scripted_router(
        [
            _llm_tool_response("bash", {"command": "sudo rm -rf /"}),
            _llm_final_response(),
        ]
    )

    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[FakeBash()],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="default"),
    )
    agent.add_user_message("please sudo")

    await agent.run()

    assert bash_calls == [], "Bash must NOT have run on a sudo command"
    last = _last_tool_result_message(agent)
    assert "Permission denied" in last.content
    assert "severe" in last.content.lower()


# ---------------------------------------------------------------------------
# Ask with approving callback → tool runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_with_approval_callback_approves_and_runs(temp_workspace: str):
    router = _scripted_router(
        [
            _llm_tool_response(
                "write_file",
                {"path": "ok.txt", "content": "approved"},
            ),
            _llm_final_response(),
        ]
    )
    write_tool = WriteTool(workspace_dir=temp_workspace)

    approvals: list[tuple[str, dict, str]] = []

    async def approver(tool_name: str, args: dict, reason: str) -> bool:
        approvals.append((tool_name, args, reason))
        return True

    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[write_tool],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="default"),
        approval_callback=approver,
    )
    agent.add_user_message("write ok.txt")

    await agent.run()

    assert len(approvals) == 1
    assert approvals[0][0] == "write_file"
    # File really was written.
    assert (Path(temp_workspace) / "ok.txt").read_text() == "approved"


# ---------------------------------------------------------------------------
# Ask with rejecting callback → tool skipped, message contains "user rejected"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_with_rejecting_callback_denies(temp_workspace: str):
    router = _scripted_router(
        [
            _llm_tool_response(
                "write_file",
                {"path": "nope.txt", "content": "nope"},
            ),
            _llm_final_response(),
        ]
    )
    write_tool = WriteTool(workspace_dir=temp_workspace)

    async def always_no(tool_name: str, args: dict, reason: str) -> bool:
        return False

    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[write_tool],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="default"),
        approval_callback=always_no,
    )
    agent.add_user_message("write nope.txt")

    await agent.run()

    assert not (Path(temp_workspace) / "nope.txt").exists()
    last = _last_tool_result_message(agent)
    assert "user rejected" in last.content


# ---------------------------------------------------------------------------
# Ask with NO callback (non-interactive / --task) → deny
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_without_callback_auto_denies(temp_workspace: str):
    router = _scripted_router(
        [
            _llm_tool_response(
                "write_file",
                {"path": "task.txt", "content": "task"},
            ),
            _llm_final_response(),
        ]
    )
    write_tool = WriteTool(workspace_dir=temp_workspace)

    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[write_tool],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="default"),
        approval_callback=None,
    )
    agent.add_user_message("write task.txt")

    await agent.run()

    assert not (Path(temp_workspace) / "task.txt").exists()
    last = _last_tool_result_message(agent)
    assert "no approval callback" in last.content.lower() or "ask→deny" in last.content


# ---------------------------------------------------------------------------
# Read-only tools run without approval in every mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "plan", "auto"])
async def test_read_only_tools_run_without_approval(temp_workspace: str, mode: str):
    target = Path(temp_workspace) / "present.txt"
    target.write_text("content")

    router = _scripted_router(
        [
            _llm_tool_response("read_file", {"path": "present.txt"}),
            _llm_final_response(),
        ]
    )
    read_tool = ReadTool(workspace_dir=temp_workspace)

    def _fail_callback(*_args, **_kw):
        raise AssertionError("approval callback should not be invoked")

    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[read_tool],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode=mode),  # type: ignore[arg-type]
        approval_callback=AsyncMock(side_effect=_fail_callback),
    )
    agent.add_user_message("read the file")

    await agent.run()

    last = _last_tool_result_message(agent)
    assert "content" in last.content or "Error" not in last.content


# ---------------------------------------------------------------------------
# Unknown tool → short-circuits before permission gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_never_asks_for_approval(temp_workspace: str):
    router = _scripted_router(
        [
            _llm_tool_response("definitely_not_real", {}),
            _llm_final_response(),
        ]
    )

    approver = AsyncMock(return_value=True)
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="default"),
        approval_callback=approver,
    )
    agent.add_user_message("invoke the ghost tool")

    await agent.run()

    approver.assert_not_called()
    last = _last_tool_result_message(agent)
    assert "Unknown tool" in last.content


# ---------------------------------------------------------------------------
# Custom deny rule overrides default allow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_deny_rule_blocks_read_file(temp_workspace: str):
    target = Path(temp_workspace) / "secret.txt"
    target.write_text("leaked")

    router = _scripted_router(
        [
            _llm_tool_response("read_file", {"path": "secret.txt"}),
            _llm_final_response(),
        ]
    )

    from mini_agent.permissions import DEFAULT_RULES
    rules = list(DEFAULT_RULES) + [
        PermissionRule(tool="read_file", behavior="deny", path="secret"),
    ]

    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[ReadTool(workspace_dir=temp_workspace)],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="default", rules=rules),
    )
    agent.add_user_message("read the secret")

    await agent.run()

    last = _last_tool_result_message(agent)
    assert "Permission denied" in last.content


# ---------------------------------------------------------------------------
# No permission manager → legacy behaviour preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_permission_manager_runs_tools_directly(temp_workspace: str):
    """Sanity check: omitting the manager preserves pre-permission behaviour."""
    router = _scripted_router(
        [
            _llm_tool_response(
                "write_file",
                {"path": "raw.txt", "content": "raw"},
            ),
            _llm_final_response(),
        ]
    )
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[WriteTool(workspace_dir=temp_workspace)],
        workspace_dir=temp_workspace,
    )
    agent.add_user_message("write raw.txt")

    await agent.run()

    assert (Path(temp_workspace) / "raw.txt").read_text() == "raw"


# ---------------------------------------------------------------------------
# --yes / auto-yes callback behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_yes_callback_unblocks_task_mode_writes(temp_workspace: str):
    """Regression test for the Codex P1 bug.

    Scenario mirrors ``mini-agent --task "..." --yes``: no interactive
    session exists, but the user opted into auto-approval, so write_file
    must still execute in default mode.
    """
    from mini_agent.cli import _auto_yes_approval  # the exact callback CLI installs

    router = _scripted_router(
        [
            _llm_tool_response("write_file", {"path": "yes.txt", "content": "ok"}),
            _llm_final_response(),
        ]
    )
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[WriteTool(workspace_dir=temp_workspace)],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="default"),
        approval_callback=_auto_yes_approval,
    )
    agent.add_user_message("write it")

    await agent.run()

    assert (Path(temp_workspace) / "yes.txt").read_text() == "ok"


@pytest.mark.asyncio
async def test_plan_mode_deny_beats_auto_yes(temp_workspace: str):
    """--yes must NOT override plan-mode deny: the doc contract is that
    plan is analysis-only regardless of approval settings."""
    from mini_agent.cli import _auto_yes_approval

    router = _scripted_router(
        [
            _llm_tool_response("write_file", {"path": "blocked.txt", "content": "nope"}),
            _llm_final_response(),
        ]
    )
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[WriteTool(workspace_dir=temp_workspace)],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="plan"),
        approval_callback=_auto_yes_approval,
    )
    agent.add_user_message("try to write")

    await agent.run()

    # File not created — plan mode denied before auto_yes could fire.
    assert not (Path(temp_workspace) / "blocked.txt").exists()
    last = _last_tool_result_message(agent)
    assert "Plan mode" in last.content


@pytest.mark.asyncio
async def test_auto_yes_callback_is_async_and_returns_true():
    """Direct unit test of the CLI's auto-yes callback itself."""
    from mini_agent.cli import _auto_yes_approval

    result = await _auto_yes_approval("bash", {"command": "ls"}, "some reason")
    assert result is True


# ---------------------------------------------------------------------------
# Permission decision is forwarded into the logger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_decision_is_logged(temp_workspace: str):
    """The logger should receive permission_behavior + permission_reason."""
    router = _scripted_router(
        [
            _llm_tool_response(
                "write_file", {"path": "x.txt", "content": "x"}
            ),
            _llm_final_response(),
        ]
    )
    agent = Agent(
        router=router,
        system_prompt="sys",
        tools=[WriteTool(workspace_dir=temp_workspace)],
        workspace_dir=temp_workspace,
        permission_manager=PermissionManager(mode="plan"),  # will deny
    )
    # Spy on the logger instead of reading from disk.
    agent.logger.log_tool_result = MagicMock()
    agent.add_user_message("write x.txt")

    await agent.run()

    # At least one call should carry the plan-mode deny decision.
    assert agent.logger.log_tool_result.called
    call_kwargs = agent.logger.log_tool_result.call_args_list[0].kwargs
    assert call_kwargs.get("permission_behavior") == "deny"
    assert "plan" in (call_kwargs.get("permission_reason") or "").lower()
