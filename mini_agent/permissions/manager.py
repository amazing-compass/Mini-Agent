"""Permission decision pipeline.

``PermissionManager.check`` is the single entry point used by
``Agent.run`` before every tool execution. It returns a
:class:`PermissionDecision` describing whether to ``allow`` / ``deny`` /
``ask`` — it never performs the approval itself.

Decision order:

1. **Bash safety validator** (only when ``tool_name == "bash"``).
   Severe hit → ``deny``. Non-severe hit → ``ask``.
2. **Deny rules** (user-provided or defaults).
3. **Plan mode** shortcut: tools in :data:`WRITE_TOOLS` → ``deny``.
4. **Auto mode** shortcut: tools in :data:`READ_ONLY_TOOLS` or
   :data:`SESSION_META_TOOLS` → ``allow``.
5. **Allow rules** (including built-in :data:`DEFAULT_RULES`).
6. Fallback → ``ask``.

MCP / unknown tools are NEVER listed in the taxonomy or default rules, so
they consistently fall through to step 6 regardless of mode.
"""

from __future__ import annotations

from typing import Any

from .bash_safety import validate_bash
from .defaults import DEFAULT_RULES, READ_ONLY_TOOLS, SESSION_META_TOOLS, WRITE_TOOLS
from .models import (
    PermissionDecision,
    PermissionMode,
    PermissionRule,
    VALID_MODES,
)


def _matches(rule: PermissionRule, tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Return True iff ``rule`` applies to this tool call."""
    if rule.tool != "*" and rule.tool != tool_name:
        return False
    if rule.path is not None:
        candidate = str(
            tool_input.get("path")
            or tool_input.get("file_path")
            or ""
        )
        if rule.path not in candidate:
            return False
    if rule.content is not None:
        candidate = str(
            tool_input.get("command")
            or tool_input.get("content")
            or ""
        )
        if rule.content not in candidate:
            return False
    return True


class PermissionManager:
    """Mode-aware permission pipeline for tool execution."""

    def __init__(
        self,
        mode: PermissionMode = "default",
        rules: list[PermissionRule] | None = None,
    ):
        self._validate_mode(mode)
        self._mode: PermissionMode = mode
        # Copy so external callers can't mutate our internal list.
        self._rules: list[PermissionRule] = (
            list(rules) if rules is not None else list(DEFAULT_RULES)
        )

    @staticmethod
    def _validate_mode(mode: PermissionMode) -> None:
        if mode not in VALID_MODES:
            raise ValueError(
                f"Unknown mode: {mode!r}. Choose from {VALID_MODES}"
            )

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def set_mode(self, mode: PermissionMode) -> None:
        self._validate_mode(mode)
        self._mode = mode

    @property
    def rules(self) -> list[PermissionRule]:
        """Return a shallow copy of active rules."""
        return list(self._rules)

    def add_rule(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    # ------------------------------------------------------------------
    # Core decision
    # ------------------------------------------------------------------

    def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> PermissionDecision:
        """Classify ``tool_name(tool_input)`` into allow/deny/ask."""
        # 1. Bash safety — runs before rules so a malicious command can't be
        #    laundered through a broad allow rule.
        if tool_name == "bash":
            command = str(tool_input.get("command", ""))
            result = validate_bash(command)
            if result.has_severe:
                return PermissionDecision(
                    behavior="deny",
                    reason=f"Bash validator (severe): {result.describe()}",
                )
            if result.has_warning:
                return PermissionDecision(
                    behavior="ask",
                    reason=f"Bash validator flagged: {result.describe()}",
                )

        # 2. Deny rules.
        for rule in self._rules:
            if rule.behavior == "deny" and _matches(rule, tool_name, tool_input):
                return PermissionDecision(
                    behavior="deny",
                    reason=f"Blocked by deny rule (tool={rule.tool})",
                )

        # 3. Plan mode blocks every write tool.
        if self._mode == "plan" and tool_name in WRITE_TOOLS:
            return PermissionDecision(
                behavior="deny",
                reason="Plan mode: write operations are blocked",
            )

        # 4. Auto mode auto-allows low-risk tools.
        if self._mode == "auto" and (
            tool_name in READ_ONLY_TOOLS or tool_name in SESSION_META_TOOLS
        ):
            return PermissionDecision(
                behavior="allow",
                reason=f"Auto mode: {tool_name} auto-approved",
            )

        # 5. Allow rules (including defaults).
        for rule in self._rules:
            if rule.behavior == "allow" and _matches(rule, tool_name, tool_input):
                return PermissionDecision(
                    behavior="allow",
                    reason=f"Matched allow rule (tool={rule.tool})",
                )

        # 6. Fallback: ask the user.
        return PermissionDecision(
            behavior="ask",
            reason=f"No rule matched for {tool_name}; approval required",
        )
