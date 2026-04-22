"""Lightweight regex-based validator for raw bash commands.

The validator produces a list of (name, severity) failures. Severities:

- ``severe``  — command pattern is (almost) always unsafe; caller should deny.
- ``warning`` — pattern is risky/ambiguous; caller should ask the user.

Callers typically map:
- any severe hit  → ``PermissionDecision("deny", ...)``
- only warnings   → ``PermissionDecision("ask", ...)``
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Severity = str  # "severe" | "warning"

# Each entry: (name, compiled pattern, severity)
_VALIDATOR_SPECS: list[tuple[str, str, Severity]] = [
    # `sudo` is basically never safe in an agent sandbox.
    ("sudo", r"\bsudo\b", "severe"),
    # `rm -r*` / `rm -Rf` / `rm --recursive` — recursive delete.
    ("rm_rf", r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive\b)", "severe"),
    # Shell meta / chaining — risky but common in legitimate commands.
    ("shell_metachar", r"[;&|`$]", "warning"),
    # Command substitution — `$(...)` can hide arbitrary execution.
    ("cmd_substitution", r"\$\(", "warning"),
    # IFS injection trick.
    ("ifs_injection", r"\bIFS\s*=", "warning"),
]

_VALIDATORS: list[tuple[str, re.Pattern[str], Severity]] = [
    (name, re.compile(pattern), severity)
    for name, pattern, severity in _VALIDATOR_SPECS
]


@dataclass
class BashValidationResult:
    """Result of validating a bash command."""

    failures: list[tuple[str, Severity]]

    @property
    def has_severe(self) -> bool:
        return any(severity == "severe" for _, severity in self.failures)

    @property
    def has_warning(self) -> bool:
        return any(severity == "warning" for _, severity in self.failures)

    def describe(self) -> str:
        """Human-readable description of triggered validators."""
        if not self.failures:
            return ""
        return ", ".join(f"{name}({severity})" for name, severity in self.failures)


def validate_bash(command: str) -> BashValidationResult:
    """Run all bash validators over ``command`` and collect hits.

    Ordering is preserved (matches :data:`_VALIDATOR_SPECS`) so callers can
    trust the first severe-hit name for reporting.
    """
    failures: list[tuple[str, Severity]] = []
    for name, pattern, severity in _VALIDATORS:
        if pattern.search(command):
            failures.append((name, severity))
    return BashValidationResult(failures=failures)
