"""Run mini-agent against a single SWE-Bench instance and return a patch.

The runner is **stateless** — every task gets a fresh ``Agent``,
``ModelRouter``, ``ModelPool``, ``SimpleBreaker``, and workspace. We share
nothing across tasks so a misbehaving task can't poison subsequent runs.

Failure model: every exception path produces a :class:`TaskResult` with
``patch=""`` and ``error`` populated. The caller (batch runner) decides
how to surface and aggregate those.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mini_agent.agent import Agent
from mini_agent.benchmarks.swebench.dataset import SWEBenchInstance
from mini_agent.benchmarks.swebench.patch_extractor import (
    PatchExtractionError,
    extract_patch,
)
from mini_agent.benchmarks.swebench.prompts import (
    format_user_message,
    get_swebench_system_prompt,
)
from mini_agent.benchmarks.swebench.workspace import (
    DEFAULT_WORKSPACE_ROOT,
    WorkspaceError,
    cleanup_workspace,
    prepare_workspace,
)
from mini_agent.config import Config
from mini_agent.llm.ha import (
    ModelNode,
    ModelPool,
    ModelRouter,
    SimpleBreaker,
    build_client_factory,
)
from mini_agent.permissions import PermissionManager
from mini_agent.planning import PlanningManager, TodoWriteTool
from mini_agent.retry import RetryConfig as RetryConfigImpl
from mini_agent.tools.bash_tool import BashKillTool, BashOutputTool, BashTool
from mini_agent.tools.base import Tool
from mini_agent.tools.file_tools import EditTool, ReadTool, WriteTool

logger = logging.getLogger(__name__)


# Default per-task ceilings. Tweak via :func:`run_single_task` arguments.
DEFAULT_MAX_STEPS = 50
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MODEL_NAME = "mini-agent-v0"

# Token-limit safety factor: we cap the agent's working budget at 80% of
# the smallest pool node's context window so compression fires before any
# node overflows. Mirrors the heuristic in :mod:`mini_agent.cli`.
TOKEN_LIMIT_SAFETY_FACTOR = 0.8


@dataclass
class TaskResult:
    """Outcome of running mini-agent on one SWE-Bench instance."""

    instance_id: str
    success: bool
    """``True`` iff the run completed without an exception. NOT a measure of
    whether the produced patch is *correct* — that's decided by sb-cli."""

    patch: str
    """The unified-diff patch the agent produced. May be empty."""

    steps_taken: int
    duration_seconds: float
    api_total_tokens: int = 0
    final_message: str | None = None
    """The agent's last text response (often a one-line summary)."""

    error: str | None = None
    """Short error description; full traceback is in the per-task log file."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Free-form metadata: per-step token usage, custom rewards, etc."""


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _build_router(config: Config) -> ModelRouter:
    """Construct a :class:`ModelRouter` from a loaded :class:`Config`.

    Mirrors the assembly logic in :mod:`mini_agent.cli` so benchmark runs
    use the same HA semantics as the interactive REPL (failover, breaker,
    cross-family fallback). Kept inline rather than imported because the
    CLI version is bundled with side-effects (printing banners, building
    permissions, etc.).
    """
    retry_cfg_data = config.llm.retry
    retry_cfg = RetryConfigImpl(
        enabled=retry_cfg_data.enabled,
        max_retries=retry_cfg_data.max_retries,
        initial_delay=retry_cfg_data.initial_delay,
        max_delay=retry_cfg_data.max_delay,
        exponential_base=retry_cfg_data.exponential_base,
    )

    nodes: list[ModelNode] = []
    for entry in config.llm.pool:
        provider = entry.provider.lower()
        nodes.append(
            ModelNode(
                node_id=entry.node_id,
                provider=provider,
                protocol_family=(entry.protocol_family or provider).lower(),
                api_key=entry.api_key or "",
                api_base=entry.api_base,
                model=entry.model,
                priority=entry.priority,
                weight=entry.weight,
                context_window=entry.context_window,
                max_output_tokens=entry.max_output_tokens,
                supports_tools=entry.supports_tools,
                supports_thinking=entry.supports_thinking,
                enabled=entry.enabled,
                supports_explicit_cache_control=entry.supports_explicit_cache_control,
                supports_automatic_context_cache=entry.supports_automatic_context_cache,
            )
        )
    if not nodes:
        raise RuntimeError("Config has no LLM pool nodes; nothing to route to.")

    pool = ModelPool(
        nodes,
        build_client=build_client_factory(retry_cfg if retry_cfg.enabled else None),
    )
    breaker = SimpleBreaker(
        failure_threshold=config.llm.breaker.failure_threshold,
        cooldown_seconds=config.llm.breaker.cooldown_seconds,
    )
    return ModelRouter(
        pool,
        breaker,
        strategy=config.llm.routing.strategy,
        cross_family_fallback=config.llm.routing.cross_family_fallback,
    )


def _compute_token_limit(config: Config) -> int:
    """Pick a conservative token budget from the smallest enabled pool node."""
    windows = [n.context_window for n in config.llm.pool if n.enabled]
    if not windows:
        return 80_000
    return int(min(windows) * TOKEN_LIMIT_SAFETY_FACTOR)


def _build_tools_for_swebench(workspace_dir: Path) -> list[Tool]:
    """Tool set offered to the agent for SWE-Bench tasks.

    We deliberately **do** include :class:`BashTool` despite the prompt
    telling the agent not to run tests: the agent legitimately needs bash
    for ``find`` / ``grep`` / ``git log`` navigation. The prompt-level
    guardrail is good enough for v0; v1 (containerised) will let bash run
    everything safely.
    """
    workspace_str = str(workspace_dir)
    return [
        ReadTool(workspace_dir=workspace_str),
        WriteTool(workspace_dir=workspace_str),
        EditTool(workspace_dir=workspace_str),
        BashTool(workspace_dir=workspace_str),
        BashOutputTool(),
        BashKillTool(),
    ]


async def _always_approve(
    tool_name: str,
    arguments: dict[str, Any],
    decision_reason: str,
) -> bool:
    """Approval callback: auto-approve every ``ask`` decision.

    SWE-Bench is a non-interactive batch context — there's no human in the
    loop. Mirrors the ``--yes`` flag in :mod:`mini_agent.cli`. Plan-mode
    deny rules still take precedence over this (deny always wins).
    """
    del tool_name, arguments, decision_reason  # logged elsewhere
    return True


def _serialize_messages(agent: Agent) -> list[dict[str, Any]]:
    """Best-effort message serialisation for trajectory export.

    Pydantic models in ``mini_agent.schema`` already implement
    ``model_dump()``; we route through that and fall back to ``str()`` for
    anything exotic so trajectory dump never blocks the run.

    Also surfaces the agent's current ``ContextSummary`` (if any) as a
    synthetic leading entry so post-hoc analysis sees the compacted
    history instead of only the live tail.
    """
    out: list[dict[str, Any]] = []
    if agent.current_summary is not None:
        out.append({
            "role": "_compacted_summary",
            "content": agent.current_summary.raw_text,
            "user_goals": list(agent.current_summary.user_goals),
        })
    for msg in agent.live_messages:
        try:
            out.append(msg.model_dump())
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Failed to dump message: %s", exc)
            out.append({"role": getattr(msg, "role", "?"), "content": str(msg)})
    return out


def _write_trajectory(
    *,
    instance: SWEBenchInstance,
    agent: Agent,
    patch: str,
    final_message: str | None,
    duration_seconds: float,
    output_dir: Path,
) -> Path:
    """Persist the full trajectory for later analysis / SFT data export."""
    traj_dir = output_dir / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    traj_path = traj_dir / f"{instance.instance_id}.json"

    payload: dict[str, Any] = {
        "instance_id": instance.instance_id,
        "instance": asdict(instance),
        "messages": _serialize_messages(agent),
        "patch": patch,
        "final_message": final_message,
        "steps_taken": agent.llm_call_count,
        "api_total_tokens": agent.api_total_tokens,
        "duration_seconds": duration_seconds,
    }
    traj_path.write_text(json.dumps(payload, indent=2, default=str))
    return traj_path


def _write_error_log(
    *,
    instance_id: str,
    error: BaseException,
    output_dir: Path,
) -> Path:
    """Dump the full traceback for a failed task into the run's log dir."""
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{instance_id}.log"
    log_path.write_text(
        f"{type(error).__name__}: {error}\n\n{traceback.format_exc()}"
    )
    return log_path


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


async def run_single_task(
    instance: SWEBenchInstance,
    *,
    output_dir: Path,
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    model_name: str = DEFAULT_MODEL_NAME,
    config: Config | None = None,
) -> TaskResult:
    """Execute mini-agent on one SWE-Bench instance.

    Args:
        instance: The task to run.
        output_dir: The benchmark run's root directory (trajectories and
            logs land in subdirectories).
        workspace_root: Where per-task git workspaces are materialised.
        max_steps: Hard cap on agent loop iterations.
        timeout_seconds: Wall-clock cap on the entire ``Agent.run()`` call.
        model_name: Stamped onto the resulting prediction.
        config: Optional preloaded :class:`Config` (saves a YAML round-trip
            when running many tasks back to back). When ``None`` the
            default ``Config.load()`` is used.

    Returns:
        A :class:`TaskResult`. Always returned (no exceptions escape).
    """
    instance_id = instance.instance_id
    workspace_dir = workspace_root / instance_id / "repo"
    start_time = time.perf_counter()

    final_message: str | None = None
    patch: str = ""
    api_total_tokens = 0
    steps_taken = 0
    agent: Agent | None = None

    try:
        # ----- 1. workspace ----------------------------------------------
        prepare_workspace(instance.repo, instance.base_commit, workspace_dir)

        # ----- 2. configuration + router --------------------------------
        if config is None:
            config = Config.load()
        router = _build_router(config)

        # ----- 3. tools + planner ---------------------------------------
        tools = _build_tools_for_swebench(workspace_dir)
        planning_manager = PlanningManager()
        tools.append(TodoWriteTool(planning_manager))

        # ----- 4. permissions (default mode + auto-approval) ------------
        # `default` mode forces every write/bash through the approval
        # callback; we install one that says yes to everything. This
        # matches `mini-agent --yes` behaviour and keeps the deny rules
        # for genuinely dangerous bash commands (sudo, rm -rf) active.
        permission_manager = PermissionManager(mode="default")

        # ----- 5. agent --------------------------------------------------
        agent = Agent(
            router=router,
            system_prompt=get_swebench_system_prompt(),
            tools=tools,
            max_steps=max_steps,
            workspace_dir=str(workspace_dir),
            token_limit=_compute_token_limit(config),
            permission_manager=permission_manager,
            approval_callback=_always_approve,
            planning_manager=planning_manager,
        )

        # ----- 6. drive the loop with a wall-clock cap -------------------
        user_msg = format_user_message(
            problem_statement=instance.problem_statement,
            repo=instance.repo,
            base_commit=instance.base_commit,
            hints_text=instance.hints_text,
        )
        agent.add_user_message(user_msg)

        try:
            final_message = await asyncio.wait_for(
                agent.run(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Task %s exceeded %ds budget; extracting patch from "
                "whatever state exists.",
                instance_id, timeout_seconds,
            )
            final_message = "(timed out)"

        steps_taken = agent.llm_call_count
        api_total_tokens = agent.api_total_tokens

        # ----- 7. patch extraction --------------------------------------
        try:
            patch = extract_patch(workspace_dir)
        except PatchExtractionError as exc:
            logger.warning("Patch extraction failed for %s: %s", instance_id, exc)
            patch = ""

        # ----- 8. trajectory export -------------------------------------
        try:
            _write_trajectory(
                instance=instance,
                agent=agent,
                patch=patch,
                final_message=final_message,
                duration_seconds=time.perf_counter() - start_time,
                output_dir=output_dir,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "Trajectory write failed for %s: %s (continuing)", instance_id, exc,
            )

        return TaskResult(
            instance_id=instance_id,
            success=True,
            patch=patch,
            steps_taken=steps_taken,
            duration_seconds=time.perf_counter() - start_time,
            api_total_tokens=api_total_tokens,
            final_message=final_message,
        )

    except WorkspaceError as exc:
        _write_error_log(instance_id=instance_id, error=exc, output_dir=output_dir)
        return TaskResult(
            instance_id=instance_id,
            success=False,
            patch="",
            steps_taken=0,
            duration_seconds=time.perf_counter() - start_time,
            error=f"workspace prep failed: {exc}",
        )

    except Exception as exc:  # noqa: BLE001 — boundary; we *must* not raise
        _write_error_log(instance_id=instance_id, error=exc, output_dir=output_dir)
        # Still try to harvest a patch from whatever state exists — sometimes
        # the agent already produced something useful before crashing.
        try:
            if workspace_dir.exists():
                patch = extract_patch(workspace_dir)
        except Exception:  # pragma: no cover
            patch = ""
        return TaskResult(
            instance_id=instance_id,
            success=False,
            patch=patch,
            steps_taken=steps_taken,
            duration_seconds=time.perf_counter() - start_time,
            api_total_tokens=api_total_tokens,
            final_message=final_message,
            error=f"{type(exc).__name__}: {exc}",
        )

    finally:
        # The bare-clone cache is shared and must NOT be cleaned. Per-task
        # workspaces under /tmp can go.
        cleanup_workspace(workspace_dir)


__all__ = [
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_TIMEOUT_SECONDS",
    "TaskResult",
    "run_single_task",
]
