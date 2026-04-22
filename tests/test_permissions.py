"""Unit tests for the permission system (policy layer only).

These tests exercise :class:`PermissionManager.check` in isolation — no
Agent, no CLI, no LLM. That keeps failures localised to the decision logic.
"""

from __future__ import annotations

import pytest

from mini_agent.permissions import (
    DEFAULT_RULES,
    PermissionDecision,
    PermissionManager,
    PermissionRule,
    READ_ONLY_TOOLS,
    SESSION_META_TOOLS,
    VALID_MODES,
    WRITE_TOOLS,
    validate_bash,
)


# ---------------------------------------------------------------------------
# Bash validator
# ---------------------------------------------------------------------------


class TestBashValidator:
    def test_empty_command_has_no_failures(self):
        assert validate_bash("").failures == []

    def test_plain_command_has_no_failures(self):
        assert validate_bash("ls -la /tmp").failures == []

    def test_sudo_is_severe(self):
        result = validate_bash("sudo apt install foo")
        assert result.has_severe
        assert any(name == "sudo" for name, _ in result.failures)

    def test_sudoku_does_not_match_sudo(self):
        # Word boundary check — "sudoku" should NOT be treated as sudo.
        result = validate_bash("echo sudoku")
        assert not any(name == "sudo" for name, _ in result.failures)

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /tmp/foo",
            "rm -fr /tmp/foo",
            "rm -Rf /tmp/foo",
            "rm --recursive /tmp/foo",
        ],
    )
    def test_recursive_rm_is_severe(self, command: str):
        result = validate_bash(command)
        assert result.has_severe, f"{command!r} should be severe"
        assert any(name == "rm_rf" for name, _ in result.failures)

    def test_plain_rm_is_not_rm_rf(self):
        result = validate_bash("rm /tmp/foo.txt")
        assert not any(name == "rm_rf" for name, _ in result.failures)

    def test_command_substitution_is_warning(self):
        result = validate_bash('echo "$(whoami)"')
        assert not result.has_severe
        assert result.has_warning
        assert any(name == "cmd_substitution" for name, _ in result.failures)

    def test_shell_metachar_is_warning(self):
        result = validate_bash("echo foo && echo bar")
        assert not result.has_severe
        assert result.has_warning

    def test_ifs_injection_is_warning(self):
        result = validate_bash("IFS=: read a b c")
        assert result.has_warning
        assert any(name == "ifs_injection" for name, _ in result.failures)


# ---------------------------------------------------------------------------
# PermissionManager construction
# ---------------------------------------------------------------------------


class TestManagerConstruction:
    def test_default_mode(self):
        mgr = PermissionManager()
        assert mgr.mode == "default"

    def test_each_valid_mode_is_accepted(self):
        for mode in VALID_MODES:
            PermissionManager(mode=mode)  # must not raise

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            PermissionManager(mode="bogus")  # type: ignore[arg-type]

    def test_set_mode_updates_state(self):
        mgr = PermissionManager()
        mgr.set_mode("plan")
        assert mgr.mode == "plan"

    def test_set_mode_rejects_invalid(self):
        mgr = PermissionManager()
        with pytest.raises(ValueError):
            mgr.set_mode("garbage")  # type: ignore[arg-type]

    def test_defaults_include_read_only_tools(self):
        names = {r.tool for r in DEFAULT_RULES if r.behavior == "allow"}
        assert READ_ONLY_TOOLS.issubset(names)
        assert SESSION_META_TOOLS.issubset(names)

    def test_custom_rules_replace_defaults(self):
        rule = PermissionRule(tool="read_file", behavior="deny")
        mgr = PermissionManager(rules=[rule])
        assert mgr.rules == [rule]


# ---------------------------------------------------------------------------
# Decision: read-only and session-meta tools
# ---------------------------------------------------------------------------


class TestReadOnlyAndSessionMeta:
    @pytest.mark.parametrize("mode", ["default", "plan", "auto"])
    @pytest.mark.parametrize("tool", sorted(READ_ONLY_TOOLS))
    def test_read_only_allowed_in_all_modes(self, mode: str, tool: str):
        mgr = PermissionManager(mode=mode)  # type: ignore[arg-type]
        decision = mgr.check(tool, {})
        assert decision.behavior == "allow", decision

    @pytest.mark.parametrize("mode", ["default", "plan", "auto"])
    @pytest.mark.parametrize("tool", sorted(SESSION_META_TOOLS))
    def test_session_meta_allowed_in_all_modes(self, mode: str, tool: str):
        mgr = PermissionManager(mode=mode)  # type: ignore[arg-type]
        decision = mgr.check(tool, {})
        assert decision.behavior == "allow", decision


# ---------------------------------------------------------------------------
# Decision: plan mode blocks writes
# ---------------------------------------------------------------------------


class TestPlanMode:
    @pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
    def test_plan_mode_denies_writes(self, tool: str):
        mgr = PermissionManager(mode="plan")
        # Give a harmless-looking input so bash validator doesn't deny first.
        tool_input = {"command": "ls", "path": "foo.txt", "content": "x"}
        decision = mgr.check(tool, tool_input)
        assert decision.behavior == "deny"
        assert "plan mode" in decision.reason.lower()

    def test_plan_mode_allows_todo_write(self):
        mgr = PermissionManager(mode="plan")
        decision = mgr.check("todo_write", {"items": []})
        assert decision.behavior == "allow"


# ---------------------------------------------------------------------------
# Decision: auto mode
# ---------------------------------------------------------------------------


class TestAutoMode:
    def test_auto_mode_allows_read_file(self):
        mgr = PermissionManager(mode="auto")
        decision = mgr.check("read_file", {"path": "/tmp/x"})
        assert decision.behavior == "allow"

    def test_auto_mode_asks_for_bash_without_validator_hit(self):
        mgr = PermissionManager(mode="auto")
        decision = mgr.check("bash", {"command": "ls -la"})
        # bash is WRITE_TOOLS and not auto-allowed; falls through to ask.
        assert decision.behavior == "ask"

    def test_auto_mode_asks_for_write_file(self):
        mgr = PermissionManager(mode="auto")
        decision = mgr.check("write_file", {"path": "x", "content": "y"})
        assert decision.behavior == "ask"


# ---------------------------------------------------------------------------
# Decision: bash validator integration
# ---------------------------------------------------------------------------


class TestBashIntegration:
    @pytest.mark.parametrize("mode", ["default", "plan", "auto"])
    def test_sudo_is_denied_in_every_mode(self, mode: str):
        mgr = PermissionManager(mode=mode)  # type: ignore[arg-type]
        decision = mgr.check("bash", {"command": "sudo rm -rf /"})
        assert decision.behavior == "deny"
        assert "severe" in decision.reason.lower()

    def test_shell_metachar_is_ask_in_default_mode(self):
        mgr = PermissionManager(mode="default")
        decision = mgr.check("bash", {"command": "echo a && echo b"})
        # Non-severe validator hit → ask, regardless of the fact that bash
        # is a WRITE_TOOLS member (the validator branch runs first).
        assert decision.behavior == "ask"

    def test_plan_mode_denies_bash_even_without_validator(self):
        mgr = PermissionManager(mode="plan")
        decision = mgr.check("bash", {"command": "ls"})
        assert decision.behavior == "deny"


# ---------------------------------------------------------------------------
# Decision: unknown (MCP-like) tools
# ---------------------------------------------------------------------------


class TestUnknownTools:
    @pytest.mark.parametrize("mode", ["default", "plan", "auto"])
    def test_unknown_tool_asks_in_every_mode(self, mode: str):
        mgr = PermissionManager(mode=mode)  # type: ignore[arg-type]
        decision = mgr.check("mcp__github__create_issue", {"title": "hi"})
        assert decision.behavior == "ask"


# ---------------------------------------------------------------------------
# Decision: custom rules
# ---------------------------------------------------------------------------


class TestCustomRules:
    def test_deny_rule_overrides_default_allow(self):
        # Start from defaults, then append a deny rule for read_file.
        rules = list(DEFAULT_RULES) + [
            PermissionRule(tool="read_file", behavior="deny"),
        ]
        mgr = PermissionManager(mode="default", rules=rules)
        decision = mgr.check("read_file", {"path": "/etc/shadow"})
        assert decision.behavior == "deny"

    def test_deny_rule_runs_before_auto_mode_allow(self):
        rules = [PermissionRule(tool="read_file", behavior="deny")]
        mgr = PermissionManager(mode="auto", rules=rules)
        decision = mgr.check("read_file", {"path": "secret"})
        assert decision.behavior == "deny"

    def test_path_filter_scopes_rule(self):
        rules = list(DEFAULT_RULES) + [
            PermissionRule(tool="read_file", behavior="deny", path="/etc"),
        ]
        mgr = PermissionManager(mode="default", rules=rules)
        assert mgr.check("read_file", {"path": "/etc/passwd"}).behavior == "deny"
        # Different path should NOT be denied.
        assert mgr.check("read_file", {"path": "/home/me/log"}).behavior == "allow"

    def test_wildcard_tool_matches_any_tool(self):
        rules = [PermissionRule(tool="*", behavior="allow")]
        mgr = PermissionManager(mode="default", rules=rules)
        decision = mgr.check("mcp__fetch__get", {"url": "https://example.com"})
        assert decision.behavior == "allow"

    def test_content_filter_matches_bash_command(self):
        # Deny any bash command containing "curl".
        rules = list(DEFAULT_RULES) + [
            PermissionRule(tool="bash", behavior="deny", content="curl"),
        ]
        mgr = PermissionManager(mode="default", rules=rules)
        decision = mgr.check("bash", {"command": "curl https://evil"})
        assert decision.behavior == "deny"

    def test_add_rule_appends_to_active_list(self):
        mgr = PermissionManager(mode="default")
        before = len(mgr.rules)
        mgr.add_rule(PermissionRule(tool="bash", behavior="deny", content="curl"))
        assert len(mgr.rules) == before + 1


# ---------------------------------------------------------------------------
# Decision shape
# ---------------------------------------------------------------------------


def test_decision_carries_reason():
    mgr = PermissionManager(mode="default")
    decision = mgr.check("read_file", {"path": "x"})
    assert isinstance(decision, PermissionDecision)
    assert decision.reason  # non-empty
