"""Permission data models.

Keep these intentionally small: they describe *what* the decision is, not
*how* it is made. Decision logic lives in :mod:`manager`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PermissionMode = Literal["default", "plan", "auto"]
PermissionBehavior = Literal["allow", "deny", "ask"]

VALID_MODES: tuple[PermissionMode, ...] = ("default", "plan", "auto")


@dataclass
class PermissionRule:
    """A single static permission rule.

    Attributes:
        tool: Tool name to match. ``"*"`` matches any tool.
        behavior: Action to take when the rule matches (``allow`` / ``deny`` / ``ask``).
        path: Optional substring that must appear in the tool's ``path``/
            ``file_path`` argument. ``None`` means no path constraint.
        content: Optional substring that must appear in the tool's ``command``/
            ``content`` argument. ``None`` means no content constraint.
    """

    tool: str
    behavior: PermissionBehavior
    path: str | None = None
    content: str | None = None


@dataclass
class PermissionDecision:
    """The result of a permission check."""

    behavior: PermissionBehavior
    reason: str
