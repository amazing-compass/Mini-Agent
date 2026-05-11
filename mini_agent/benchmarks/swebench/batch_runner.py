"""Batch driver for SWE-Bench runs.

Responsibilities:

- Load existing ``predictions.json`` so a crashed run resumes where it
  left off (every task's prediction is persisted as soon as it completes).
- Bound concurrency with an :class:`asyncio.Semaphore` — LLM provider
  rate-limits make unconstrained ``asyncio.gather`` a bad idea.
- Surface per-task progress to stdout in a format that's readable when
  you tail it from tmux.
- Aggregate a final summary (resolve rate is decided by sb-cli, but we
  can still report patch-emission rate, error rate, and timing).

Output layout::

    <output_dir>/
    ├── predictions.json   ← submitted to sb-cli
    ├── summary.json       ← post-run statistics
    ├── trajectories/
    │   └── <instance_id>.json
    └── logs/
        └── <instance_id>.log   (only on errors)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mini_agent.benchmarks.swebench.dataset import SWEBenchInstance
from mini_agent.benchmarks.swebench.task_runner import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MODEL_NAME,
    DEFAULT_TIMEOUT_SECONDS,
    TaskResult,
    run_single_task,
)
from mini_agent.config import Config

logger = logging.getLogger(__name__)


PREDICTIONS_FILE = "predictions.json"
SUMMARY_FILE = "summary.json"


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #


def _load_existing_predictions(path: Path) -> dict[str, dict[str, Any]]:
    """Read a previous ``predictions.json`` if present.

    Accepts either the list shape (``[{instance_id, ...}, ...]``) or the
    dict shape (``{instance_id: {...}}``). Returns a dict keyed by
    ``instance_id`` so duplicate-skip checks are O(1).
    """
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        logger.warning(
            "Existing %s is not valid JSON (%s); starting from scratch.",
            path, exc,
        )
        return {}

    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    if isinstance(raw, list):
        out: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if isinstance(entry, dict) and "instance_id" in entry:
                out[entry["instance_id"]] = entry
        return out
    logger.warning("Existing %s has unexpected shape; ignoring.", path)
    return {}


def _save_predictions(
    predictions: dict[str, dict[str, Any]],
    path: Path,
) -> None:
    """Persist predictions atomically (write to .tmp then rename).

    sb-cli accepts both list and dict shapes — we emit the list shape
    because it preserves task order and is what most evaluators show in
    examples.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    as_list = list(predictions.values())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(as_list, indent=2))
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


@dataclass
class BatchSummary:
    """Aggregate stats over a benchmark run (NOT the resolve-rate score)."""

    run_id: str
    model_name: str
    total: int
    completed: int
    skipped_already_done: int
    successes: int
    """Tasks where the agent completed without exception (regardless of patch correctness)."""
    errors: int
    """Tasks where the agent crashed or workspace prep failed."""
    non_empty_patches: int
    empty_patches: int
    total_duration_seconds: float
    total_api_tokens: int
    per_task: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        """Pretty-printed multi-line summary."""
        avg = (self.total_duration_seconds / max(self.completed, 1))
        return (
            f"=== Batch Summary [{self.run_id}] ===\n"
            f"  model:                 {self.model_name}\n"
            f"  total tasks:           {self.total}\n"
            f"  newly completed:       {self.completed}\n"
            f"  skipped (resumed):     {self.skipped_already_done}\n"
            f"  agent success / error: {self.successes} / {self.errors}\n"
            f"  patches non-empty:     {self.non_empty_patches} "
            f"({100 * self.non_empty_patches / max(self.completed, 1):.1f}%)\n"
            f"  total wall time:       {self.total_duration_seconds:.1f}s "
            f"(avg {avg:.1f}s/task)\n"
            f"  total API tokens:      {self.total_api_tokens:,}"
        )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


async def run_batch(
    instances: list[SWEBenchInstance],
    *,
    output_dir: Path,
    run_id: str = "default",
    model_name: str = DEFAULT_MODEL_NAME,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    skip_completed: bool = True,
    concurrency: int = 1,
) -> BatchSummary:
    """Run mini-agent over a list of SWE-Bench instances and write predictions.

    Args:
        instances: Tasks to attempt. Order is preserved in output.
        output_dir: Per-run directory; created if missing.
        run_id: Free-form label stored in the summary (handy when grepping
            multiple runs).
        model_name: Stamped on every prediction's ``model_name_or_path``.
        max_steps: Per-task agent loop cap.
        timeout_seconds: Per-task wall-clock cap.
        skip_completed: If a previous ``predictions.json`` exists, skip
            instances that already have an entry. Set ``False`` to redo
            everything.
        concurrency: Maximum number of in-flight tasks. Default 1 (serial)
            because LLM rate limits bite quickly. Try 2-3 if your provider
            is generous.

    Returns:
        :class:`BatchSummary` describing the run.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = output_dir / PREDICTIONS_FILE

    predictions = _load_existing_predictions(predictions_path)

    # Decide what to actually run.
    if skip_completed and predictions:
        skipped_ids = {iid for iid in predictions if predictions[iid].get("model_patch") is not None}
        todo = [inst for inst in instances if inst.instance_id not in skipped_ids]
        skipped_count = len(instances) - len(todo)
        if skipped_count:
            print(
                f"📂 Resuming: {skipped_count} instance(s) already in "
                f"{predictions_path.name}, will run remaining {len(todo)}."
            )
    else:
        todo = list(instances)
        skipped_count = 0

    if not todo:
        print("✅ Nothing left to run; all predictions already exist.")
        summary = _finalise(
            instances=instances,
            predictions=predictions,
            output_dir=output_dir,
            run_id=run_id,
            model_name=model_name,
            completed=0,
            skipped=skipped_count,
            successes=0,
            errors=0,
            total_duration=0.0,
            total_tokens=0,
            per_task=[],
        )
        return summary

    print(f"📊 Running {len(todo)} task(s) with concurrency={concurrency}")
    print(f"   output: {output_dir.resolve()}")
    print(f"   model:  {model_name}\n")

    # Pre-load config once and reuse — cheap (YAML parse) but every save adds up.
    try:
        shared_config = Config.load()
    except Exception as exc:
        # We let the per-task call fail with a clearer error message later.
        logger.warning("Config preload failed (will retry per task): %s", exc)
        shared_config = None

    sem = asyncio.Semaphore(max(1, concurrency))
    completed_counter = 0
    successes = 0
    errors = 0
    total_duration = 0.0
    total_tokens = 0
    per_task: list[dict[str, Any]] = []

    state_lock = asyncio.Lock()

    async def _worker(inst: SWEBenchInstance) -> None:
        nonlocal completed_counter, successes, errors, total_duration, total_tokens

        async with sem:
            print(f"▶ start  [{inst.instance_id}]  {inst.repo}@{inst.base_commit[:8]}")
            t0 = time.perf_counter()
            try:
                result = await run_single_task(
                    inst,
                    output_dir=output_dir,
                    max_steps=max_steps,
                    timeout_seconds=timeout_seconds,
                    model_name=model_name,
                    config=shared_config,
                )
            except Exception as exc:  # pragma: no cover — defensive boundary
                logger.exception("Unhandled exception in run_single_task")
                result = TaskResult(
                    instance_id=inst.instance_id,
                    success=False,
                    patch="",
                    steps_taken=0,
                    duration_seconds=time.perf_counter() - t0,
                    error=f"{type(exc).__name__}: {exc}",
                )

            # Convert TaskResult → SWE-Bench prediction shape.
            prediction = {
                "instance_id": result.instance_id,
                "model_patch": result.patch,
                "model_name_or_path": model_name,
            }

            # Update shared state under a lock so concurrent workers don't
            # tear the predictions file or counters.
            async with state_lock:
                predictions[result.instance_id] = prediction
                _save_predictions(predictions, predictions_path)

                completed_counter += 1
                if result.success:
                    successes += 1
                else:
                    errors += 1
                total_duration += result.duration_seconds
                total_tokens += result.api_total_tokens
                per_task.append({
                    "instance_id": result.instance_id,
                    "success": result.success,
                    "patch_chars": len(result.patch),
                    "steps": result.steps_taken,
                    "duration_seconds": round(result.duration_seconds, 2),
                    "api_tokens": result.api_total_tokens,
                    "error": result.error,
                })

                status = "✅" if result.success else "❌"
                tag = "(empty patch)" if result.success and not result.patch else ""
                print(
                    f"{status} done   [{result.instance_id}]  "
                    f"{result.duration_seconds:.1f}s  "
                    f"{result.steps_taken} steps  "
                    f"patch={len(result.patch)}c  "
                    f"{tag}"
                    f"   [{completed_counter}/{len(todo)}]"
                )

    await asyncio.gather(*(_worker(inst) for inst in todo))

    summary = _finalise(
        instances=instances,
        predictions=predictions,
        output_dir=output_dir,
        run_id=run_id,
        model_name=model_name,
        completed=completed_counter,
        skipped=skipped_count,
        successes=successes,
        errors=errors,
        total_duration=total_duration,
        total_tokens=total_tokens,
        per_task=per_task,
    )
    print("\n" + summary.render())
    return summary


# --------------------------------------------------------------------------- #
# Internal: build + persist final summary
# --------------------------------------------------------------------------- #


def _finalise(
    *,
    instances: list[SWEBenchInstance],
    predictions: dict[str, dict[str, Any]],
    output_dir: Path,
    run_id: str,
    model_name: str,
    completed: int,
    skipped: int,
    successes: int,
    errors: int,
    total_duration: float,
    total_tokens: int,
    per_task: list[dict[str, Any]],
) -> BatchSummary:
    non_empty = sum(1 for p in predictions.values() if (p.get("model_patch") or "").strip())
    empty = len(predictions) - non_empty

    summary = BatchSummary(
        run_id=run_id,
        model_name=model_name,
        total=len(instances),
        completed=completed,
        skipped_already_done=skipped,
        successes=successes,
        errors=errors,
        non_empty_patches=non_empty,
        empty_patches=empty,
        total_duration_seconds=total_duration,
        total_api_tokens=total_tokens,
        per_task=per_task,
    )

    summary_path = output_dir / SUMMARY_FILE
    summary_path.write_text(json.dumps(asdict(summary), indent=2))
    return summary


__all__ = ["BatchSummary", "run_batch"]
