"""Command-line interface for the SWE-Bench adapter.

Usage::

    # Run the canonical 50-task verified-mini subset:
    python -m mini_agent.benchmarks.swebench.cli run \\
        --subset verified_mini \\
        --output benchmark_runs/baseline_v0/

    # Smoke test on a single instance:
    python -m mini_agent.benchmarks.swebench.cli run \\
        --subset verified \\
        --instance-ids django__django-11848 \\
        --output benchmark_runs/smoke1/

    # First N tasks of any subset (e.g. 5-task warmup):
    python -m mini_agent.benchmarks.swebench.cli run \\
        --subset verified \\
        --max-tasks 5 \\
        --output benchmark_runs/smoke5/

    # Just preview the dataset (no agent runs):
    python -m mini_agent.benchmarks.swebench.cli list \\
        --subset verified_mini \\
        --max-tasks 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from mini_agent.benchmarks.swebench.batch_runner import run_batch
from mini_agent.benchmarks.swebench.dataset import (
    DEFAULT_VERIFIED_MINI_SIZE,
    load_swebench_dataset,
)
from mini_agent.benchmarks.swebench.task_runner import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MODEL_NAME,
    DEFAULT_TIMEOUT_SECONDS,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _setup_logging(verbose: bool) -> None:
    """Configure logging level + format.

    Default: WARNING (so the user sees only the curated stdout progress).
    --verbose: INFO (shows dataset loading, workspace prep, etc).
    --verbose --verbose: DEBUG (everything).
    """
    level = logging.WARNING
    if verbose:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_instance_ids(value: str | None) -> list[str] | None:
    """Parse a comma-separated ``--instance-ids`` value."""
    if not value:
        return None
    ids = [s.strip() for s in value.split(",") if s.strip()]
    return ids or None


def _resolve_output_dir(raw: str) -> Path:
    """Treat the ``--output`` argument as a directory.

    For convenience, if the user passed ``…/predictions.json`` we use the
    parent directory (the predictions file is always at a fixed name
    inside the run directory).
    """
    p = Path(raw).expanduser()
    if p.suffix == ".json":
        return p.parent
    return p


# --------------------------------------------------------------------------- #
# Subcommand: run
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    """Drive a benchmark batch end-to-end."""
    instances = load_swebench_dataset(
        subset=args.subset,
        split=args.split,
        limit=args.max_tasks,
        instance_ids=_parse_instance_ids(args.instance_ids),
    )
    if not instances:
        print("⚠️  No instances matched the selection; nothing to do.")
        return 1

    output_dir = _resolve_output_dir(args.output)
    print(f"📁 Output directory: {output_dir.resolve()}")
    print(f"📋 Subset: {args.subset} ({len(instances)} instance(s))")

    try:
        summary = asyncio.run(
            run_batch(
                instances,
                output_dir=output_dir,
                run_id=args.run_id or output_dir.name,
                model_name=args.model_name,
                max_steps=args.max_steps,
                timeout_seconds=args.timeout,
                skip_completed=not args.force,
                concurrency=args.concurrency,
            )
        )
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user. Partial predictions are persisted; "
              "rerun the same command to resume.")
        return 130

    # Non-zero exit code if every newly-attempted task failed.
    if summary.completed > 0 and summary.successes == 0:
        return 2
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: list
# --------------------------------------------------------------------------- #


def cmd_list(args: argparse.Namespace) -> int:
    """Print a preview of the dataset (no agent execution)."""
    instances = load_swebench_dataset(
        subset=args.subset,
        split=args.split,
        limit=args.max_tasks,
        instance_ids=_parse_instance_ids(args.instance_ids),
    )
    if not instances:
        print("⚠️  No instances matched the selection.")
        return 1

    print(f"📋 {args.subset} (showing {len(instances)} instance(s)):\n")
    for inst in instances:
        preview = inst.problem_statement.replace("\n", " ").strip()[:90]
        print(f"  - {inst.instance_id}")
        print(f"      {inst.repo}@{inst.base_commit[:8]}  "
              f"FAIL_TO_PASS={len(inst.fail_to_pass)}")
        print(f"      {preview}{'…' if len(inst.problem_statement) > 90 else ''}")
        print()
    return 0


# --------------------------------------------------------------------------- #
# Argparse plumbing
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    # Common flags shared between top-level and every subcommand so the
    # user can put them in either position (`cli -v run ...` or
    # `cli run ... -v`). argparse does not forward top-level flags into
    # subparsers automatically, so we attach the same options to both via
    # `parents=[...]`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable INFO-level logs from the benchmark adapter.",
    )

    parser = argparse.ArgumentParser(
        prog="python -m mini_agent.benchmarks.swebench.cli",
        description="Run mini-agent against SWE-Bench instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        parents=[common],
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # ---- run ----------------------------------------------------------
    p_run = sub.add_parser("run", parents=[common], help="Execute a benchmark batch.")
    p_run.add_argument(
        "--subset", default="verified_mini",
        choices=["verified", "verified_mini", "lite"],
        help="Which dataset slice to run (default: verified_mini → "
             f"first {DEFAULT_VERIFIED_MINI_SIZE} of Verified).",
    )
    p_run.add_argument(
        "--split", default="test",
        help="HuggingFace split name (SWE-Bench almost always uses 'test').",
    )
    p_run.add_argument(
        "--output", required=True,
        help="Run directory. predictions.json is written inside this path.",
    )
    p_run.add_argument(
        "--run-id", default=None,
        help="Free-form label for the run (defaults to the output dir name).",
    )
    p_run.add_argument(
        "--max-tasks", type=int, default=None,
        help="Cap on number of tasks. For verified_mini, defaults to "
             f"{DEFAULT_VERIFIED_MINI_SIZE} when unset.",
    )
    p_run.add_argument(
        "--instance-ids", default=None,
        help="Comma-separated instance_id whitelist (applied before --max-tasks).",
    )
    p_run.add_argument(
        "--max-steps", type=int, default=DEFAULT_MAX_STEPS,
        help=f"Per-task agent step cap (default: {DEFAULT_MAX_STEPS}).",
    )
    p_run.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-task wall-clock cap in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    p_run.add_argument(
        "--concurrency", type=int, default=1,
        help="Max in-flight tasks (default: 1; raise carefully — provider rate limits).",
    )
    p_run.add_argument(
        "--model-name", default=DEFAULT_MODEL_NAME,
        help=f"Value stamped on each prediction's model_name_or_path "
             f"(default: {DEFAULT_MODEL_NAME!r}).",
    )
    p_run.add_argument(
        "--force", action="store_true",
        help="Re-run instances even if they're already in predictions.json.",
    )
    p_run.set_defaults(func=cmd_run)

    # ---- list ---------------------------------------------------------
    p_list = sub.add_parser("list", parents=[common], help="Preview which instances would run.")
    p_list.add_argument(
        "--subset", default="verified_mini",
        choices=["verified", "verified_mini", "lite"],
    )
    p_list.add_argument("--split", default="test")
    p_list.add_argument("--max-tasks", type=int, default=None)
    p_list.add_argument("--instance-ids", default=None)
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m mini_agent.benchmarks.swebench.cli``."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
