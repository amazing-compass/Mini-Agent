"""Per-task git workspace preparation.

A SWE-Bench run touches the same repos repeatedly (django alone has dozens
of tasks). Cloning the full repo from GitHub for every task wastes time and
bandwidth. We therefore keep one **bare** clone per repo in a global cache
directory and produce per-task worktrees by cloning *from the local bare*.

Design choices:

- **Bare clone in cache** (``--bare``): only ``.git`` is downloaded once;
  the worktree is materialised per task, so different tasks never see each
  other's modifications.
- **Local clone for worktree** (``git clone <bare>``): essentially a
  filesystem copy of pack files plus a checkout — much faster than network
  clone and fully isolated.
- **Cache invalidation**: we ``git fetch`` on cache hits so a stale cache
  can still resolve newer ``base_commit`` values.
- **Best-effort cleanup**: ``cleanup_workspace`` never raises; per-task
  workspaces live under ``/tmp`` so the OS will reap them eventually anyway.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# Default bare-clone cache. ``~/.cache`` is the freedesktop convention and
# survives ``rm -rf /tmp`` cycles, which makes repeated benchmark runs much
# faster.
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mini_agent" / "swebench_repos"

# Default per-task workspace root. ``/tmp`` is plenty for one-task lifetime
# and frees automatically on macOS / Linux reboot.
DEFAULT_WORKSPACE_ROOT = Path("/tmp/swebench_runs")

# Subprocess timeout for git operations (seconds). 600s is generous for
# the worst case (huge first-time django clone over a slow link).
_GIT_TIMEOUT_SECONDS = 600


class WorkspaceError(RuntimeError):
    """Raised when we can't produce a valid workspace for a task."""


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = _GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with consistent timeout/error handling.

    Raises:
        WorkspaceError: On non-zero exit, with stderr captured.
    """
    cmd = ["git", *args]
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"git command timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    except FileNotFoundError as exc:
        raise WorkspaceError(
            "git executable not found in PATH. Install git first."
        ) from exc

    if result.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed (exit={result.returncode}) "
            f"in {cwd}: {result.stderr.strip()}"
        )
    return result


def _bare_repo_path(repo: str, cache_dir: Path) -> Path:
    """Return the bare-clone path for ``repo`` (e.g. ``django/django``)."""
    safe = repo.replace("/", "__")
    return cache_dir / f"{safe}.git"


def _ensure_bare_clone(repo: str, cache_dir: Path) -> Path:
    """Ensure a bare clone of ``repo`` exists in ``cache_dir`` and is fresh-ish.

    On first call: clones ``https://github.com/<repo>.git --bare``.
    On subsequent calls: runs ``git fetch --all`` to refresh refs (failures
    here are tolerated — a stale cache can still produce a valid worktree
    for any commit it already knows).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    bare_path = _bare_repo_path(repo, cache_dir)

    if bare_path.exists():
        try:
            _run_git(["fetch", "--all", "--quiet", "--prune"], cwd=bare_path)
        except WorkspaceError as exc:
            logger.warning(
                "Refresh of bare cache for %s failed (continuing with stale cache): %s",
                repo, exc,
            )
        return bare_path

    logger.info("First-time bare clone of %s → %s (this may take a few minutes)", repo, bare_path)
    url = f"https://github.com/{repo}.git"
    # Don't pass cwd here — we're cloning *into* a path that doesn't exist yet.
    _run_git(["clone", "--bare", "--quiet", url, str(bare_path)])
    return bare_path


def prepare_workspace(
    repo: str,
    base_commit: str,
    target_dir: Path,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Path:
    """Materialise a worktree of ``repo`` at ``base_commit`` under ``target_dir``.

    The returned directory is a full git working tree (has ``.git`` etc.),
    ready for the agent to read/edit/diff.

    Args:
        repo: GitHub ``owner/name``.
        base_commit: SHA or ref to check out (full or short SHA both work).
        target_dir: Where the worktree should land. Will be wiped if it
            already exists.
        cache_dir: Override the bare-clone cache location.

    Raises:
        WorkspaceError: For any git failure.
    """
    bare_repo = _ensure_bare_clone(repo, cache_dir)

    # Wipe any pre-existing worktree at the target. We do this rather than
    # ``git clean`` because a stale worktree might be at the wrong commit
    # and reusing it risks subtle pollution.
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Local clone from bare → fast filesystem-level copy.
    _run_git(["clone", "--quiet", str(bare_repo), str(target_dir)])

    # Checkout the requested commit (detached HEAD is fine — we don't push).
    try:
        _run_git(["checkout", "--quiet", base_commit], cwd=target_dir)
    except WorkspaceError as exc:
        # Fall back: maybe the commit is in a branch we didn't fetch
        # locally. Try fetching it explicitly from the bare cache origin.
        logger.warning("checkout %s failed, retrying with explicit fetch: %s", base_commit, exc)
        try:
            _run_git(["fetch", "origin", base_commit, "--quiet"], cwd=target_dir)
            _run_git(["checkout", "--quiet", base_commit], cwd=target_dir)
        except WorkspaceError as exc2:
            raise WorkspaceError(
                f"Failed to checkout {base_commit} in {repo}: {exc2}"
            ) from exc2

    # Local git identity — some repos run hooks that require it. Doesn't
    # affect diff output but prevents spurious failures on commit/amend.
    _run_git(["config", "user.email", "agent@mini-agent.local"], cwd=target_dir)
    _run_git(["config", "user.name", "mini-agent"], cwd=target_dir)

    return target_dir


def cleanup_workspace(target_dir: Path) -> None:
    """Remove a per-task workspace. Never raises.

    The bare cache in ``cache_dir`` is intentionally NOT touched — that's
    shared across tasks and benchmark runs.
    """
    if not target_dir.exists():
        return
    try:
        shutil.rmtree(target_dir)
    except OSError as exc:
        # Worst case the OS will clean /tmp eventually.
        logger.warning("Failed to clean up workspace %s: %s", target_dir, exc)


def main() -> None:  # pragma: no cover — manual smoke test
    """``python -m mini_agent.benchmarks.swebench.workspace`` round-trip test."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    target = Path("/tmp/swebench_workspace_smoke/repo")
    try:
        prepare_workspace("psf/requests", "HEAD", target)
        files = sorted(p.name for p in target.iterdir() if not p.name.startswith("."))
        print(f"✅ Prepared workspace at {target}")
        print(f"   Sample files: {files[:8]}")
    finally:
        cleanup_workspace(target)
        print("✅ Cleaned up")


if __name__ == "__main__":
    main()
