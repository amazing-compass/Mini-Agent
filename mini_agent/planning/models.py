"""Data models for the session planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PlanStatus = Literal["pending", "in_progress", "completed"]

VALID_STATUSES: frozenset[PlanStatus] = frozenset(
    ("pending", "in_progress", "completed")
)


@dataclass
class PlanItem:
    """A single step in the session plan.

    Attributes:
        content: The imperative-form description, e.g. ``"Run unit tests"``.
        status: ``pending`` / ``in_progress`` / ``completed``.
        active_form: The present-continuous rendering shown while the item
            is ``in_progress``, e.g. ``"Running unit tests"``. Optional.
    """

    content: str
    status: PlanStatus = "pending"
    active_form: str = ""


@dataclass
class PlanningState:
    """Mutable session plan state.

    ``rounds_since_update`` counts how many agent steps have completed
    without the plan being rewritten via ``todo_write``. It drives the
    stale-plan reminder in :class:`PlanningManager`.
    """

    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0
