"""Unit tests for the session planner / todo-write tool."""

from __future__ import annotations

import pytest

from mini_agent.planning import (
    MAX_ITEMS,
    PLAN_REMINDER_INTERVAL,
    PlanItem,
    PlanningManager,
    PlanningState,
    STATUS_MARKERS,
    TodoWriteTool,
    VALID_STATUSES,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    def test_plan_item_defaults(self):
        item = PlanItem(content="Do the thing")
        assert item.content == "Do the thing"
        assert item.status == "pending"
        assert item.active_form == ""

    def test_planning_state_defaults(self):
        state = PlanningState()
        assert state.items == []
        assert state.rounds_since_update == 0

    def test_valid_statuses_complete(self):
        assert VALID_STATUSES == {"pending", "in_progress", "completed"}
        for status in VALID_STATUSES:
            assert status in STATUS_MARKERS


# ---------------------------------------------------------------------------
# Manager.update — valid plans
# ---------------------------------------------------------------------------


class TestManagerUpdateValid:
    def test_empty_manager(self):
        mgr = PlanningManager()
        assert mgr.state.items == []
        assert mgr.state.rounds_since_update == 0
        assert mgr.render() == "(no plan)"
        assert mgr.render_for_prompt() == ""
        assert mgr.reminder() is None

    def test_update_accepts_single_pending_item(self):
        mgr = PlanningManager()
        rendered = mgr.update([{"content": "Write tests", "status": "pending"}])
        assert len(mgr.state.items) == 1
        assert mgr.state.items[0].content == "Write tests"
        assert "Write tests" in rendered
        assert STATUS_MARKERS["pending"] in rendered

    def test_update_accepts_mixed_statuses(self):
        mgr = PlanningManager()
        mgr.update(
            [
                {"content": "A", "status": "completed"},
                {"content": "B", "status": "in_progress", "activeForm": "Doing B"},
                {"content": "C", "status": "pending"},
            ]
        )
        assert [i.status for i in mgr.state.items] == ["completed", "in_progress", "pending"]
        assert mgr.state.items[1].active_form == "Doing B"

    def test_update_resets_round_counter(self):
        mgr = PlanningManager()
        # Prime with a plan, then fake some staleness.
        mgr.update([{"content": "A", "status": "pending"}])
        mgr.state.rounds_since_update = 10
        mgr.update([{"content": "A", "status": "completed"}])
        assert mgr.state.rounds_since_update == 0

    def test_update_accepts_snake_case_active_form(self):
        mgr = PlanningManager()
        mgr.update(
            [{"content": "A", "status": "in_progress", "active_form": "Doing A"}]
        )
        assert mgr.state.items[0].active_form == "Doing A"

    def test_update_accepts_empty_list(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        mgr.update([])
        assert mgr.state.items == []


# ---------------------------------------------------------------------------
# Manager.update — validation failures
# ---------------------------------------------------------------------------


class TestManagerUpdateInvalid:
    def test_update_rejects_non_list(self):
        mgr = PlanningManager()
        with pytest.raises(TypeError):
            mgr.update("not a list")  # type: ignore[arg-type]

    def test_update_rejects_over_max_items(self):
        mgr = PlanningManager()
        items = [
            {"content": f"Task {i}", "status": "pending"} for i in range(MAX_ITEMS + 1)
        ]
        with pytest.raises(ValueError, match="max"):
            mgr.update(items)

    def test_update_rejects_empty_content(self):
        mgr = PlanningManager()
        with pytest.raises(ValueError, match="content"):
            mgr.update([{"content": "   ", "status": "pending"}])

    def test_update_rejects_invalid_status(self):
        mgr = PlanningManager()
        with pytest.raises(ValueError, match="invalid status"):
            mgr.update([{"content": "A", "status": "bogus"}])

    def test_update_rejects_multiple_in_progress(self):
        mgr = PlanningManager()
        with pytest.raises(ValueError, match="in_progress"):
            mgr.update(
                [
                    {"content": "A", "status": "in_progress"},
                    {"content": "B", "status": "in_progress"},
                ]
            )

    def test_update_rejects_non_dict_item(self):
        mgr = PlanningManager()
        with pytest.raises(ValueError, match="must be an object"):
            mgr.update(["just a string"])  # type: ignore[list-item]

    def test_failed_update_does_not_modify_state(self):
        mgr = PlanningManager()
        mgr.update([{"content": "Original", "status": "pending"}])
        with pytest.raises(ValueError):
            mgr.update(
                [
                    {"content": "A", "status": "in_progress"},
                    {"content": "B", "status": "in_progress"},  # 2nd in_progress
                ]
            )
        # State should be unchanged.
        assert len(mgr.state.items) == 1
        assert mgr.state.items[0].content == "Original"


# ---------------------------------------------------------------------------
# Reminder / stale counter
# ---------------------------------------------------------------------------


class TestReminder:
    def test_reminder_none_when_plan_empty(self):
        mgr = PlanningManager()
        # Any number of rounds without a plan should never trigger.
        for _ in range(PLAN_REMINDER_INTERVAL + 5):
            mgr.note_round_without_update()
        assert mgr.reminder() is None

    def test_reminder_none_below_threshold(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        for _ in range(PLAN_REMINDER_INTERVAL - 1):
            mgr.note_round_without_update()
        assert mgr.reminder() is None

    def test_reminder_fires_at_threshold(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        for _ in range(PLAN_REMINDER_INTERVAL):
            mgr.note_round_without_update()
        reminder = mgr.reminder()
        assert reminder is not None
        assert "todo_write" in reminder

    def test_reminder_clears_after_update(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        for _ in range(PLAN_REMINDER_INTERVAL):
            mgr.note_round_without_update()
        assert mgr.reminder() is not None
        mgr.update([{"content": "A", "status": "completed"}])
        assert mgr.reminder() is None

    def test_reset_round_counter(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        mgr.state.rounds_since_update = 99
        mgr.reset_round_counter()
        assert mgr.state.rounds_since_update == 0


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_removes_plan(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        mgr.state.rounds_since_update = 3
        mgr.clear()
        assert mgr.state.items == []
        assert mgr.state.rounds_since_update == 0
        assert mgr.render_for_prompt() == ""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_render_markers(self):
        mgr = PlanningManager()
        mgr.update(
            [
                {"content": "Done", "status": "completed"},
                {"content": "Doing", "status": "in_progress"},
                {"content": "Later", "status": "pending"},
            ]
        )
        rendered = mgr.render()
        assert STATUS_MARKERS["completed"] in rendered
        assert STATUS_MARKERS["in_progress"] in rendered
        assert STATUS_MARKERS["pending"] in rendered

    def test_render_uses_active_form_for_in_progress(self):
        mgr = PlanningManager()
        mgr.update(
            [{"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"}]
        )
        rendered = mgr.render()
        assert "Running tests" in rendered
        assert "Run tests" not in rendered

    def test_render_falls_back_to_content_when_no_active_form(self):
        mgr = PlanningManager()
        mgr.update([{"content": "Work", "status": "in_progress"}])
        rendered = mgr.render()
        assert "Work" in rendered

    def test_render_for_prompt_includes_heading(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        section = mgr.render_for_prompt()
        assert section.startswith("## Current Plan")
        assert "A" in section

    def test_render_for_prompt_includes_reminder_when_stale(self):
        mgr = PlanningManager()
        mgr.update([{"content": "A", "status": "pending"}])
        mgr.state.rounds_since_update = PLAN_REMINDER_INTERVAL + 1
        section = mgr.render_for_prompt()
        assert "## Current Plan" in section
        assert "<reminder>" in section


# ---------------------------------------------------------------------------
# TodoWriteTool
# ---------------------------------------------------------------------------


class TestTodoWriteTool:
    def test_schema_shape(self):
        tool = TodoWriteTool(PlanningManager())
        assert tool.name == "todo_write"
        schema = tool.parameters
        assert schema["type"] == "object"
        assert "items" in schema["properties"]
        assert schema["required"] == ["items"]

    @pytest.mark.asyncio
    async def test_execute_success_updates_manager(self):
        mgr = PlanningManager()
        tool = TodoWriteTool(mgr)
        result = await tool.execute(
            items=[{"content": "Write unit test", "status": "pending"}]
        )
        assert result.success
        assert len(mgr.state.items) == 1
        assert "Plan updated" in result.content

    @pytest.mark.asyncio
    async def test_execute_missing_items_returns_error(self):
        tool = TodoWriteTool(PlanningManager())
        result = await tool.execute()
        assert not result.success
        assert "items" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_validation_error_returns_failed_tool_result(self):
        tool = TodoWriteTool(PlanningManager())
        result = await tool.execute(
            items=[
                {"content": "A", "status": "in_progress"},
                {"content": "B", "status": "in_progress"},
            ]
        )
        assert not result.success
        assert "in_progress" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_to_schema_is_consistent(self):
        """Sanity check: the OpenAI schema conversion preserves the tool name."""
        tool = TodoWriteTool(PlanningManager())
        oai = tool.to_openai_schema()
        assert oai["function"]["name"] == "todo_write"
        assert "items" in oai["function"]["parameters"]["properties"]
