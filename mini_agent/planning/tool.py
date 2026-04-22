"""``todo_write`` tool exposed to the LLM.

Thin wrapper that delegates validation and rendering to :class:`PlanningManager`.
"""

from __future__ import annotations

from typing import Any

from ..tools.base import Tool, ToolResult
from .manager import MAX_ITEMS, PlanningManager


class TodoWriteTool(Tool):
    """Replace the session plan in a single atomic write.

    Design notes:
    - This tool always replaces the full plan. Partial edits are out of
      scope; the model should send back the entire desired state.
    - Successful writes reset the stale-plan reminder counter via
      ``PlanningManager.update()``.
    - The agent's ``render_for_provider()`` picks up the plan from the
      shared manager — there is no need for the tool result itself to
      re-inject it into context.
    """

    def __init__(self, planning_manager: PlanningManager) -> None:
        self._manager = planning_manager

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Write or update the current session plan. Use this for any "
            "multi-step task to make your intended steps visible. Send back "
            "the ENTIRE plan each time (this call replaces it). Rules: at "
            "most one item may have status 'in_progress', and the plan "
            f"must contain at most {MAX_ITEMS} items. Mark items "
            "'completed' as you finish them and move one 'pending' item to "
            "'in_progress'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": (
                        "Full replacement plan. Each item must have "
                        "'content' and 'status'; 'activeForm' is optional."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Imperative-form task description.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {
                                "type": "string",
                                "description": (
                                    "Present-continuous label shown while "
                                    "the item is in_progress. Optional."
                                ),
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["items"],
        }

    async def execute(self, items: Any = None, **kwargs: Any) -> ToolResult:
        """Apply ``items`` as the new session plan."""
        # Accept both `items=[...]` and the first positional arg.
        if items is None:
            return ToolResult(
                success=False,
                content="",
                error="todo_write requires an 'items' parameter (list of plan items)",
            )
        try:
            rendered = self._manager.update(items)
        except (ValueError, TypeError) as exc:
            return ToolResult(success=False, content="", error=str(exc))
        return ToolResult(
            success=True,
            content=f"Plan updated ({len(self._manager.state.items)} items):\n{rendered}",
        )
