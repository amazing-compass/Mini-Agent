"""Permission system for Mini-Agent tool execution.

Provides a pre-execution gate that classifies every tool call into
``allow`` / ``deny`` / ``ask`` based on mode + rules + bash safety checks.

Layer split (per implementation plan):
- :mod:`models`       — dataclasses: ``PermissionMode``, ``PermissionRule``,
                        ``PermissionDecision``
- :mod:`bash_safety`  — regex-based bash command validator
- :mod:`defaults`     — built-in tool taxonomy and default rule list
- :mod:`manager`      — ``PermissionManager`` orchestrating the decision pipeline
"""

from .bash_safety import BashValidationResult, validate_bash
from .defaults import (
    DEFAULT_RULES,
    READ_ONLY_TOOLS,
    SESSION_META_TOOLS,
    WRITE_TOOLS,
)
from .manager import PermissionManager
from .models import (
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
    VALID_MODES,
)

__all__ = [
    "BashValidationResult",
    "validate_bash",
    "DEFAULT_RULES",
    "READ_ONLY_TOOLS",
    "SESSION_META_TOOLS",
    "WRITE_TOOLS",
    "PermissionManager",
    "PermissionBehavior",
    "PermissionDecision",
    "PermissionMode",
    "PermissionRule",
    "VALID_MODES",
]
