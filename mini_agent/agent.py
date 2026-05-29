# ✅
"""Core Agent implementation."""

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Optional

import tiktoken

from .compaction import (
    CachePolicy,
    CompactionPolicy,
    CompactionSnapshot,
    ModelPricing,
)
from .llm.ha import ContextOverflowError, ModelRouter
from .logger import AgentLogger
from .permissions import PermissionDecision, PermissionManager
from .planning import PlanningManager
from .schema import ContextSummary, Message, TokenUsage
from .tools.base import Tool, ToolResult
from .utils import calculate_display_width


# Type alias: approval callback signature.
# Returns True to allow, False to deny. Reason is the PermissionDecision reason
# so the CLI can display context ("why is this being asked?").

# 给Callablep[参数, 返回值] 起别名 ApprovalCallback
# Callable[[str, dict, str], Awaitable[bool]] 表示一个函数类型约束
# 凡是接受(str, dict, str) 这三个参数的函数 -- 返回Awaitable[bool] 的函数 -- 都叫ApprovalCallback
ApprovalCallback = Callable[[str, dict, str], Awaitable[bool]]


# ANSI color codes
class Colors:
    """Terminal color definitions"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


class Agent:
    """Single agent with basic tools and MCP support."""

    # ✅
    # 初始化agent -- 接收 router、system_prompt、tools、权限管理器等，构建内部状态
    def __init__(
        self,
        router: ModelRouter,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,  # Summary triggered when tokens exceed this value
        permission_manager: Optional[PermissionManager] = None,
        approval_callback: Optional[ApprovalCallback] = None,
        planning_manager: Optional[PlanningManager] = None,
    ):
        """Phase 3: Agent directly owns a `ModelRouter` — no more facade.

        `router.call(messages, tools)` replaces the old `llm.generate`;
        `router.internal_call(messages, tools)` replaces the cache-aligned
        summary path. Design §1.0 + §8.2 + §13.7 step 3.

        Permission system (optional):
          - ``permission_manager``: if ``None``, all tool calls are executed
            directly (legacy behaviour). If provided, every tool call passes
            through its ``check()`` first.
          - ``approval_callback``: invoked when the decision is ``ask``. If
            ``None`` (e.g. ``--task`` non-interactive mode), ``ask`` falls back
            to ``deny`` — this prevents agents from hanging forever waiting
            for a user who isn't there.
        """
        self.router = router
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        # Cancellation event for interrupting agent execution (set externally, e.g., by Esc key)
        self.cancel_event: Optional[asyncio.Event] = None

        # Permission system (both optional — agent is fully backwards-compatible).
        self.permission_manager: Optional[PermissionManager] = permission_manager
        self.approval_callback: Optional[ApprovalCallback] = approval_callback

        # Session planner (optional). When present, `render_for_provider()`
        # surfaces the current plan + stale-plan reminder into the system
        # prompt, and the tool loop ticks its stale counter after every
        # step that didn't call `todo_write`.
        self.planning_manager: Optional[PlanningManager] = planning_manager

        # Ensure workspace exists
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Inject workspace information into base system prompt (IMPROVEMENT_04):
        # pinned notes are NOT baked into a persistent string anymore — they
        # are rendered into system blocks at request time. So workspace info
        # belongs in _base_system_prompt directly.
        if "Current Workspace" not in system_prompt:
            workspace_info = f"\n\n## Current Workspace\nYou are currently working in: `{self.workspace_dir.absolute()}`\nAll relative paths will be resolved relative to this directory."
            system_prompt = system_prompt + workspace_info

        self._base_system_prompt: str = system_prompt
        self.pinned_notes: list[dict] = []
        # Single rolling summary (v1 simplification of cold_summaries).
        self.current_summary: ContextSummary | None = None
        self.live_messages: list[Message] = []

        # Initialize logger
        self.logger = AgentLogger()

        # Token usage from last API response (updated after each LLM call).
        self.api_total_tokens: int = 0
        # Last full TokenUsage object — used for cache-hit logging /
        # diagnostics. Snapshot-time api_input estimate is computed from
        # _estimate_tokens(), not from this.
        self.last_usage: TokenUsage | None = None
        # Counters for the DP NetBenefit formula.
        self.compact_count: int = 0
        self.llm_call_count: int = 0
        self.user_turn_count: int = 0

        # Cache the primary node once at startup for pricing lookups.
        # Failover may pick a different node at call time; the spec
        # accepts the resulting pricing skew (errs toward "less
        # compaction", which is the safe direction).
        self._primary_node = (
            router.peek_primary_node() if hasattr(router, "peek_primary_node") else None
        )
        self.compaction_policy: CompactionPolicy = CompactionPolicy()
        # Tokenizer encoder cached lazily for the agent's own counting
        # helpers (separate from the policy's own encoder cache).
        self._token_encoder = None

    # --- Backward-compatible messages property ---

    # ✅
    # 兼容旧接口，读取时等价于 render_for_provider()，写入时支持 /clear 重置会话
    @property
    def messages(self) -> list[Message]:
        """Backward compatible: return full message list view (read-only)."""
        return self.render_for_provider()

    # ✅
    # 没命中if -- 实际上是兜底
    # 这部分代码只有/clear执行
    @messages.setter
    def messages(self, value: list[Message]):
        """Backward compatible: support agent.messages = [agent.messages[0]] for /clear."""
        if len(value) == 1 and value[0].role == "system":
            # /clear path. Reset everything compaction-related so the
            # next session starts at a true clean slate; pinned_notes
            # survive by design (they're cross-session memory).
            self.live_messages = []
            self.current_summary = None
            self.compact_count = 0
            self.llm_call_count = 0
            self.user_turn_count = 0
            self.last_usage = None
            self.api_total_tokens = 0
            return
        self.live_messages = [m for m in value if m.role != "system"]

    # --- Render for provider ---
    # ✅
    def _render_system_blocks(self) -> list[dict]:
        """Build Anthropic-shape system blocks ordered by change frequency.

        Order: ``[base | BP#1] [pinned] [current_summary | BP#2] [current_plan]``.

        BP #2 placement table:
        - pinned + summary  →  BP #2 on the summary block
        - pinned, no summary  →  BP #2 promoted to the pinned block
        - no pinned, summary  →  BP #2 on the summary block
        - neither  →  no BP #2 (early in the session; 3 BPs used)

        BP #1 / BP #2 markers are only honored by Anthropic-explicit
        nodes; other clients strip them. plan is intentionally last so
        the high-frequency churn doesn't invalidate BP #2.
        """
        blocks: list[dict] = []

        blocks.append({
            "type": "text",
            "text": self._base_system_prompt,
            "cache_control": {"type": "ephemeral"},  # BP #1
        })

        pinned_block_idx: int | None = None
        if self.pinned_notes:
            pinned_text = "## Pinned Context (Important - Always Available)\n"
            for note in self.pinned_notes:
                cat = note.get("category", "general")
                content = note.get("content", "")
                pinned_text += f"- [{cat}] {content}\n"
            blocks.append({"type": "text", "text": pinned_text})
            pinned_block_idx = len(blocks) - 1

        if self.current_summary is not None:
            blocks.append({
                "type": "text",
                "text": f"## Historical Summary\n{self.current_summary.raw_text}",
                "cache_control": {"type": "ephemeral"},  # BP #2 on summary
            })
        elif pinned_block_idx is not None:
            # No summary but we do have pinned notes: promote pinned to BP #2.
            blocks[pinned_block_idx]["cache_control"] = {"type": "ephemeral"}

        if self.planning_manager is not None:
            plan_section = self.planning_manager.render_for_prompt()
            if plan_section:
                blocks.append({"type": "text", "text": plan_section})

        return blocks

    # ✅
    # 组装完整消息列表，把 system blocks + live_messages 合并成 API 可接受的格式
    def render_for_provider(self) -> list[Message]:
        """Assemble internal storage into an API-legal message sequence.

        The system message uses Anthropic-shape ``content=list[dict]``.
        Non-Anthropic clients flatten this to a string in their own
        ``_convert_messages`` (see OpenAIClient).
        """
        result: list[Message] = []
        result.append(Message(role="system", content=self._render_system_blocks()))
        result.extend(self.live_messages)
        return result

    # --- Message append methods ---

    # Ingest-time truncation cap for oversized tool results
    # (IMPROVEMENT_04 §3.3). Replaces the old
    # ``CONTENT_TRUNCATE_KEEP_CHARS`` post-hoc emergency knob — this
    # one fires at append time, before the message enters live_messages
    # / the cache hash.
    #
    # 50_000 chars ≈ ~12K tokens on ASCII/code traffic. For Chinese-
    # dense traffic (~3 bytes/char UTF-8) the per-message token count
    # can triple; this is a known trade-off.
    MAX_TOOL_RESULT_CHARS = 50_000

    # ✅
    # 添加用户消息到历史，同时递增 user_turn_count
    def add_user_message(self, content: str):
        """Add a user message to history."""
        self.live_messages.append(Message(role="user", content=content))
        self.user_turn_count += 1

    # ✅
    def _add_assistant_message(self, response) -> Message:
        """Add an assistant message from LLM response."""
        msg = Message(
            role="assistant",
            content=response.content,
            thinking=response.thinking,
            tool_calls=response.tool_calls,
        )
        self.live_messages.append(msg)
        return msg

    # ✅
    def _add_tool_message(self, tool_call_id: str, function_name: str, result: ToolResult) -> Message:
        """Add a tool result message, truncating oversized payloads at ingest.

        Critical: this truncation happens BEFORE the message enters
        ``live_messages`` so the cache hash sees the truncated form on
        the very first request. This is NOT the L1/L2 in-place rewrite
        that the v0 design relied on — that path is gone.
        """
        content = result.content if result.success else f"Error: {result.error}"

        if len(content) > self.MAX_TOOL_RESULT_CHARS:
            original_len = len(content)
            keep_head = int(self.MAX_TOOL_RESULT_CHARS * 0.7)
            keep_tail = self.MAX_TOOL_RESULT_CHARS - keep_head - 200
            head = content[:keep_head]
            tail = content[-keep_tail:]
            content = (
                f"{head}\n\n"
                f"...[truncated {original_len - keep_head - keep_tail} chars from middle; "
                f"original {original_len} chars; "
                f"if you need more, use Read with offset/limit, or re-run the tool with narrower scope]\n\n"
                f"{tail}"
            )

        msg = Message(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=function_name,
        )
        self.live_messages.append(msg)
        return msg

    # ✅
    def _check_cancelled(self) -> bool:
        """Check if agent execution has been cancelled.

        Returns:
            True if cancelled, False otherwise.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    # ✅ 清理未完成的 assistant 消息和其部分工具结果
    def _cleanup_incomplete_messages(self):
        """Remove the incomplete assistant message and its partial tool results.

        This ensures message consistency after cancellation by removing
        only he current step's incomplete messages, preserving completed steps.
        """
        # Find the index of the last assistant message
        last_assistant_idx = -1
        for i in range(len(self.live_messages) - 1, -1, -1):
            if self.live_messages[i].role == "assistant":
                last_assistant_idx = i
                break

        if last_assistant_idx == -1:
            # No assistant message found, nothing to clean
            return

        # Remove the last assistant message and all tool results after it
        removed_count = len(self.live_messages) - last_assistant_idx
        if removed_count > 0:
            self.live_messages = self.live_messages[:last_assistant_idx]
            print(f"{Colors.DIM}   Cleaned up {removed_count} incomplete message(s){Colors.RESET}")

    # ✅把当前发给LLM 的所有消息进行token计数
    def _estimate_tokens(self) -> int:
        """Accurately calculate token count for message history using tiktoken

        Uses cl100k_base encoder (GPT-4/Claude/M2 compatible)
        """
        try:
            # Use cl100k_base encoder (used by GPT-4 and most modern models)
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Fallback: if tiktoken initialization fails, use simple estimation
            return self._estimate_tokens_fallback()

        total_tokens = 0

        for msg in self.render_for_provider():
            # Count text content
            if isinstance(msg.content, str):
                total_tokens += len(encoding.encode(msg.content))
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        # Convert dict to string for calculation
                        total_tokens += len(encoding.encode(str(block)))

            # Count thinking
            if msg.thinking:
                total_tokens += len(encoding.encode(msg.thinking))

            # Count tool_calls
            if msg.tool_calls:
                total_tokens += len(encoding.encode(str(msg.tool_calls)))

            # Metadata overhead per message (approximately 4 tokens)
            total_tokens += 4

        return total_tokens

    # ✅
    def _estimate_tokens_fallback(self) -> int:
        """Fallback token estimation method (when tiktoken is unavailable)"""
        total_chars = 0
        for msg in self.render_for_provider():
            if isinstance(msg.content, str):
                total_chars += len(msg.content)
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        total_chars += len(str(block))

            if msg.thinking:
                total_chars += len(msg.thinking)

            if msg.tool_calls:
                total_chars += len(str(msg.tool_calls))

        # Rough estimation: average 2.5 characters = 1 token
        return int(total_chars / 2.5)

    # --- Cache-aware compaction (IMPROVEMENT_04) ---

    # SUMMARY_INSTRUCTION is a session-level constant string per §3.5.4
    # Version A. Explicit "either fresh or merge" branches eliminate the
    # silent-bug risk where an unguided LLM occasionally drops the prior
    # summary on the floor. Keep it deterministic — no round numbers, no
    # timestamps; those would invalidate the cache prefix.
    SUMMARY_INSTRUCTION = (
        'The system prompt above may contain a "Historical Summary" section '
        "covering earlier rounds. The conversation above shows additional rounds "
        "that haven't been summarized yet. Produce an UPDATED structured summary "
        "that incorporates BOTH the prior summary (if present) and the new rounds, "
        "in this EXACT format:\n\n"
        "## Completed Work\n"
        "- (list what was done)\n\n"
        "## Active Files\n"
        "- (list files that were read/written/modified, with status)\n\n"
        "## Key Findings\n"
        "- (list important discoveries or facts)\n\n"
        "## Pending / TODO\n"
        "- (list unfinished work or next steps)\n\n"
        "Requirements:\n"
        "- Use the exact section headers above\n"
        '- Each item starts with "- "\n'
        "- Be concise, under 800 words total\n"
        "- English only\n"
        "- If no prior summary exists, produce a fresh summary covering only the conversation above"
    )
    # ✅
    CONTENT_TRUNCATE_KEEP_CHARS = 2000
    # ✅
    def _content_truncate_large_tool_results(self):
        """Emergency-only: truncate the *content* of oversized tool messages.

        This is the **only** in-place rewrite of ``live_messages`` that
        survives IMPROVEMENT_04. It only runs from the overflow recovery
        path AFTER a forced DP compaction failed to free enough room.
        On the normal path the DP pipeline never touches this.
        """
        count = 0
        for msg in self.live_messages:
            if msg.role != "tool":
                continue
            if isinstance(msg.content, str) and msg.content.startswith("[Previous "):
                continue
            if isinstance(msg.content, str) and len(msg.content) > self.CONTENT_TRUNCATE_KEEP_CHARS * 2:
                msg.content = (
                    msg.content[:self.CONTENT_TRUNCATE_KEEP_CHARS]
                    + f"\n\n...[content truncated from {len(msg.content)} to "
                    f"{self.CONTENT_TRUNCATE_KEEP_CHARS} chars]"
                )
                count += 1
        if count > 0:
            print(
                f"{Colors.BRIGHT_YELLOW}🔄 Emergency: content-truncated {count} oversized tool result(s){Colors.RESET}"
            )

    # ✅
    def _parse_section(self, text: str, section_name: str) -> list[str]:
        """Pull bullet items out of a ``## Section Name`` block."""
        lines = text.split("\n")
        in_section = False
        items: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"## {section_name}"):
                in_section = True
                continue
            if in_section:
                if stripped.startswith("## "):
                    break
                if stripped.startswith("- "):
                    items.append(stripped[2:])
        return items

    def _render_summary_text(self, user_goals: list[str], summary_text: str) -> str:
        """Compose the final ``raw_text`` that lands in ``current_summary``."""
        parts: list[str] = []
        if user_goals:
            goals_text = "\n".join(f"- {g}" for g in user_goals)
            parts.append(f"## User Goals\n{goals_text}")
        parts.append(summary_text)
        return "\n\n".join(parts)

    # ✅
    @staticmethod
    def _merge_user_goals_preserving_order(
        prior: list[str],
        new: list[str],
    ) -> list[str]:
        """Deduplicate ``prior + new`` while keeping first-seen order.

        Used by both the LLM-summary path and the deterministic fallback
        path so the rendered ``raw_text`` and the ``user_goals`` field
        share a single source of truth — otherwise the LLM could drop a
        prior goal and the prompt-rendering path would silently lose it
        (the field gets fixed up, but ``raw_text`` is what
        ``_render_system_blocks`` actually sends to the model).
        """
        seen: set[str] = set()
        merged: list[str] = []
        for goal in (*prior, *new):
            if goal not in seen:
                seen.add(goal)
                merged.append(goal)
        return merged

    # ✅
    def _parse_structured_summary(
        self,
        summary_text: str,
        dropped: list[Message],
        *,
        prior_user_goals: list[str] | None = None,
    ) -> ContextSummary:
        """Turn raw LLM markdown into a ContextSummary.

        ``prior_user_goals`` lets the caller preserve goals from the
        existing ``current_summary`` even when the LLM drops them: we
        merge BEFORE rendering ``raw_text`` so the rendered prompt and
        the structured field can never disagree.
        """
        new_user_goals = [
            msg.content
            for msg in dropped
            if msg.role == "user" and isinstance(msg.content, str)
        ]
        merged_user_goals = self._merge_user_goals_preserving_order(
            prior_user_goals or [],
            new_user_goals,
        )
        return ContextSummary(
            covered_rounds=[self.compact_count + 1],
            user_goals=merged_user_goals,
            completed_work=self._parse_section(summary_text, "Completed Work"),
            active_files=self._parse_section(summary_text, "Active Files"),
            key_findings=self._parse_section(summary_text, "Key Findings"),
            pending_todo=self._parse_section(summary_text, "Pending / TODO"),
            raw_text=self._render_summary_text(merged_user_goals, summary_text),
        )

    # ✅
    def _count_value_tokens(self, value: object) -> int:
        """Best-effort token count for a single string/dict/list value."""
        if value is None:
            return 0
        enc = self._token_encoder
        if enc is None:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                # Fallback: rough character-based estimate (consistent
                # with _estimate_tokens_fallback ratio of 2.5 chars/token).
                if isinstance(value, str):
                    return int(len(value) / 2.5)
                return int(len(str(value)) / 2.5)
            self._token_encoder = enc
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return len(enc.encode(text))

    # ✅
    def _count_system_tokens(self) -> int:
        """Token count of base + pinned + current_summary (NOT plan).

        Plan is intentionally excluded from the DP stable-prefix V
        estimate: it's the highest-churn block and the spec keeps it
        outside BP #2 specifically so its churn doesn't invalidate the
        summary cache.
        """
        pieces: list[object] = [self._base_system_prompt]
        if self.pinned_notes:
            pieces.append(self.pinned_notes)
        if self.current_summary is not None:
            pieces.append(self.current_summary.raw_text)
        return sum(self._count_value_tokens(piece) + 4 for piece in pieces)

    # ✅
    def _count_tools_tokens(self, tool_list: list) -> int:
        """Token count for the tools schema array."""
        schemas: list[object] = []
        for tool in tool_list:
            if isinstance(tool, dict):
                schemas.append(tool)
            elif hasattr(tool, "to_schema"):
                schemas.append(tool.to_schema())
            elif hasattr(tool, "to_openai_schema"):
                schemas.append(tool.to_openai_schema())
            else:
                schemas.append(str(tool))
        return self._count_value_tokens(schemas)

    # ✅
    def _extract_file_paths_from_tool_args(
        self,
        messages: list[Message],
    ) -> set[str]:
        """Best-effort extraction of file paths from Read/Write/Edit tool calls."""
        paths: set[str] = set()
        path_keys = {"path", "file_path", "filepath", "filename", "target_file", "target_path"}
        for msg in messages:
            if not msg.tool_calls:
                continue
            for call in msg.tool_calls:
                name = (call.function.name or "").lower()
                if not any(marker in name for marker in ("read", "write", "edit", "file")):
                    continue
                args = call.function.arguments or {}
                for key, value in args.items():
                    if key in path_keys and isinstance(value, str) and value:
                        paths.add(value)
        return paths

    # ✅
    def _build_deterministic_fallback_summary(
        self,
        dropped: list[Message],
        *,
        reason: str,
        prior_user_goals: list[str] | None = None,
    ) -> ContextSummary:
        """Lossy but non-empty fallback when the LLM summary call fails on the forced path.

        Captures user_goals verbatim (deterministic, zero-loss), records
        which tools ran, and harvests file paths from tool args.

        ``prior_user_goals`` keeps this path symmetric with
        ``_parse_structured_summary``: both render ``raw_text`` from the
        merged list so the rendered prompt can never lose prior goals.
        """
        new_user_goals = [
            m.content for m in dropped
            if m.role == "user" and isinstance(m.content, str)
        ]
        user_goals = self._merge_user_goals_preserving_order(
            prior_user_goals or [],
            new_user_goals,
        )
        tool_names: set[str] = set()
        for msg in dropped:
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.function.name:
                        tool_names.add(tc.function.name)
        file_paths = self._extract_file_paths_from_tool_args(dropped)

        completed_work = [
            f"(LLM summary unavailable: {reason}; below is deterministic fallback)"
        ]
        key_findings: list[str] = []
        if tool_names:
            key_findings.append(f"Tools invoked: {', '.join(sorted(tool_names))}")

        parts: list[str] = []
        if user_goals:
            parts.append("## User Goals\n" + "\n".join(f"- {g}" for g in user_goals))
        parts.append("## Completed Work\n" + "\n".join(f"- {w}" for w in completed_work))
        if file_paths:
            parts.append("## Active Files\n" + "\n".join(f"- {p}" for p in sorted(file_paths)))
        if key_findings:
            parts.append("## Key Findings\n" + "\n".join(f"- {k}" for k in key_findings))

        raw_text = "\n\n".join(parts)

        return ContextSummary(
            covered_rounds=[self.compact_count + 1],
            user_goals=user_goals,
            completed_work=completed_work,
            active_files=sorted(file_paths),
            key_findings=key_findings,
            pending_todo=[],
            raw_text=raw_text,
        )

    # 把要丢弃的旧消息喂给LLM--归纳成一个ContextSummary
    async def _run_cache_aligned_summary(
        self,
        dropped: list[Message],
        *,
        prior_user_goals: list[str] | None = None,
    ) -> ContextSummary:
        """Issue a summary LLM call that shares the main request's stable prefix.

        Invariants:
        - system blocks and tools are passed in the same order as the main
          request so DeepSeek's automatic Context Caching / Anthropic's
          explicit BP #1/#2/#3 can match.
        - ``dropped`` is passed by reference — never deep-copied, never
          mutated.
        - Router.internal_call passes ``attach_message_bp=False``, so no
          BP #4 lands on the dropped messages. This is the v1 invariant
          that ``P_summary_input = P_input`` in the DP formula.
        - The SUMMARY_INSTRUCTION is appended as a single user message at
          the very end — the only new bytes vs. the main request.

        ``prior_user_goals`` is forwarded to ``_parse_structured_summary``
        so the returned summary's ``user_goals`` and ``raw_text`` both
        reflect the merge of prior + new goals — defending against the
        LLM silently dropping a prior goal during a merge call.
        """
        base_messages = self.render_for_provider()
        system_msg = next((m for m in base_messages if m.role == "system"), None)
        if system_msg is None:
            raise RuntimeError("render_for_provider produced no system message")

        summary_messages: list[Message] = [
            system_msg,
            *dropped,
            Message(role="user", content=self.SUMMARY_INSTRUCTION),
        ]

        tool_list = list(self.tools.values())
        # internal_call -- 
        response = await self.router.internal_call(summary_messages, tools=tool_list)
        return self._parse_structured_summary(
            response.content,
            dropped,
            prior_user_goals=prior_user_goals,
        )

    # ✅
    def _build_compaction_snapshot(self, tool_list: list) -> CompactionSnapshot:
        """Read-only snapshot of agent state for the DP policy.

        ``api_input_token_estimate`` is computed from the CURRENT
        rendering, not from ``last_usage.prompt_tokens``: the latter
        doesn't include tool_results that arrived after the last LLM
        call, and we routinely add big Reads after the call.
        """
        api_estimate = self._estimate_tokens() + self._count_tools_tokens(tool_list)

        pricing = CachePolicy.pricing_for_node(self._primary_node)
        if (
            self._primary_node is not None
            and not getattr(self._primary_node, "supports_explicit_cache_control", False)
            and not getattr(self._primary_node, "supports_automatic_context_cache", False)
        ):
            # Unknown / non-caching node: degrade pricing so the DP
            # discount term collapses (cache_read == cache_write == input).
            pricing = ModelPricing(
                input=pricing.input,
                cache_read=pricing.input,
                cache_write=pricing.input,
                output=pricing.output,
            )

        return CompactionSnapshot(
            live_messages=self.live_messages,
            current_summary=self.current_summary,
            system_token_count=self._count_system_tokens(),
            tools_token_count=self._count_tools_tokens(tool_list),
            pricing=pricing,
            user_turn_count=self.user_turn_count,
            llm_call_count=self.llm_call_count,
            compact_count=self.compact_count,
            api_input_token_estimate=api_estimate,
            max_context=self.token_limit,
        )

    # ✅
    async def _maybe_run_compaction(
        self,
        tool_list: list,
        *,
        forced: bool = False,
    ) -> None:
        """DP-driven compaction. Splits live_messages, summarises dropped, swaps in current_summary."""
        snapshot = self._build_compaction_snapshot(tool_list)
        decision = self.compaction_policy.decide(snapshot, forced=forced)

        if not decision.should_compact:
            return

        dropped = self.live_messages[: decision.drop_message_count]
        kept = self.live_messages[decision.drop_message_count :]

        # Snapshot prior goals BEFORE entering the try/except so both
        # the LLM path and the deterministic fallback path see the same
        # list. Both construction helpers merge prior+new and render
        # raw_text from the merged list — keeping ``user_goals`` and
        # ``raw_text`` in lock-step. (Updating only the field after the
        # fact would leave raw_text stale, and raw_text is what
        # ``_render_system_blocks`` actually sends to the model.)
        prior_user_goals = (
            list(self.current_summary.user_goals) if self.current_summary else []
        )

        try:
            # 把"要丢弃的旧消息"喂给 LLM，让它归纳成一个 ContextSummary -- 问题是prior_user_goals怎么处理 需要仔细看看源码
            summary = await self._run_cache_aligned_summary(
                dropped,
                prior_user_goals=prior_user_goals,
            )
        except Exception as exc:
            if forced:
                # Forced path must move forward or it loops forever.
                # Drop the dropped messages and use a deterministic fallback
                # so user_goals + tool inventory survive.
                summary = self._build_deterministic_fallback_summary(
                    dropped,
                    reason=str(exc),
                    prior_user_goals=prior_user_goals,
                )
            else:
                # Normal path: keep dropped, try again next step. The DP
                # snapshot will re-decide whether compaction is still
                # worth it.
                print(
                    f"{Colors.BRIGHT_YELLOW}⚠️  Summary failed; "
                    f"deferring compaction: {exc}{Colors.RESET}"
                )
                return

        self.current_summary = summary
        self.live_messages = kept
        self.compact_count += 1

        print(
            f"{Colors.BRIGHT_GREEN}✓ Compacted {len(dropped)} messages → summary "
            f"(reason={decision.reason}, "
            f"net_benefit=${decision.net_benefit:.4f}){Colors.RESET}"
        )

    # ---- Router interop: ContextOverflowError recovery ----

    # ✅
    # 对外的 LLM 调用入口，内含 ContextOverflow 三阶段恢复逻辑，外部调用方应走这里而非直接调 router
    async def safe_generate(self, tool_list: list) -> Any:
        """Public entry point: call the LLM with ContextOverflow recovery.

        `Agent.run()` (CLI path) and any external driver that owns this
        Agent should go through here instead of calling
        `self.router.call(...)` directly — otherwise the pre-flight
        `ContextOverflowError` surfaces as a hard crash instead of
        triggering the forced DP compaction + emergency truncation retry
        flow defined by IMPROVEMENT_04 §3.6.

        The method is a thin wrapper around
        `_generate_with_overflow_recovery` and exists so the naming
        signals "drivers should call this, not .llm.generate".
        """
        return await self._generate_with_overflow_recovery(tool_list)

    # ✅
    async def _generate_with_overflow_recovery(self, tool_list: list) -> Any:
        """Call the LLM, recovering from ContextOverflowError via forced compaction.

        Three-step recovery per IMPROVEMENT_04 §3.6:
        1. forced DP compaction → retry
        2. emergency content-truncate large tool_results → retry
        3. propagate if still overflowing

        The router raises ``ContextOverflowError`` either at pre-flight
        (no healthy node fits the current messages) or at event time
        (the provider's own 400 ``context_length_exceeded``). Both are
        the agent's responsibility.
        """
        try:
            return await self.router.call(
                messages=self.render_for_provider(), tools=tool_list
            )
        except ContextOverflowError as exc:
            print(
                f"\n{Colors.BRIGHT_YELLOW}⚠️  ContextOverflow: {exc}. "
                f"Forcing compaction...{Colors.RESET}"
            )

        await self._maybe_run_compaction(tool_list, forced=True)

        try:
            return await self.router.call(
                messages=self.render_for_provider(), tools=tool_list
            )
        except ContextOverflowError as exc2:
            print(
                f"\n{Colors.BRIGHT_YELLOW}⚠️  Still overflow: {exc2}. "
                f"Emergency content truncation...{Colors.RESET}"
            )

        # Last resort: in-place truncate oversized tool results in the
        # kept window. This is the only path that still mutates a
        # message already in live_messages — see IMPROVEMENT_04 §3.3.3.
        self._content_truncate_large_tool_results()

        return await self.router.call(
            messages=self.render_for_provider(), tools=tool_list
        )

    # --- Pinned Notes ---

    MAX_PINNED_CHARS = 4000

    # ✅
    # 从文件加载持久化便签 -- agent 启动时调用 -- 跨会话保留重要上下文
    def load_pinned_notes(self, memory_file: str):
        """Load existing pinned notes from JSON file at startup.

        Uses _pin_note() for each entry so MAX_PINNED_CHARS is enforced.
        """
        path = Path(memory_file)
        if not path.exists():
            return
        try:
            notes = json.loads(path.read_text())
            for note in notes:
                self._pin_note(
                    category=note.get("category", "general"),
                    content=note.get("content", ""),
                )
        except Exception:
            pass

    # ✅
    def _pin_note(self, category: str, content: str):
        """Add a pinned note, drop oldest if over limit.

        Pinned notes used to be baked into ``self.system_prompt`` at
        write time; in IMPROVEMENT_04 they're rendered into the system
        blocks at request time by ``_render_system_blocks``, so we no
        longer keep a duplicate string copy.
        """
        self.pinned_notes.append({"category": category, "content": content})
        total = sum(len(n["content"]) + len(n["category"]) + 10 for n in self.pinned_notes)
        while total > self.MAX_PINNED_CHARS and len(self.pinned_notes) > 1:
            self.pinned_notes.pop(0)
            total = sum(len(n["content"]) + len(n["category"]) + 10 for n in self.pinned_notes)

    # ✅
    # 	agent 主循环，驱动"调用 LLM → 执行工具 → 循环"直到任务完成或到达 max_steps
    async def run(self, cancel_event: Optional[asyncio.Event] = None) -> str:
        """Execute agent loop until task is complete or max steps reached.

        Args:
            cancel_event: Optional asyncio.Event that can be set to cancel execution.
                          When set, the agent will stop at the next safe checkpoint
                          (after completing the current step to keep messages consistent).

        Returns:
            The final response content, or error message (including cancellation message).
        """
        # Set cancellation event (can also be set via self.cancel_event before calling run())
        if cancel_event is not None:
            self.cancel_event = cancel_event

        # Start new run, initialize log file
        self.logger.start_new_run()
        print(f"{Colors.DIM}📝 Log file: {self.logger.get_log_file_path()}{Colors.RESET}")

        step = 0
        # perf_counter() python标准库time模块的高精度计时器
        run_start_time = perf_counter()

        while step < self.max_steps:
            # Check for cancellation at start of each step
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                return cancel_msg

            step_start_time = perf_counter()

            # IMPROVEMENT_04: tool_list must be defined before
            # _maybe_run_compaction so the DP snapshot can count tool
            # schema tokens. The v0 ordering had this assignment after
            # the compression call.
            tool_list = list(self.tools.values())

            # DP-driven compaction (replaces v0 three-level compression).
            # forced=False so the policy can decide NO_OP / net-positive.
            await self._maybe_run_compaction(tool_list, forced=False)

            # Step header with proper width calculation
            BOX_WIDTH = 58
            step_text = f"{Colors.BOLD}{Colors.BRIGHT_CYAN}💭 Step {step + 1}/{self.max_steps}{Colors.RESET}"
            step_display_width = calculate_display_width(step_text)
            padding = max(0, BOX_WIDTH - 1 - step_display_width)  # -1 for leading space

            print(f"\n{Colors.DIM}╭{'─' * BOX_WIDTH}╮{Colors.RESET}")
            print(f"{Colors.DIM}│{Colors.RESET} {step_text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")
            print(f"{Colors.DIM}╰{'─' * BOX_WIDTH}╯{Colors.RESET}")

            # Log LLM request and call LLM with Tool objects directly
            self.logger.log_request(messages=self.render_for_provider(), tools=tool_list)

            try:
                response = await self.safe_generate(tool_list)
            except Exception as e:
                # Check if it's a retry exhausted error
                from .retry import RetryExhaustedError

                if isinstance(e, RetryExhaustedError):
                    error_msg = f"LLM call failed after {e.attempts} retries\nLast error: {str(e.last_exception)}"
                    print(f"\n{Colors.BRIGHT_RED}❌ Retry failed:{Colors.RESET} {error_msg}")
                else:
                    error_msg = f"LLM call failed: {str(e)}"
                    print(f"\n{Colors.BRIGHT_RED}❌ Error:{Colors.RESET} {error_msg}")
                return error_msg

            # Accumulate API reported token usage and update DP inputs.
            if response.usage:
                self.api_total_tokens = response.usage.total_tokens
                self.last_usage = response.usage
            self.llm_call_count += 1

            # Log LLM response
            self.logger.log_response(
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
            )

            # Add assistant message
            self._add_assistant_message(response)

            # Print thinking if present
            if response.thinking:
                print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                print(f"{Colors.DIM}{response.thinking}{Colors.RESET}")

            # Print assistant response
            if response.content:
                print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                print(f"{response.content}")

            # Track whether this step refreshed the session plan. Declared
            # up-front so the no-tool-calls early return can also tick.
            # (Without this, a direct-answer turn never advances the stale
            # counter — causing the reminder to never fire across pure
            # conversational turns.)
            step_touched_plan = False

            # Check if task is complete (no tool calls)
            if not response.tool_calls:
                # A direct-answer turn still counts as a "step" during
                # which the plan was NOT refreshed, so tick here.
                if self.planning_manager is not None:
                    self.planning_manager.note_round_without_update()
                step_elapsed = perf_counter() - step_start_time
                total_elapsed = perf_counter() - run_start_time
                print(f"\n{Colors.DIM}⏱️  Step {step + 1} completed in {step_elapsed:.2f}s (total: {total_elapsed:.2f}s){Colors.RESET}")
                return response.content

            # Check for cancellation before executing tools
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                return cancel_msg

            # Execute tool calls
            for tool_call in response.tool_calls:
                tool_call_id = tool_call.id
                function_name = tool_call.function.name
                arguments = tool_call.function.arguments

                # Tool call header
                print(f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{function_name}{Colors.RESET}")

                # Arguments (formatted display)
                print(f"{Colors.DIM}   Arguments:{Colors.RESET}")
                # Truncate each argument value to avoid overly long output
                truncated_args = {}
                for key, value in arguments.items():
                    value_str = str(value)
                    if len(value_str) > 200:
                        truncated_args[key] = value_str[:200] + "..."
                    else:
                        truncated_args[key] = value
                args_json = json.dumps(truncated_args, indent=2, ensure_ascii=False)
                for line in args_json.split("\n"):
                    print(f"   {Colors.DIM}{line}{Colors.RESET}")

                # Unknown tool: short-circuit before permission gate.
                # A hallucinated tool name has nothing to approve.
                result: Optional[ToolResult] = None
                permission_decision: Optional[PermissionDecision] = None
                if function_name not in self.tools:
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Unknown tool: {function_name}",
                    )

                # Permission gate (only for known tools).
                if result is None and self.permission_manager is not None:
                    # check() --- 返回一个PermissionDecision 对象
                    # behavior: allow, deny, ask
                    permission_decision = self.permission_manager.check(
                        function_name, arguments
                    )

                    if permission_decision.behavior == "deny":
                        print(
                            f"{Colors.BRIGHT_RED}⛔ Denied:{Colors.RESET} "
                            f"{Colors.RED}{permission_decision.reason}{Colors.RESET}"
                        )
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"Permission denied: {permission_decision.reason}",
                        )
                    elif permission_decision.behavior == "ask":
                        approved = False
                        if self.approval_callback is not None:
                            try:
                                # ✅ 调用 approval_callback 回调函数，让用户决定是否允许工具调用
                                # 这里的 approval_callback 是来自 cli.py 中的 request_tool_approval 函数
                                approved = await self.approval_callback(
                                    function_name,
                                    arguments,
                                    permission_decision.reason,
                                )
                            except Exception as approval_exc:
                                # A broken approval callback must not execute
                                # the tool. Log and deny.
                                approved = False
                                print(
                                    f"{Colors.BRIGHT_RED}⛔ Approval callback failed:{Colors.RESET} "
                                    f"{Colors.RED}{approval_exc}{Colors.RESET}"
                                )
                        else:
                            # Non-interactive mode (e.g. --task): no one to ask.
                            print(
                                f"{Colors.BRIGHT_RED}⛔ Denied (non-interactive):{Colors.RESET} "
                                f"{Colors.RED}{permission_decision.reason}{Colors.RESET}"
                            )
                        if not approved:
                            reason_tag = (
                                "user rejected"
                                if self.approval_callback is not None
                                else "no approval callback (ask→deny)"
                            )
                            result = ToolResult(
                                success=False,
                                content="",
                                error=(
                                    f"Permission denied: {reason_tag}. "
                                    f"Original decision: {permission_decision.reason}"
                                ),
                            )
                    # On "allow", result stays None and the tool executes below.

                # Execute tool if gate did not block.
                if result is None:
                    try:
                        tool = self.tools[function_name]
                        result = await tool.execute(**arguments)
                    except Exception as e:
                        # Catch all exceptions during tool execution, convert to failed ToolResult
                        import traceback

                        error_detail = f"{type(e).__name__}: {str(e)}"
                        error_trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"Tool execution failed: {error_detail}\n\nTraceback:\n{error_trace}",
                        )

                # Log tool execution result, including the permission
                # decision when the gate ran. `permission_decision` is
                # ``None`` when no manager is attached OR when the tool
                # was rejected earlier as Unknown (in which case there is
                # nothing meaningful to approve).
                self.logger.log_tool_result(
                    tool_name=function_name,
                    arguments=arguments,
                    result_success=result.success,
                    result_content=result.content if result.success else None,
                    result_error=result.error if not result.success else None,
                    permission_behavior=(
                        permission_decision.behavior
                        if permission_decision is not None
                        else None
                    ),
                    permission_reason=(
                        permission_decision.reason
                        if permission_decision is not None
                        else None
                    ),
                )

                # Print result
                if result.success:
                    result_text = result.content
                    if len(result_text) > 300:
                        result_text = result_text[:300] + f"{Colors.DIM}...{Colors.RESET}"
                    print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {result_text}")
                else:
                    print(f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} {Colors.RED}{result.error}{Colors.RESET}")

                # Add tool result message
                self._add_tool_message(tool_call_id, function_name, result)

                # Intercept record_note: pin to system prompt
                if function_name == "record_note" and result.success:
                    self._pin_note(
                        category=arguments.get("category", "general"),
                        content=arguments.get("content", ""),
                    )

                # Intercept todo_write: the stale-plan counter has already
                # been reset by PlanningManager.update() on success; here
                # we just remember that a refresh happened this step.
                if function_name == "todo_write" and result.success:
                    step_touched_plan = True

                # Check for cancellation after each tool execution
                if self._check_cancelled():
                    self._cleanup_incomplete_messages()
                    cancel_msg = "Task cancelled by user."
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                    return cancel_msg

            # Tick the stale-plan counter if the step didn't refresh the
            # plan. Only meaningful when there IS a plan — the manager
            # itself guards against ticking on an empty plan.
            if self.planning_manager is not None and not step_touched_plan:
                self.planning_manager.note_round_without_update()

            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time
            print(f"\n{Colors.DIM}⏱️  Step {step + 1} completed in {step_elapsed:.2f}s (total: {total_elapsed:.2f}s){Colors.RESET}")

            step += 1

        # Max steps reached
        error_msg = f"Task couldn't be completed after {self.max_steps} steps."
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {error_msg}{Colors.RESET}")
        return error_msg

    # ✅
    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.render_for_provider().copy()
