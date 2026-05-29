# ✅ 
"""Base class for LLM clients."""

# ABC -- 抽象基类  abstract base class
# ABC -- 作用就是强制规定子类必须实现哪些方法
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
        # 触发重试时调用的通知函数
        self.retry_callback = None
        # Phase 3 removed the `should_retry` double-insurance: the default
        # `retryable_exceptions=(TransientError,)` is narrow enough on its
        # own, and the provider-side `normalize_sdk_error` guarantees that
        # anything reaching the retry decorator is already classified.
        # The attribute is kept on the instance so external callers that
        # want an extra classifier gate can still inject one.
        # 重试前的否决门   在返回false时，不进行重试 比如认证失败（401）、请求格式错误这类 --- 重试10次也没用 直接报错
        self.should_retry = None

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        max_tokens: int | None = None,
        attach_message_bp: bool = True,
        enable_cache_control: bool = False,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            max_tokens: Output budget for this call. Router always passes
                an explicit value; direct callers may omit and fall back
                to `self.default_max_tokens`.
            attach_message_bp: Whether to attach an Anthropic cache_control
                breakpoint to the last stable assistant message. Main-path
                calls (``router.call``) keep the default ``True``; the
                summary bypass (``router.internal_call``) passes ``False``
                so dropped messages aren't paid for as cache_write entries
                that no future request will read.
            enable_cache_control: Whether the target node supports Anthropic
                explicit ``cache_control`` markers. When ``False`` the
                client strips/skips all marker injection regardless of
                ``attach_message_bp`` (DeepSeek, OpenAI, MiniMax, etc.).

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
