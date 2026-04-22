"""Default tool taxonomy and baseline permission rules.

The three tool groups drive the mode-aware branch in :class:`PermissionManager`.
Tools **not** listed in any group (e.g. MCP-provided tools) fall through to
the ``ask`` fallback in every mode — that is intentional, since we can't
reason about what a third-party MCP server does.
"""

from __future__ import annotations

from .models import PermissionRule

# Tools that only observe state. Safe to auto-allow in every mode.
READ_ONLY_TOOLS: set[str] = {
    "read_file",
    "bash_output",
    "recall_notes",
    "get_skill",
}

# Tools that mutate workspace / shell state. `plan` mode blocks them.
WRITE_TOOLS: set[str] = {
    "write_file",
    "edit_file",
    "bash",
    "bash_kill",
}

# Agent-internal session state tools. Always allowed — not destructive.
SESSION_META_TOOLS: set[str] = {
    "record_note",
    "todo_write",
}


# Built-in allow rules: read-only + session-meta tools are pre-allowed so
# they never fall through to ``ask``. Mode-specific behaviour (plan blocks
# writes, auto allows reads) is handled in manager.check().
DEFAULT_RULES: list[PermissionRule] = [
    *(PermissionRule(tool=name, behavior="allow") for name in sorted(READ_ONLY_TOOLS)),
    *(PermissionRule(tool=name, behavior="allow") for name in sorted(SESSION_META_TOOLS)),
]
