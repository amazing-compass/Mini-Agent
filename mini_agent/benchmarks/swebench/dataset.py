"""SWE-Bench dataset loader.

Pulls instance metadata from HuggingFace ``datasets``. We never download the
docker images here — those are pulled lazily by the cloud evaluator (sb-cli)
or, in v1, by the local docker harness. This module only deals with the
*task definitions* (problem statement, repo, base commit, expected tests).

Public surface:
    - :class:`SWEBenchInstance` — typed view of a single task row.
    - :func:`load_swebench_dataset` — load + filter + slice in one call.

Subset naming:
    - ``"verified"``       → ``princeton-nlp/SWE-bench_Verified`` (500 tasks).
    - ``"verified_mini"``  → same dataset, capped to ``DEFAULT_VERIFIED_MINI_SIZE``
      (50 tasks). Convention only — there is no separate mini dataset on HF.
    - ``"lite"``           → ``princeton-nlp/SWE-bench_Lite`` (300 tasks).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Canonical HuggingFace dataset identifiers.
SWEBENCH_VERIFIED_DATASET = "princeton-nlp/SWE-bench_Verified"
SWEBENCH_LITE_DATASET = "princeton-nlp/SWE-bench_Lite"

# Verified Mini convention: first N tasks of the Verified test split.
# 50 is the size widely used in industry for ablation studies (small enough
# to iterate in <1h, large enough to be statistically meaningful).
DEFAULT_VERIFIED_MINI_SIZE = 50

# Allowed values for the ``subset`` argument.
_VALID_SUBSETS = ("verified", "verified_mini", "lite")


@dataclass(frozen=True)
class SWEBenchInstance:
    """Typed snapshot of a single SWE-Bench task row.

    Field names mirror the upstream dataset schema; see
    https://github.com/princeton-nlp/SWE-bench for the source of truth.

    Frozen so instances can be safely shared across asyncio tasks without
    accidental mutation.
    """

    instance_id: str
    """Globally unique task id, e.g. ``"django__django-11848"``."""

    repo: str
    """GitHub ``owner/name``, e.g. ``"django/django"``."""

    base_commit: str
    """40-char SHA the agent works against."""

    problem_statement: str
    """The GitHub issue body — the agent's primary input."""

    hints_text: str = ""
    """Optional hints (often empty in Verified)."""

    created_at: str = ""
    """ISO timestamp of the upstream issue/PR."""

    version: str = ""
    """Repo version (e.g. ``"3.0"`` for django 3.0)."""

    fail_to_pass: tuple[str, ...] = field(default_factory=tuple)
    """Tests that must move from FAIL → PASS for the patch to count."""

    pass_to_pass: tuple[str, ...] = field(default_factory=tuple)
    """Tests that must remain PASS (regression guard)."""

    environment_setup_commit: str = ""
    """Commit used by the official harness to set up dependencies."""

    @classmethod
    def from_hf_row(cls, row: dict[str, Any]) -> "SWEBenchInstance":
        """Build an instance from one HuggingFace ``datasets`` row.

        Some columns (notably ``FAIL_TO_PASS`` / ``PASS_TO_PASS``) are
        stored as JSON-encoded strings in older mirrors but as native lists
        in newer ones. We accept both shapes.
        """
        return cls(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row["problem_statement"],
            hints_text=row.get("hints_text") or "",
            created_at=row.get("created_at") or "",
            version=str(row.get("version") or ""),
            fail_to_pass=tuple(_coerce_str_list(row.get("FAIL_TO_PASS"))),
            pass_to_pass=tuple(_coerce_str_list(row.get("PASS_TO_PASS"))),
            environment_setup_commit=row.get("environment_setup_commit") or "",
        )


def _coerce_str_list(value: Any) -> list[str]:
    """Best-effort coercion of mixed-shape list/JSON columns into ``list[str]``."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            pass
        # Fall back to single-element list.
        return [value]
    return [str(value)]


def _resolve_dataset_name(subset: str) -> str:
    """Map our subset name to the upstream dataset id."""
    if subset in ("verified", "verified_mini"):
        return SWEBENCH_VERIFIED_DATASET
    if subset == "lite":
        return SWEBENCH_LITE_DATASET
    raise ValueError(
        f"Unknown subset {subset!r}. Expected one of {_VALID_SUBSETS}."
    )


def load_swebench_dataset(
    subset: str = "verified_mini",
    *,
    split: str = "test",
    limit: int | None = None,
    instance_ids: Iterable[str] | None = None,
) -> list[SWEBenchInstance]:
    """Load and filter a SWE-Bench dataset.

    Args:
        subset: ``"verified"`` / ``"verified_mini"`` / ``"lite"`` (see
            module docstring for the mapping).
        split: HuggingFace split name. SWE-Bench typically only ships ``"test"``.
        limit: Cap the number of returned instances. For ``"verified_mini"``
            this defaults to :data:`DEFAULT_VERIFIED_MINI_SIZE` when unset.
        instance_ids: Optional whitelist — only keep tasks whose
            ``instance_id`` is in this iterable. Applied **before** ``limit``.

    Returns:
        A list of :class:`SWEBenchInstance`, deterministically ordered (the
        upstream split's natural order, not shuffled).

    Raises:
        ValueError: For unknown ``subset``.
        ImportError: If ``datasets`` is not installed (we keep it as a
            soft dependency so users who never run benchmarks aren't forced
            to install it).
        RuntimeError: If the dataset can't be reached (wraps the upstream
            error with a friendlier hint).
    """
    if subset not in _VALID_SUBSETS:
        raise ValueError(
            f"Unknown subset {subset!r}. Expected one of {_VALID_SUBSETS}."
        )

    try:
        from datasets import load_dataset  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "The `datasets` package is required to load SWE-Bench. "
            "Install it via `uv add datasets` or `pip install datasets`."
        ) from exc

    dataset_name = _resolve_dataset_name(subset)
    logger.info("Loading %s (split=%s) from HuggingFace", dataset_name, split)

    try:
        ds = load_dataset(dataset_name, split=split)
    except Exception as exc:  # `datasets` raises a variety of types
        raise RuntimeError(
            f"Failed to load {dataset_name}. Common causes: "
            f"network blocked, HF_ENDPOINT not set, or the dataset id "
            f"changed upstream. Original error: {exc}"
        ) from exc

    instances = [SWEBenchInstance.from_hf_row(row) for row in ds]
    logger.info("Raw dataset size: %d", len(instances))

    if instance_ids is not None:
        wanted = set(instance_ids)
        if not wanted:
            return []
        instances = [inst for inst in instances if inst.instance_id in wanted]
        missing = wanted - {inst.instance_id for inst in instances}
        if missing:
            logger.warning(
                "instance_ids filter: %d ids not found in %s: %s",
                len(missing), dataset_name, sorted(missing)[:5],
            )

    # `verified_mini` defaults to a fixed slice when no explicit limit is
    # given. Explicit `limit` always wins so callers can override.
    effective_limit = limit
    if effective_limit is None and subset == "verified_mini":
        effective_limit = DEFAULT_VERIFIED_MINI_SIZE

    if effective_limit is not None:
        instances = instances[:effective_limit]

    logger.info("Returning %d instance(s) after filtering", len(instances))
    return instances


def main() -> None:  # pragma: no cover — manual smoke test
    """Quick manual sanity check: ``python -m mini_agent.benchmarks.swebench.dataset``."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    instances = load_swebench_dataset(subset="verified_mini", limit=3)
    for inst in instances:
        print(f"  - {inst.instance_id}")
        print(f"      repo={inst.repo}@{inst.base_commit[:8]}")
        print(f"      FAIL_TO_PASS={len(inst.fail_to_pass)} test(s)")
        preview = inst.problem_statement.replace("\n", " ")[:100]
        print(f"      preview: {preview}...")


if __name__ == "__main__":
    main()
