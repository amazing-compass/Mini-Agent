"""Anthropic LLM client implementation."""

import logging
from typing import Any

import anthropic

from ..retry import RetryConfig, async_retry
from ..schema import FunctionCall, LLMResponse, Message, TokenUsage, ToolCall
from .base import LLMClientBase
from .ha.errors import LLMError, normalize_sdk_error

logger = logging.getLogger(__name__)


class AnthropicClient(LLMClientBase):
    """LLM client using Anthropic's protocol.

    This client uses the official Anthropic SDK and supports:
    - Extended thinking content
    - Tool calling
    - Retry logic
    """

    # Anthropic SDK requires `max_tokens` on every request (it has no
    # "unlimited" sentinel). Preserve the pre-Phase-2 hardcoded value
    # so direct/ACP callers that never configure the knob keep working
    # the same way. Router-driven calls always pass an explicit value.
    _LEGACY_MAX_TOKENS = 16384

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.minimaxi.com/anthropic",
        model: str = "MiniMax-M2.5",
        retry_config: RetryConfig | None = None,
        *,
        default_max_tokens: int | None = None,
    ):
        """Initialize Anthropic client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API (default: MiniMax Anthropic endpoint)
            model: Model name to use (default: MiniMax-M2.5)
            retry_config: Optional retry configuration
            default_max_tokens: Fallback for `generate(max_tokens=...)`.
        """
        super().__init__(api_key, api_base, model, retry_config, default_max_tokens=default_max_tokens)

        # Initialize Anthropic async client
        self.client = anthropic.AsyncAnthropic(
            base_url=api_base,
            api_key=api_key,
            default_headers={"Authorization": f"Bearer {api_key}"},
        )

    async def _make_api_request(
        self,
        system_message: Any,
        api_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int,
    ) -> anthropic.types.Message:
        """Execute API request (core method that can be retried).

        Args:
            system_message: Optional system content. May be either a
                plain string (legacy callers) or an Anthropic
                ``list[TextBlockParam]`` carrying ``cache_control``
                markers (cache-aware callers).
            api_messages: List of messages in Anthropic-protocol shape.
            tools: Optional list of tools, *already* converted to
                Anthropic-shape dicts by the caller. The original
                ``_convert_tools`` call site lives in ``_prepare_request``
                so it can apply ``cache_last_tool`` consistently.
            max_tokens: Output budget for this call (computed by the router).

        Returns:
            Anthropic Message response

        Raises:
            LLMError subclass: normalized from the SDK exception so the router
            and retry layer never see vendor-specific error types.
        """
        params = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }

        if system_message:
            params["system"] = system_message

        if tools:
            params["tools"] = tools

        try:
            return await self.client.messages.create(**params)
        except LLMError:
            raise  # already normalized — don't double-wrap
        except Exception as exc:
            raise normalize_sdk_error(exc) from exc

    def _convert_tools(
        self,
        tools: list[Any],
        *,
        cache_last_tool: bool = False,
    ) -> list[dict[str, Any]]:
        """Convert tools to Anthropic format.

        Anthropic tool format:
        {
            "name": "tool_name",
            "description": "Tool description",
            "input_schema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }

        When ``cache_last_tool=True``, a shallow-copy of the last entry
        gets a top-level ``cache_control`` marker (BP #3). The shallow
        copy is critical: the Anthropic-shape branch below reuses the
        caller's dict reference, and we must not write request-time
        cache metadata back onto ``Agent.tools``.

        Args:
            tools: List of Tool objects or dicts
            cache_last_tool: Whether to attach a cache_control breakpoint
                to the last tool (Anthropic-only behavior, gated by the
                node's ``supports_explicit_cache_control``).

        Returns:
            List of tools in Anthropic dict format
        """
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                # If already in OpenAI function tool format, convert to Anthropic.
                if tool.get("type") == "function" and "function" in tool:
                    function = tool["function"]
                    result.append(
                        {
                            "name": function["name"],
                            "description": function.get("description", ""),
                            "input_schema": function["parameters"],
                        }
                    )
                else:
                    # Assume it's already in Anthropic format.
                    result.append(tool)
            elif hasattr(tool, "to_schema"):
                # Tool object with to_schema method
                result.append(tool.to_schema())
            else:
                raise TypeError(f"Unsupported tool type: {type(tool)}")

        if cache_last_tool and result:
            last = result[-1]
            if isinstance(last, dict):
                result[-1] = {**last, "cache_control": {"type": "ephemeral"}}
        return result

    def _convert_messages(
        self,
        messages: list[Message],
        *,
        attach_message_bp: bool = True,
        enable_cache_control: bool = False,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Convert internal messages to Anthropic format.

        Important: when an assistant message contains multiple ``tool_use``
        blocks (parallel tool calls), the **immediately following user
        message must contain all matching ``tool_result`` blocks together**.
        Strict Anthropic-compatible endpoints (e.g. DeepSeek's
        ``/anthropic`` endpoint) reject the request with
        ``400 invalid_request_error: tool_use ids were found without
        tool_result blocks immediately after`` when results are split
        across multiple user messages.

        We therefore batch consecutive ``role="tool"`` messages into a
        single user message whose ``content`` is a list of all collected
        ``tool_result`` blocks. The order is preserved so each ``tool_use``
        is matched with its corresponding result.

        Cache behavior (Anthropic explicit cache only):
        - When ``enable_cache_control=False`` we defensively strip any
          ``cache_control`` markers from the system message and message
          content blocks; this prevents DeepSeek/OpenAI/MiniMax endpoints
          from ever seeing the marker even if it leaked in.
        - When ``enable_cache_control=True`` AND ``attach_message_bp=True``
          we dynamically attach BP #4 to the last stable assistant
          message in ``api_messages`` (string content → upgraded to a
          block list; thinking-only blocks are skipped; the original
          ``Message`` is never mutated).
        - When ``enable_cache_control=True`` but ``attach_message_bp=False``
          (summary/internal_call path), BP #4 is intentionally NOT
          attached so we avoid paying ``cache_write`` for a prefix no
          future request will read.

        Args:
            messages: List of internal Message objects.
            attach_message_bp: Whether to inject BP #4 on the trailing
                stable assistant message (main-path only).
            enable_cache_control: Whether the target node supports
                Anthropic explicit ``cache_control`` markers.

        Returns:
            Tuple of (system_message, api_messages).
        """
        system_message: Any = None
        api_messages: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        def flush_tool_results() -> None:
            """Emit accumulated tool_result blocks as one user message."""
            if pending_tool_results:
                api_messages.append({
                    "role": "user",
                    "content": list(pending_tool_results),
                })
                pending_tool_results.clear()

        for msg in messages:
            if msg.role == "system":
                # ``content`` may be ``str`` or ``list[dict]``. Anthropic
                # SDK accepts both shapes; we pass through verbatim.
                system_message = msg.content
                continue

            # Tool results: accumulate; do not flush until a non-tool
            # message arrives. Multiple consecutive tool results from
            # parallel tool calls collapse into a single user message.
            if msg.role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                })
                continue

            # Any non-tool message implicitly flushes the pending batch
            # (Anthropic requires tool_results immediately after tool_use).
            flush_tool_results()

            # User / assistant messages.
            if msg.role in ("user", "assistant"):
                if msg.role == "assistant" and (msg.thinking or msg.tool_calls):
                    content_blocks: list[dict[str, Any]] = []

                    if msg.thinking:
                        content_blocks.append({"type": "thinking", "thinking": msg.thinking})

                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})

                    if msg.tool_calls:
                        for tool_call in msg.tool_calls:
                            content_blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": tool_call.id,
                                    "name": tool_call.function.name,
                                    "input": tool_call.function.arguments,
                                }
                            )

                    api_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    api_messages.append({"role": msg.role, "content": msg.content})

        # End of message list: flush any trailing tool_results so they
        # don't get silently dropped (rare — would mean the conversation
        # ends on a tool result, which the model never sees).
        flush_tool_results()

        if not enable_cache_control:
            system_message = _strip_cache_control_from_system(system_message)
            api_messages = _strip_cache_control_from_messages(api_messages)
            return system_message, api_messages

        if attach_message_bp:
            self._attach_bp4(api_messages)

        return system_message, api_messages

    @staticmethod
    def _attach_bp4(api_messages: list[dict[str, Any]]) -> None:
        """Attach BP #4 to the last stable assistant message in-place.

        Walks ``api_messages`` (request dicts, not Message objects) from
        the end and finds the last assistant entry. Three edge cases:
        - content is a string → upgrade to a single-text-block list
          (Anthropic requires block-form for cache_control)
        - content is an empty list → skip (nothing to attach to)
        - content is a list but all entries are ``thinking`` blocks →
          skip (Anthropic 400s when cache_control lands on thinking)

        Mutates the request-dict that this method builds locally; never
        touches the original ``Message`` objects.
        """
        last_asst_idx: int | None = None
        for i in range(len(api_messages) - 1, -1, -1):
            if api_messages[i].get("role") == "assistant":
                last_asst_idx = i
                break
        if last_asst_idx is None:
            return

        content = api_messages[last_asst_idx].get("content")

        if isinstance(content, str):
            api_messages[last_asst_idx]["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            return

        if not isinstance(content, list) or not content:
            return

        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and block.get("type") != "thinking":
                # Shallow-copy the list so we never mutate the underlying
                # Message.content list when the request dict shares its
                # reference (see _convert_messages line ~275).
                new_content = list(content)
                new_content[i] = {**block, "cache_control": {"type": "ephemeral"}}
                api_messages[last_asst_idx]["content"] = new_content
                return

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        attach_message_bp: bool = True,
        enable_cache_control: bool = False,
    ) -> dict[str, Any]:
        """Prepare the request for Anthropic API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            attach_message_bp: Forwarded to ``_convert_messages`` for BP #4
                attachment on the trailing stable assistant.
            enable_cache_control: Whether to actually emit any Anthropic
                ``cache_control`` markers. When ``False`` the message
                content blocks are stripped and the tool BP #3 is omitted.

        Returns:
            Dictionary containing request parameters
        """
        system_message, api_messages = self._convert_messages(
            messages,
            attach_message_bp=attach_message_bp,
            enable_cache_control=enable_cache_control,
        )

        api_tools: list[dict[str, Any]] | None = None
        if tools:
            api_tools = self._convert_tools(
                tools,
                cache_last_tool=enable_cache_control,
            )
            if not enable_cache_control:
                api_tools = _strip_cache_control_from_tools(api_tools)

        return {
            "system_message": system_message,
            "api_messages": api_messages,
            "tools": api_tools,
        }

    def _parse_response(self, response: anthropic.types.Message) -> LLMResponse:
        """Parse Anthropic response into LLMResponse.

        Args:
            response: Anthropic Message response

        Returns:
            LLMResponse object
        """
        # Extract text content, thinking, and tool calls
        text_content = ""
        thinking_content = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_content += block.text
            elif block.type == "thinking":
                thinking_content += block.thinking
            elif block.type == "tool_use":
                # Parse Anthropic tool_use block
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        type="function",
                        function=FunctionCall(
                            name=block.name,
                            arguments=block.input,
                        ),
                    )
                )

        # Extract token usage from response.
        # Two shapes coexist:
        # - DeepSeek Anthropic-compatible endpoint may surface
        #   ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
        #   (their automatic Context Caching telemetry).
        # - Anthropic-standard responses use ``input_tokens`` +
        #   ``cache_read_input_tokens`` + ``cache_creation_input_tokens``.
        # We support both paths so we keep cache visibility even when
        # the provider tweaks the field names.
        usage = None
        if hasattr(response, "usage") and response.usage:
            usage_obj = response.usage
            output_tokens = getattr(usage_obj, "output_tokens", 0) or 0

            deepseek_hit = getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0
            deepseek_miss = getattr(usage_obj, "prompt_cache_miss_tokens", 0) or 0

            if deepseek_hit or deepseek_miss:
                input_tokens = deepseek_miss
                cache_read = deepseek_hit
                cache_creation = 0
            else:
                input_tokens = getattr(usage_obj, "input_tokens", 0) or 0
                cache_read = getattr(usage_obj, "cache_read_input_tokens", 0) or 0
                cache_creation = getattr(usage_obj, "cache_creation_input_tokens", 0) or 0

            total_input = input_tokens + cache_read + cache_creation
            usage = TokenUsage(
                prompt_tokens=total_input,
                completion_tokens=output_tokens,
                total_tokens=total_input + output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                cache_miss_tokens=input_tokens,
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=response.stop_reason or "stop",
            usage=usage,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        max_tokens: int | None = None,
        attach_message_bp: bool = True,
        enable_cache_control: bool = False,
    ) -> LLMResponse:
        """Generate response from Anthropic LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            max_tokens: Output budget for this call. When None the client
                falls back to `self.default_max_tokens` (set by the router
                when building the client, or a conservative default for
                direct/legacy callers).
            attach_message_bp: Whether to attach BP #4 to the trailing
                stable assistant message. Forwarded to
                ``_convert_messages`` via ``_prepare_request``. The
                router passes ``True`` from ``call()`` and ``False`` from
                ``internal_call()``.
            enable_cache_control: Whether the target node accepts
                Anthropic explicit ``cache_control`` markers. The router
                derives this from ``node.supports_explicit_cache_control``.
                When ``False`` we strip any leaked markers and skip BP
                injection entirely.

        Returns:
            LLMResponse containing the generated content
        """
        request_params = self._prepare_request(
            messages,
            tools,
            attach_message_bp=attach_message_bp,
            enable_cache_control=enable_cache_control,
        )
        # Precedence: explicit caller value → configured default → legacy 16384.
        effective_max_tokens = (
            max_tokens
            if max_tokens is not None
            else (self.default_max_tokens or self._LEGACY_MAX_TOKENS)
        )

        if self.retry_config.enabled:
            retry_decorator = async_retry(
                config=self.retry_config,
                on_retry=self.retry_callback,
                should_retry=self.should_retry,
            )
            api_call = retry_decorator(self._make_api_request)
            response = await api_call(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
                max_tokens=effective_max_tokens,
            )
        else:
            response = await self._make_api_request(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
                max_tokens=effective_max_tokens,
            )

        return self._parse_response(response)


# ---------------------------------------------------------------------
# Module-level helpers — cache_control strip utilities. Placed at module
# scope so they can be unit-tested without instantiating the SDK client.
# ---------------------------------------------------------------------


def _strip_cache_control_from_system(system_message: Any) -> Any:
    """Remove Anthropic ``cache_control`` markers from a system payload.

    The system payload may be a plain string (no markers possible) or a
    list of dict blocks. Returns the input unchanged for the string case.
    """
    if isinstance(system_message, list):
        stripped = []
        for block in system_message:
            if isinstance(block, dict):
                stripped.append({k: v for k, v in block.items() if k != "cache_control"})
            else:
                stripped.append(block)
        return stripped
    return system_message


def _strip_cache_control_from_messages(
    api_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop ``cache_control`` from any nested content block in request dicts."""
    for msg in api_messages:
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                ({k: v for k, v in block.items() if k != "cache_control"}
                 if isinstance(block, dict) else block)
                for block in content
            ]
    return api_messages


def _strip_cache_control_from_tools(
    api_tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Drop the top-level ``cache_control`` marker from each tool dict."""
    if not api_tools:
        return api_tools
    stripped: list[dict[str, Any]] = []
    for tool in api_tools:
        if not isinstance(tool, dict):
            stripped.append(tool)
            continue
        stripped.append({k: v for k, v in tool.items() if k != "cache_control"})
    return stripped
