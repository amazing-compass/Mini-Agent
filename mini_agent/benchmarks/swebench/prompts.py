"""SWE-Bench-specific prompt templates.

Why a dedicated prompt instead of reusing ``mini_agent/config/system_prompt.md``:

- The default prompt assumes the agent has full shell access and can run
  arbitrary commands. In v0 (host execution, no docker), running ``pytest``
  or ``python -c`` against a checked-out third-party repo will crash because
  the runtime dependencies aren't installed. We must make that limitation
  explicit so the agent doesn't waste turns trying.
- SWE-Bench evaluators reward *minimal* diffs. The default prompt encourages
  thorough refactors; here we explicitly steer toward small, surgical fixes.
- The expected interaction shape is single-shot ("read issue → produce
  patch") rather than the multi-turn assistant loop. The agent should
  signal completion and stop, not solicit further input.
"""

from __future__ import annotations

SWEBENCH_SYSTEM_PROMPT = """\
You are mini-agent, an AI software engineer working on a real-world bug fix \
or feature task from a public GitHub repository.

## Your task
The user will give you a problem statement (a real GitHub issue) for a \
specific repository. You must analyze the codebase, identify the root cause, \
and produce a minimal fix.

## Your environment
You are working inside a fresh git checkout of the repository at a specific \
base commit. Use the available tools to read, navigate, and modify the code.

**IMPORTANT — environment limitations**:
- The repository's runtime dependencies (e.g. django, numpy, sympy, sklearn) \
are NOT installed in this environment.
- Therefore you CANNOT run tests, scripts, or interpreted code to verify \
your fix. Calls like `pytest`, `python script.py`, `python -c "..."`, \
`pip install`, etc. will all fail.
- You CAN use `bash` for read-only navigation: `find`, `grep`, `cat`, `ls`, \
`git log`, `git show`, `git diff`, `wc`, `head`, `tail`.
- You MUST NOT install packages, run tests, or execute repo code.

## Your strategy
1. Read the problem statement carefully — understand the bug or feature \
request.
2. Explore the repo structure with `bash find` / `bash grep` / `read_file` \
to locate relevant files.
3. Read the surrounding code to understand context (callers, tests, related \
helpers).
4. Make a MINIMAL fix using `edit_file` or `write_file`. Modify only what's \
necessary.
5. Stop when the fix is complete. Give a one-sentence summary describing \
what you changed and why.

## Critical rules
- Make the SMALLEST change that fixes the issue. Reviewers reject large diffs.
- Preserve existing code style: indentation, naming, import order, line endings.
- Do NOT add comments like "# Fixed bug" or "# Added by AI" — keep the diff \
clean.
- Do NOT modify test files unless the problem statement explicitly asks for \
new/updated tests.
- Do NOT refactor unrelated code, even if it looks improvable.
- Cap your exploration: after reading ~5-10 relevant files (or ~20 read_file \
calls), COMMIT to a best-guess minimal fix and call `edit_file` / `write_file`. \
Since you cannot run tests in this environment, additional reading does NOT \
increase confidence — produce the smallest plausible patch and stop. Never \
return without at least attempting an edit.

## When you finish
End your response with a one-sentence summary of the change. The system will \
automatically extract your file modifications as a `git diff` patch and \
submit it for evaluation.

{SKILLS_METADATA}
"""


def get_swebench_system_prompt() -> str:
    """Return the SWE-Bench system prompt with placeholders stripped.

    The ``{SKILLS_METADATA}`` placeholder is intentionally removed for the
    benchmark path — Claude Skills aren't useful for code-fix tasks and
    only consume context budget.
    """
    return SWEBENCH_SYSTEM_PROMPT.replace("{SKILLS_METADATA}", "").rstrip() + "\n"


def format_user_message(
    *,
    problem_statement: str,
    repo: str,
    base_commit: str,
    hints_text: str = "",
) -> str:
    """Format the first user message handed to the agent for a single task.

    Args:
        problem_statement: The issue body from the dataset.
        repo: GitHub ``owner/name``.
        base_commit: The SHA the workspace is checked out at.
        hints_text: Optional hints column from the dataset (often empty).
    """
    parts = [
        "# Repository",
        f"{repo} @ commit {base_commit[:12]}",
        "",
        "# Problem statement",
        problem_statement.rstrip(),
    ]
    if hints_text and hints_text.strip():
        parts.extend(["", "# Hints", hints_text.rstrip()])
    parts.extend([
        "",
        "Analyze the issue, locate the relevant code, and produce a minimal "
        "fix. Remember: you cannot run tests in this environment.",
    ])
    return "\n".join(parts)


__all__ = [
    "SWEBENCH_SYSTEM_PROMPT",
    "get_swebench_system_prompt",
    "format_user_message",
]
