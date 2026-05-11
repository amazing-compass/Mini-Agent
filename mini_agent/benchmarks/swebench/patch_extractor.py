"""Extract a unified-diff patch from a (possibly modified) git workspace.

The SWE-Bench evaluator expects each prediction to carry a ``model_patch``
field whose value is a unified-diff string applicable via ``git apply`` from
the repo root. This module captures everything the agent did:

- Modifications to tracked files (``git diff HEAD``).
- New files the agent created (``--intent-to-add`` makes ``git diff`` see
  them).
- Deletions (already covered by ``git diff HEAD`` since deletions are
  tracked-file changes).

We do **not** generate patches via raw ``diff -u`` because git's diff
respects ``.gitattributes``, line-ending policy, and binary-file detection,
all of which the evaluator's ``git apply`` will also enforce.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# Generous timeout — diffing a few changed files is fast, but a pathological
# repo with hundreds of changes could take a moment.
_GIT_TIMEOUT_SECONDS = 60


class PatchExtractionError(RuntimeError):
    """Raised when we can't produce a patch from the workspace."""


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing output."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PatchExtractionError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from exc
    except FileNotFoundError as exc:
        raise PatchExtractionError("git not found in PATH") from exc

    if check and result.returncode != 0:
        raise PatchExtractionError(
            f"git {' '.join(args)} failed (exit={result.returncode}) "
            f"in {cwd}: {result.stderr.strip()}"
        )
    return result


def _list_untracked(workspace_dir: Path) -> list[str]:
    """Return paths of untracked, non-ignored files, relative to workspace."""
    result = _run_git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=workspace_dir,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _intent_to_add(workspace_dir: Path, paths: list[str]) -> None:
    """Mark new files with ``--intent-to-add`` so ``git diff`` includes them.

    This does NOT actually stage them; it just makes the index aware of
    their existence so unified-diff hunks can be generated for new content.
    """
    if not paths:
        return
    # Batch in groups of 50 to keep argv sane on systems with low ARG_MAX.
    for i in range(0, len(paths), 50):
        chunk = paths[i : i + 50]
        _run_git(["add", "--intent-to-add", "--", *chunk], cwd=workspace_dir)


def extract_git_diff(
    workspace_dir: Path,
    *,
    include_untracked: bool = True,
) -> str:
    """Compute a unified-diff patch capturing every change since ``HEAD``.

    Args:
        workspace_dir: Path to a git working tree (must contain ``.git``).
        include_untracked: When ``True`` (default), new files are
            ``--intent-to-add``-staged so the diff reflects them. When
            ``False``, only modifications/deletions of tracked files are
            captured.

    Returns:
        A unified-diff string, possibly empty if the agent didn't change
        anything. Empty strings are valid SWE-Bench predictions; they just
        score zero.

    Raises:
        PatchExtractionError: If git itself errors out (no ``.git``,
            corrupted repo, etc.). An empty diff is **not** an error.
    """
    if not workspace_dir.exists():
        raise PatchExtractionError(f"workspace {workspace_dir} does not exist")
    if not (workspace_dir / ".git").exists():
        raise PatchExtractionError(
            f"workspace {workspace_dir} is not a git repository"
        )

    if include_untracked:
        try:
            untracked = _list_untracked(workspace_dir)
            _intent_to_add(workspace_dir, untracked)
        except PatchExtractionError as exc:
            # Non-fatal: continue with a tracked-only diff.
            logger.warning(
                "Failed to stage untracked files for diff in %s: %s",
                workspace_dir, exc,
            )

    # ``git diff HEAD`` covers tracked modifications, deletions, and (with
    # --intent-to-add applied above) new files.
    result = _run_git(["diff", "HEAD"], cwd=workspace_dir)
    return result.stdout


def normalize_patch(patch: str) -> str:
    """Light normalisation for evaluator-friendly patches.

    SWE-Bench's ``git apply`` is fairly forgiving, but we avoid a few
    common pitfalls:

    - Strip a trailing CR before LF on each line (Windows-style endings
      sometimes leak in via tool output).
    - Ensure the patch ends with a single newline.
    - **Strip ``index <sha>..<sha> <mode>`` metadata lines.** These are
      informational and not required for ``git apply``; in practice the
      evaluator's docker image may have slightly different blob SHAs
      (line-ending / gitattribute filters), causing strict apply checks
      to reject otherwise valid patches. SWE-Bench's own ground-truth
      patches in the dataset omit these lines entirely.
    """
    if not patch:
        return ""
    # Normalise line endings to LF.
    normalised = patch.replace("\r\n", "\n").replace("\r", "\n")
    # Drop optional `index <oldsha>..<newsha> <mode>` lines.
    normalised = "\n".join(
        line for line in normalised.split("\n")
        if not line.startswith("index ")
    )
    if not normalised.endswith("\n"):
        normalised += "\n"
    return normalised


def extract_patch(workspace_dir: Path, *, include_untracked: bool = True) -> str:
    """Convenience: extract + normalise in one call."""
    raw = extract_git_diff(workspace_dir, include_untracked=include_untracked)
    return normalize_patch(raw)


__all__ = [
    "PatchExtractionError",
    "extract_git_diff",
    "extract_patch",
    "normalize_patch",
]
