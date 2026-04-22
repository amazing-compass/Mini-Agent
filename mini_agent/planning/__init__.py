"""Session-level planning / TODO system for Mini-Agent.

Unlike a persistent task tracker, ``PlanningManager`` is session-scoped:
its state lives inside ``Agent`` and is wiped by ``/clear``. The plan is
rendered back into the prompt on every step so the model always sees
its own current intentions.

Layer split (per implementation plan):
- :mod:`models`   — ``PlanItem``, ``PlanningState``, status literal
- :mod:`manager`  — ``PlanningManager`` (update / render / reminder / clear)
- :mod:`tool`     — ``TodoWriteTool`` (the ``todo_write`` tool exposed to the LLM)
"""

from .manager import (
    MAX_ITEMS,
    PLAN_REMINDER_INTERVAL,
    PlanningManager,
    STATUS_MARKERS,
)
from .models import PlanItem, PlanningState, PlanStatus, VALID_STATUSES
from .tool import TodoWriteTool

__all__ = [
    "MAX_ITEMS",
    "PLAN_REMINDER_INTERVAL",
    "PlanningManager",
    "PlanItem",
    "PlanningState",
    "PlanStatus",
    "STATUS_MARKERS",
    "TodoWriteTool",
    "VALID_STATUSES",
]
