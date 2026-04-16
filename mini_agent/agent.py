"""Core Agent implementation."""

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Optional

import tiktoken

from .llm import LLMClient
from .logger import AgentLogger
from .schema import ContextSummary, Message
from .tools.base import Tool, ToolResult
from .utils import calculate_display_width


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

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,
        workspace_dir: str = "./workspace",
        token_limit: int = 80000,  # Summary triggered when tokens exceed this value
    ):
        self.llm = llm_client
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.workspace_dir = Path(workspace_dir)
        # Cancellation event for interrupting agent execution (set externally, e.g., by Esc key)
        self.cancel_event: Optional[asyncio.Event] = None

        # Ensure workspace exists
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Inject workspace information into system prompt if not already present
        if "Current Workspace" not in system_prompt:
            workspace_info = f"\n\n## Current Workspace\nYou are currently working in: `{self.workspace_dir.absolute()}`\nAll relative paths will be resolved relative to this directory."
            system_prompt = system_prompt + workspace_info

        # Split internal storage: system_prompt / pinned_notes / cold_summaries / live_messages
        self._base_system_prompt: str = system_prompt  # Original system prompt (without pinned notes)
        self.system_prompt: str = system_prompt  # Current system prompt (with pinned notes)
        self.pinned_notes: list[dict] = []  # Pinned meta-info
        self.cold_summaries: list[ContextSummary] = []  # L4 summaries (can have multiple)
        self.live_messages: list[Message] = []  # Recent raw messages

        # Initialize logger
        self.logger = AgentLogger()

        # Token usage from last API response (updated after each LLM call)
        self.api_total_tokens: int = 0
        # Flag to skip token check right after summary (avoid consecutive triggers)
        self._skip_next_token_check: bool = False

    # --- Backward-compatible messages property ---

    @property
    def messages(self) -> list[Message]:
        """Backward compatible: return full message list view (read-only)."""
        return self.render_for_provider()

    @messages.setter
    def messages(self, value: list[Message]):
        """Backward compatible: support agent.messages = [agent.messages[0]] for /clear."""
        # /clear scenario: only keep system prompt
        if len(value) == 1 and value[0].role == "system":
            self.live_messages = []
            self.cold_summaries = []
            # pinned_notes survive /clear
            return
        # Other scenarios: replace live_messages
        self.live_messages = [m for m in value if m.role != "system"]

    # --- Render for provider ---

    def render_for_provider(self) -> list[Message]:
        """Assemble internal storage into API-legal message sequence."""
        result = []

        # 1. System prompt (with pinned notes) + cold summaries (appended at render time)
        system_content = self.system_prompt  # Already contains pinned notes
        if self.cold_summaries:
            merged = "\n\n---\n\n".join(s.raw_text for s in self.cold_summaries)
            system_content += f"\n\n## Historical Summary\n{merged}"
        result.append(Message(role="system", content=system_content))

        # 2. Recent raw messages
        result.extend(self.live_messages)

        return result

    # --- Message append methods ---

    def add_user_message(self, content: str):
        """Add a user message to history."""
        self.live_messages.append(Message(role="user", content=content))

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

    def _add_tool_message(self, tool_call_id: str, function_name: str, result: ToolResult) -> Message:
        """Add a tool result message."""
        msg = Message(
            role="tool",
            content=result.content if result.success else f"Error: {result.error}",
            tool_call_id=tool_call_id,
            name=function_name,
        )
        self.live_messages.append(msg)
        return msg

    def _check_cancelled(self) -> bool:
        """Check if agent execution has been cancelled.

        Returns:
            True if cancelled, False otherwise.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    def _cleanup_incomplete_messages(self):
        """Remove the incomplete assistant message and its partial tool results.

        This ensures message consistency after cancellation by removing
        only the current step's incomplete messages, preserving completed steps.
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

    # --- Three-level compression ---

    async def _compress_context(self):
        """Three-level compression (preventive + forced).

        Strategy:
        - soft_limit (85%): trigger L1/L2 lightweight truncation early, near-zero cost
        - hard_limit (100%): trigger L4 full LLM summary, higher cost
        This avoids sudden context overflow; L1/L2 is usually sufficient.
        """
        if self._skip_next_token_check:
            self._skip_next_token_check = False
            return

        estimated = self._estimate_tokens()
        soft_limit = int(self.token_limit * 0.85)

        # Below soft_limit, no compression needed
        if estimated <= soft_limit and self.api_total_tokens <= soft_limit:
            return

        print(
            f"\n{Colors.BRIGHT_YELLOW}📊 Token: local={estimated}, api={self.api_total_tokens}, "
            f"soft={soft_limit}, hard={self.token_limit}{Colors.RESET}"
        )

        # L1: Truncate old tool results (non read_file), near-zero cost
        self._truncate_old_tool_results(keep_recent_n=3)
        estimated = self._estimate_tokens()
        if estimated <= self.token_limit:
            print(f"{Colors.BRIGHT_GREEN}✓ L1 sufficient: {estimated} tokens{Colors.RESET}")
            return

        # L2: Truncate old read_file results, near-zero cost
        self._truncate_old_readfile_results(keep_recent_n=3)
        estimated = self._estimate_tokens()
        if estimated <= self.token_limit:
            print(f"{Colors.BRIGHT_GREEN}✓ L2 sufficient: {estimated} tokens{Colors.RESET}")
            return

        # L4: Full compression (keep recent N rounds), only when truly over hard limit
        await self._full_compress(keep_recent_n=3)
        self._skip_next_token_check = True

        # Post-L4 safety: the kept rounds themselves may still exceed the limit.
        # Step 1: L1/L2 with keep_recent_n=1 (protect only the latest round)
        # Step 2: content-truncate oversized tool messages (last resort)
        estimated = self._estimate_tokens()
        if estimated > self.token_limit:
            print(
                f"{Colors.BRIGHT_YELLOW}⚠️  Post-L4 still over limit ({estimated} > {self.token_limit}), "
                f"applying aggressive truncation{Colors.RESET}"
            )
            self._truncate_old_tool_results(keep_recent_n=1)
            self._truncate_old_readfile_results(keep_recent_n=1)
            estimated = self._estimate_tokens()
            if estimated > self.token_limit:
                # Oversized tool results in current round — content-truncate them
                self._content_truncate_large_tool_results()
                estimated = self._estimate_tokens()
            print(f"{Colors.BRIGHT_GREEN}✓ Post-L4 cleanup: {estimated} tokens{Colors.RESET}")

    def _get_round_boundary(self, keep_recent_n: int) -> int:
        """Return the starting index in live_messages for the most recent N rounds.
        One round = one user message to the next user message (all messages in between).
        """
        user_indices = [
            i for i, msg in enumerate(self.live_messages) if msg.role == "user"
        ]
        if len(user_indices) <= keep_recent_n:
            return 0  # Not enough rounds, keep all
        # Starting index of the most recent N rounds' first user message
        return user_indices[-keep_recent_n]

    def _truncate_old_tool_results(self, keep_recent_n: int = 3):
        """L1: Replace non-read_file tool results before N rounds with placeholders."""
        boundary = self._get_round_boundary(keep_recent_n)
        if boundary == 0:
            return

        count = 0
        for msg in self.live_messages[:boundary]:
            if msg.role == "tool" and msg.name and msg.name != "read_file":
                # Avoid re-truncation
                if not msg.content.startswith("[Previous "):
                    failed = msg.content.startswith("Error:")
                    status = "failed" if failed else "executed successfully"
                    msg.content = f"[Previous {msg.name} {status}]"
                    count += 1

        if count > 0:
            print(f"{Colors.BRIGHT_YELLOW}🔄 L1: truncated {count} old tool results{Colors.RESET}")

    def _truncate_old_readfile_results(self, keep_recent_n: int = 3):
        """L2: Replace read_file results before N rounds with path-bearing placeholders."""
        boundary = self._get_round_boundary(keep_recent_n)
        if boundary == 0:
            return

        # Build tool_call_id → arguments index (from assistant messages in live_messages)
        args_index = {}
        for msg in self.live_messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    args_index[tc.id] = tc.function.arguments

        count = 0
        for msg in self.live_messages[:boundary]:
            if msg.role == "tool" and msg.name == "read_file":
                if not msg.content.startswith("[Previous "):
                    args = args_index.get(msg.tool_call_id, {})
                    path = args.get("path", "unknown")
                    offset = args.get("offset")
                    limit = args.get("limit")
                    if offset or limit:
                        params = ", ".join(
                            f"{k}={v}" for k, v in [("offset", offset), ("limit", limit)] if v
                        )
                        msg.content = f"[Previous read_file: {path} ({params})]"
                    else:
                        msg.content = f"[Previous read_file: {path}]"
                    count += 1

        if count > 0:
            print(f"{Colors.BRIGHT_YELLOW}🔄 L2: truncated {count} old read_file results{Colors.RESET}")

    # Characters to keep when content-truncating an oversized tool result.
    # ~2000 chars ≈ ~500-800 tokens — enough for LLM to understand the gist.
    CONTENT_TRUNCATE_KEEP_CHARS = 2000

    def _content_truncate_large_tool_results(self):
        """Last resort: truncate the *content* of oversized tool messages.

        Unlike L1/L2 which replace with placeholders, this keeps the first N
        characters so the LLM still gets partial context.  Applied to ALL
        tool messages in live_messages (including the current round).
        """
        count = 0
        for msg in self.live_messages:
            if msg.role != "tool":
                continue
            if msg.content.startswith("[Previous "):
                continue  # Already placeholder-truncated
            if len(msg.content) > self.CONTENT_TRUNCATE_KEEP_CHARS * 2:
                msg.content = (
                    msg.content[:self.CONTENT_TRUNCATE_KEEP_CHARS]
                    + f"\n\n...[content truncated from {len(msg.content)} to "
                    f"{self.CONTENT_TRUNCATE_KEEP_CHARS} chars]"
                )
                count += 1

        if count > 0:
            print(f"{Colors.BRIGHT_YELLOW}🔄 Content-truncated {count} oversized tool result(s){Colors.RESET}")

    async def _full_compress(self, keep_recent_n: int = 3):
        """L4: Compress messages before N rounds into ContextSummary, keep recent N rounds."""
        boundary = self._get_round_boundary(keep_recent_n)
        if boundary == 0:
            return

        old_messages = self.live_messages[:boundary]
        recent_messages = self.live_messages[boundary:]

        # 1. Extract old user prompts (deterministic, zero-loss)
        user_goals = [
            msg.content for msg in old_messages
            if msg.role == "user" and isinstance(msg.content, str)
        ]

        # 2. LLM structured summary
        summary_text = await self._create_structured_summary(old_messages)

        # 3. Parse structured summary (best-effort; fallback to raw_text)
        summary = ContextSummary(
            covered_rounds=list(range(1, len([m for m in old_messages if m.role == "user"]) + 1)),
            user_goals=user_goals,
            completed_work=self._parse_section(summary_text, "Completed Work"),
            active_files=self._parse_section(summary_text, "Active Files"),
            key_findings=self._parse_section(summary_text, "Key Findings"),
            pending_todo=self._parse_section(summary_text, "Pending / TODO"),
            raw_text=self._render_summary_text(user_goals, summary_text),
        )

        # 4. Append to cold_summaries, replace live_messages
        self.cold_summaries.append(summary)
        self.live_messages = recent_messages

        print(
            f"{Colors.BRIGHT_GREEN}✓ L4: compressed {len(old_messages)} messages → summary, "
            f"kept {len(recent_messages)} recent{Colors.RESET}"
        )

    def _render_summary_text(self, user_goals: list[str], summary_text: str) -> str:
        """Render user_goals + summary text into a full block (for render_for_provider)."""
        parts = []
        if user_goals:
            goals_text = "\n".join(f"- {g}" for g in user_goals)
            parts.append(f"## User Goals\n{goals_text}")
        parts.append(summary_text)
        return "\n\n".join(parts)

    def _parse_section(self, text: str, section_name: str) -> list[str]:
        """Parse items from a specific section in structured summary text."""
        lines = text.split("\n")
        in_section = False
        items = []
        for line in lines:
            if line.strip().startswith(f"## {section_name}"):
                in_section = True
                continue
            if in_section:
                if line.strip().startswith("## "):
                    break
                if line.strip().startswith("- "):
                    items.append(line.strip()[2:])
        return items

    async def _create_structured_summary(self, messages: list[Message]) -> str:
        """Call LLM to generate a structured summary."""
        content = ""
        for msg in messages:
            if msg.role == "assistant":
                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                content += f"Assistant: {text}\n"
                if msg.tool_calls:
                    names = [tc.function.name for tc in msg.tool_calls]
                    content += f"  → Tools: {', '.join(names)}\n"
            elif msg.role == "tool":
                content += f"  ← {msg.name}: {msg.content}\n"

        prompt = f"""Summarize the following agent execution history in this EXACT format:

## Completed Work
- (list what was done)

## Active Files
- (list files that were read/written/modified, with status)

## Key Findings
- (list important discoveries or facts)

## Pending / TODO
- (list unfinished work or next steps)

---
Execution history:

{content}

Requirements:
- Use the exact section headers above
- Each item starts with "- "
- Be concise, under 800 words total
- English only"""

        try:
            response = await self.llm.generate(messages=[
                Message(role="system", content="You summarize agent execution histories in structured format."),
                Message(role="user", content=prompt),
            ])
            return response.content
        except Exception:
            # Fallback: return truncated raw content
            return (
                f"## Completed Work\n- (Summary generation failed)\n\n"
                f"## Key Findings\n- Raw content length: {len(content)} chars"
            )

    # --- Pinned Notes ---

    MAX_PINNED_CHARS = 4000

    def _rebuild_system_prompt(self):
        """Inject pinned notes into system prompt."""
        if not self.pinned_notes:
            self.system_prompt = self._base_system_prompt
            return

        notes_section = "\n\n## Pinned Context (Important - Always Available)\n"
        for note in self.pinned_notes:
            cat = note.get("category", "general")
            content = note.get("content", "")
            notes_section += f"- [{cat}] {content}\n"

        self.system_prompt = self._base_system_prompt + notes_section

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

    def _pin_note(self, category: str, content: str):
        """Add a pinned note, drop oldest if over limit."""
        self.pinned_notes.append({"category": category, "content": content})
        # Check total length
        total = sum(len(n["content"]) + len(n["category"]) + 10 for n in self.pinned_notes)
        while total > self.MAX_PINNED_CHARS and len(self.pinned_notes) > 1:
            self.pinned_notes.pop(0)
            total = sum(len(n["content"]) + len(n["category"]) + 10 for n in self.pinned_notes)
        self._rebuild_system_prompt()

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
        run_start_time = perf_counter()

        while step < self.max_steps:
            # Check for cancellation at start of each step
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                return cancel_msg

            step_start_time = perf_counter()
            # Check and compress context to prevent overflow
            await self._compress_context()

            # Step header with proper width calculation
            BOX_WIDTH = 58
            step_text = f"{Colors.BOLD}{Colors.BRIGHT_CYAN}💭 Step {step + 1}/{self.max_steps}{Colors.RESET}"
            step_display_width = calculate_display_width(step_text)
            padding = max(0, BOX_WIDTH - 1 - step_display_width)  # -1 for leading space

            print(f"\n{Colors.DIM}╭{'─' * BOX_WIDTH}╮{Colors.RESET}")
            print(f"{Colors.DIM}│{Colors.RESET} {step_text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")
            print(f"{Colors.DIM}╰{'─' * BOX_WIDTH}╯{Colors.RESET}")

            # Get tool list for LLM call
            tool_list = list(self.tools.values())

            # Log LLM request and call LLM with Tool objects directly
            self.logger.log_request(messages=self.render_for_provider(), tools=tool_list)

            try:
                response = await self.llm.generate(messages=self.render_for_provider(), tools=tool_list)
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

            # Accumulate API reported token usage
            if response.usage:
                self.api_total_tokens = response.usage.total_tokens

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

            # Check if task is complete (no tool calls)
            if not response.tool_calls:
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

                # Execute tool
                if function_name not in self.tools:
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Unknown tool: {function_name}",
                    )
                else:
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

                # Log tool execution result
                self.logger.log_tool_result(
                    tool_name=function_name,
                    arguments=arguments,
                    result_success=result.success,
                    result_content=result.content if result.success else None,
                    result_error=result.error if not result.success else None,
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

                # Check for cancellation after each tool execution
                if self._check_cancelled():
                    self._cleanup_incomplete_messages()
                    cancel_msg = "Task cancelled by user."
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                    return cancel_msg

            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time
            print(f"\n{Colors.DIM}⏱️  Step {step + 1} completed in {step_elapsed:.2f}s (total: {total_elapsed:.2f}s){Colors.RESET}")

            step += 1

        # Max steps reached
        error_msg = f"Task couldn't be completed after {self.max_steps} steps."
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {error_msg}{Colors.RESET}")
        return error_msg

    def get_history(self) -> list[Message]:
        """Get message history."""
        return self.render_for_provider().copy()
