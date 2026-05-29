# ✅
"""OpenAI LLM client implementation."""

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from ..retry import RetryConfig, async_retry
from ..schema import FunctionCall, LLMResponse, Message, TokenUsage, ToolCall
from .base import LLMClientBase
from .ha.errors import LLMError, normalize_sdk_error

logger = logging.getLogger(__name__)


class OpenAIClient(LLMClientBase):
    """LLM client using OpenAI's protocol.

    This client uses the official OpenAI SDK and supports:
    - Reasoning content (via reasoning_split=True)
    - Tool calling
    - Retry logic
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.minimaxi.com/v1",
        model: str = "MiniMax-M2.5",
        retry_config: RetryConfig | None = None,
        *,
        default_max_tokens: int | None = None,
    ):
        """Initialize OpenAI client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API (default: MiniMax OpenAI endpoint)
            model: Model name to use (default: MiniMax-M2.5)
            retry_config: Optional retry configuration
            default_max_tokens: Fallback for `generate(max_tokens=...)`.
        """
        super().__init__(api_key, api_base, model, retry_config, default_max_tokens=default_max_tokens)

        # Initialize OpenAI client
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
        )

    async def _make_api_request(
        self,
        api_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> Any:
        """Execute API request (core method that can be retried).

        Args:
            api_messages: List of messages in OpenAI format
            tools: Optional list of tools
            max_tokens: Output budget for this call (computed by the
                router). When None, the parameter is **omitted entirely**
                so the provider applies its own default — preserves the
                pre-Phase-2 behavior for direct/ACP callers who never
                configure the knob.

        Returns:
            OpenAI ChatCompletion response (full response including usage)

        Raises:
            LLMError subclass: normalized from the SDK exception so the router
            and retry layer never see vendor-specific error types.
        """
        params = {
            "model": self.model,
            "messages": api_messages,
            # Enable reasoning_split to separate thinking content
            "extra_body": {"reasoning_split": True},
        }
        # Only send max_tokens when explicitly configured — otherwise
        # let the provider use its own (usually larger) default.
        if max_tokens is not None:
            params["max_tokens"] = max_tokens

        if tools:
            params["tools"] = self._convert_tools(tools)

        try:
            return await self.client.chat.completions.create(**params)
        except LLMError:
            raise
        except Exception as exc:
            raise normalize_sdk_error(exc) from exc

# ✅
    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        """Convert tools to OpenAI format.

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in OpenAI dict format
        """
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                # If already a dict, check if it's in OpenAI format
                if "type" in tool and tool["type"] == "function":
                    result.append(tool)
                else:
                    # Assume it's in Anthropic format, convert to OpenAI
                    result.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool["description"],
                                "parameters": tool["input_schema"],
                            },
                        }
                    )
            elif hasattr(tool, "to_openai_schema"):
                # Tool object with to_openai_schema method
                result.append(tool.to_openai_schema())
            else:
                raise TypeError(f"Unsupported tool type: {type(tool)}")
        return result

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to OpenAI format.

        OpenAI's chat completions API requires ``system.content`` to be
        a single string. When the caller hands us an Anthropic-style
        ``Message(role="system", content=[{type:"text", text:..., cache_control:...}, ...])``
        we flatten the text blocks into one ``"\n\n"``-joined string and
        drop ``cache_control`` (OpenAI auto-caches its own prefixes; the
        marker would be a 400).

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
            Note: OpenAI includes system message in the messages array
        """
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                # OpenAI includes system message in messages array. If the
                # content is a list[dict] (Anthropic-style blocks),
                # flatten to a single string and discard cache_control.
                if isinstance(msg.content, list):
                    text = "\n\n".join(
                        block.get("text", "")
                        for block in msg.content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                    api_messages.append({"role": "system", "content": text})
                else:
                    api_messages.append({"role": "system", "content": msg.content})
                continue

            # For user messages
            if msg.role == "user":
                api_messages.append({"role": "user", "content": msg.content})

            # For assistant messages
            elif msg.role == "assistant":
                assistant_msg = {"role": "assistant"}

                # Add content if present
                if msg.content:
                    assistant_msg["content"] = msg.content

                # Add tool calls if present
                if msg.tool_calls:
                    tool_calls_list = []
                    for tool_call in msg.tool_calls:
                        tool_calls_list.append(
                            {
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": json.dumps(tool_call.function.arguments),
                                },
                            }
                        )
                    assistant_msg["tool_calls"] = tool_calls_list

                # IMPORTANT: Add reasoning_details if thinking is present
                # This is CRITICAL for Interleaved Thinking to work properly!
                # The complete response_message (including reasoning_details) must be
                # preserved in Message History and passed back to the model in the next turn.
                # This ensures the model's chain of thought is not interrupted.
                if msg.thinking:
                    assistant_msg["reasoning_details"] = [{"text": msg.thinking}]

                api_messages.append(assistant_msg)

            # For tool result messages
            elif msg.role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )

        return None, api_messages

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request for OpenAI API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing request parameters
        """
        _, api_messages = self._convert_messages(messages)

        return {
            "api_messages": api_messages,
            "tools": tools,
        }

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse OpenAI response into LLMResponse.

        Args:
            response: OpenAI ChatCompletion response (full response object)

        Returns:
            LLMResponse object
        """
        choice = response.choices[0]
        message = choice.message

        # Extract text content
        text_content = message.content or ""

        # Extract thinking content from reasoning_details
        thinking_content = ""
        if hasattr(message, "reasoning_details") and message.reasoning_details:
            # reasoning_details is a list of reasoning blocks
            for detail in message.reasoning_details:
                if hasattr(detail, "text"):
                    thinking_content += detail.text

        # Extract tool calls
        tool_calls = []
        if message.tool_calls:
            for tool_call in message.tool_calls:
                # Parse arguments from JSON string
                arguments = json.loads(tool_call.function.arguments)

                tool_calls.append(
                    ToolCall(
                        id=tool_call.id,
                        type="function",
                        function=FunctionCall(
                            name=tool_call.function.name,
                            arguments=arguments,
                        ),
                    )
                )

        # Extract token usage from response. Two cache-aware shapes:
        # - DeepSeek's OpenAI-compatible endpoint surfaces
        #   ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``.
        # - OpenAI native uses ``prompt_tokens_details.cached_tokens``.
        # Plain OpenAI (no cache detail) ends up with zeros, which is
        # the same as today.
        usage = None
        if hasattr(response, "usage") and response.usage:
            usage_obj = response.usage
            prompt_total = getattr(usage_obj, "prompt_tokens", 0) or 0
            completion_total = getattr(usage_obj, "completion_tokens", 0) or 0
            total = getattr(usage_obj, "total_tokens", 0) or 0

            cache_hit = getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0
            cache_miss = getattr(usage_obj, "prompt_cache_miss_tokens", 0) or 0

            if not cache_hit and not cache_miss:
                details = getattr(usage_obj, "prompt_tokens_details", None)
                if details is not None:
                    cache_hit = getattr(details, "cached_tokens", 0) or 0
                    if cache_hit:
                        cache_miss = max(prompt_total - cache_hit, 0)

            usage = TokenUsage(
                prompt_tokens=prompt_total or (cache_hit + cache_miss),
                completion_tokens=completion_total,
                total_tokens=total,
                cache_read_tokens=cache_hit,
                cache_creation_tokens=0,
                cache_miss_tokens=cache_miss,
            )

        # Fix: read real finish_reason from choices[0] instead of hardcoding "stop".
        # Possible values: "stop", "length", "tool_calls", "content_filter", "function_call".
        finish_reason = getattr(choice, "finish_reason", None) or "stop"

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=finish_reason,
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
        """Generate response from OpenAI LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            max_tokens: Output budget for this call (computed by the router).
            attach_message_bp: Accepted for interface parity; the OpenAI
                protocol has no equivalent of Anthropic's BP #4, so the
                flag is intentionally ignored.
            enable_cache_control: Accepted for interface parity. OpenAI
                auto-caches its own prefixes and does not honor Anthropic
                ``cache_control`` markers. ``_convert_messages`` already
                strips markers when flattening system lists, so this is a
                no-op for OpenAI.

        Returns:
            LLMResponse containing the generated content
        """
        del attach_message_bp, enable_cache_control  # interface parity only
        # Prepare request
        request_params = self._prepare_request(messages, tools)
        # Precedence: explicit caller → configured default → None
        # (which makes `_make_api_request` omit the field so the provider
        # applies its own larger default).
        effective_max_tokens = max_tokens if max_tokens is not None else self.default_max_tokens

        # Make API request with retry logic
        if self.retry_config.enabled:
            # Apply retry logic
            retry_decorator = async_retry(
                config=self.retry_config,
                on_retry=self.retry_callback,
                should_retry=self.should_retry,
            )
            api_call = retry_decorator(self._make_api_request)
            response = await api_call(
                request_params["api_messages"],
                request_params["tools"],
                max_tokens=effective_max_tokens,
            )
        else:
            # Don't use retry
            response = await self._make_api_request(
                request_params["api_messages"],
                request_params["tools"],
                max_tokens=effective_max_tokens,
            )

        # Parse and return response
        return self._parse_response(response)
