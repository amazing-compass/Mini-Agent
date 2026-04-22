"""Session-level plan manager.

Wraps :class:`PlanningState` with validation, rendering, and a stale-plan
reminder mechanic. It is deliberately side-effect-free w.r.t. the agent
loop — the agent ticks :meth:`note_round_without_update` after every step
that did NOT call ``todo_write``.
"""

from __future__ import annotations

from typing import Any

from .models import PlanItem, PlanStatus, PlanningState, VALID_STATUSES

# Public knobs (kept as module-level constants so tests can assert on them).
MAX_ITEMS: int = 12
PLAN_REMINDER_INTERVAL: int = 5

STATUS_MARKERS: dict[PlanStatus, str] = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
}


class PlanningManager:
    """Owns the session plan state and exposes a tight mutation surface."""

    def __init__(self) -> None:
        self.state: PlanningState = PlanningState()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def update(self, items: list[Any]) -> str:
        """Replace the plan with a new set of items.

        Raises:
            ValueError: On any validation failure (too many items, invalid
                status, duplicate ``in_progress``, missing content, etc.).
            TypeError:  When ``items`` is not a list.

        Returns:
            A human-readable rendering of the new plan (suitable for
            surfacing in the tool result).
        """
        if not isinstance(items, list):
            raise TypeError("items must be a list")
        if len(items) > MAX_ITEMS:
            raise ValueError(
                f"Keep the session plan short (max {MAX_ITEMS} items, got {len(items)})"
            )

        normalized: list[PlanItem] = []
        in_progress_count = 0

        for idx, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise ValueError(f"Item {idx}: must be an object, got {type(raw).__name__}")

            content = str(raw.get("content", "")).strip()
            status_raw = str(raw.get("status", "pending")).strip().lower()
            # Accept both camelCase (activeForm) from LLM tool schemas and
            # snake_case (active_form) for Python callers / tests.
            active_form = str(
                raw.get("activeForm", raw.get("active_form", ""))
            ).strip()

            if not content:
                raise ValueError(f"Item {idx}: content is required")
            if status_raw not in VALID_STATUSES:
                raise ValueError(
                    f"Item {idx}: invalid status {status_raw!r}, "
                    f"must be one of {sorted(VALID_STATUSES)}"
                )
            # status_raw is validated above, cast for typing clarity.
            status: PlanStatus = status_raw  # type: ignore[assignment]

            if status == "in_progress":
                in_progress_count += 1

            normalized.append(
                PlanItem(content=content, status=status, active_form=active_form)
            )

        if in_progress_count > 1:
            raise ValueError(
                f"Only one plan item can be in_progress at a time "
                f"(got {in_progress_count})"
            )

        self.state.items = normalized
        self.state.rounds_since_update = 0
        return self.render()

    def clear(self) -> None:
        """Drop the entire plan. Used by ``/clear``."""
        self.state = PlanningState()

    # ------------------------------------------------------------------
    # Round tracking
    # ------------------------------------------------------------------

    def note_round_without_update(self) -> None:
        """Increment the stale counter.

        No-op when there is no plan yet — stale-plan reminders only make
        sense once the agent has actually committed to a plan.
        """
        if self.state.items:
            self.state.rounds_since_update += 1

    def reset_round_counter(self) -> None:
        self.state.rounds_since_update = 0

    def reminder(self) -> str | None:
        """Return a reminder string when the plan has gone stale, else None."""
        if not self.state.items:
            return None
        if self.state.rounds_since_update < PLAN_REMINDER_INTERVAL:
            return None
        return (
            "<reminder>Your session plan has not been updated for "
            f"{self.state.rounds_since_update} steps. Call `todo_write` to "
            "refresh the plan (mark completed items, set the next "
            "in_progress) before continuing.</reminder>"
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Compact plan rendering (no heading, no reminder)."""
        if not self.state.items:
            return "(no plan)"
        lines: list[str] = []
        for item in self.state.items:
            marker = STATUS_MARKERS[item.status]
            # Use active_form when the item is currently in progress AND
            # the LLM supplied one; otherwise fall back to content.
            label = (
                item.active_form
                if item.status == "in_progress" and item.active_form
                else item.content
            )
            lines.append(f"  {marker} {label}")
        return "\n".join(lines)

    def render_for_prompt(self) -> str:
        """Full prompt section (heading + plan + optional stale reminder).

        Returns an empty string when there is no plan — callers can use
        ``if section:`` to decide whether to append it to the prompt.
        """
        if not self.state.items:
            return ""
        section = f"## Current Plan\n{self.render()}"
        reminder = self.reminder()
        if reminder:
            section += f"\n\n{reminder}"
        return section
