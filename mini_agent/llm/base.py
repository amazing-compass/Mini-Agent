"""Base class for LLM clients."""

from abc import ABC, abstractmethod
from typing import Any

from ..retry import RetryConfig
from ..schema import LLMResponse, Message


class LLMClientBase(ABC):
    """Abstract base class for LLM clients.

    This class defines the interface that all LLM clients must implement,
    regardless of the underlying API protocol (Anthropic, OpenAI, etc.).
    """

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        retry_config: RetryConfig | None = None,
        *,
        default_max_tokens: int | None = None,
    ):
        """Initialize the LLM client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
            default_max_tokens: Output budget used by `generate()` when
                the caller doesn't pass `max_tokens`. When None, each
                subclass falls back to its own provider-appropriate
                behavior (Anthropic: a fixed high cap — the SDK requires
                the field; OpenAI: omit the field entirely so provider
                defaults apply). This preserves legacy behavior for
                direct/ACP callers who never set the knob.
        """
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.retry_config = retry_config or RetryConfig()
        # Intentionally None-preserving: subclasses check for None and
        # choose their own legacy behavior.
        self.default_max_tokens = default_max_tokens

        # Callback for tracking retry count
        self.retry_callback = None
        # Phase 3 removed the `should_retry` double-insurance: the default
        # `retryable_exceptions=(TransientError,)` is narrow enough on its
        # own, and the provider-side `normalize_sdk_error` guarantees that
        # anything reaching the retry decorator is already classified.
        # The attribute is kept on the instance so external callers that
        # want an extra classifier gate can still inject one.
        self.should_retry = None

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            max_tokens: Output budget for this call. Router always passes
                an explicit value; direct callers may omit and fall back
                to `self.default_max_tokens`.

        Returns:
            LLMResponse containing the generated content, thinking, and tool calls
        """
        pass

    @abstractmethod
    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request payload for the API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing the request payload
        """
        pass

    @abstractmethod
    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal message format to API-specific format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        pass
