"""Regression tests for the Phase 2 max_tokens default-resolution contract.

Codex flagged that setting a universal `DEFAULT_MAX_TOKENS = 8192` on
`LLMClientBase` silently downgraded direct/ACP callers — Anthropic went
16384 → 8192, OpenAI went "let provider decide" → 8192. These tests pin
the corrected behavior:
  * Base class default is None (no universal cap).
  * AnthropicClient falls back to 16384 when no default is configured
    (the SDK requires the field).
  * OpenAIClient OMITS max_tokens entirely when no default is configured
    (preserves provider-default behavior).
  * Router/LLMClient pool path still injects `default_max_tokens=node.max_output_tokens`.
"""

from __future__ import annotations

from mini_agent.llm.anthropic_client import AnthropicClient
from mini_agent.llm.base import LLMClientBase
from mini_agent.llm.openai_client import OpenAIClient


def test_base_client_default_max_tokens_is_none() -> None:
    """No universal cap — subclasses decide their own legacy fallback."""

    class _Dummy(LLMClientBase):
        async def generate(self, messages, tools=None, *, max_tokens=None):
            return None

        def _prepare_request(self, messages, tools=None):
            return {}

        def _convert_messages(self, messages):
            return None, []

    d = _Dummy("sk", "https://example.test", "m")
    assert d.default_max_tokens is None


def test_anthropic_without_default_uses_legacy_16384() -> None:
    """Direct AnthropicClient(api_key=...) callers should keep Phase 1 behavior:
    the `_make_api_request` path must resolve to 16384 when neither the
    call nor the constructor specified `max_tokens`."""
    c = AnthropicClient(api_key="sk", api_base="https://example.test", model="m")
    assert c.default_max_tokens is None
    # Exposed as a class constant for assertion clarity.
    assert c._LEGACY_MAX_TOKENS == 16384


def test_anthropic_with_configured_default_honors_it() -> None:
    """Pool path: the router injects `default_max_tokens=node.max_output_tokens`."""
    c = AnthropicClient(
        api_key="sk", api_base="https://example.test", model="m",
        default_max_tokens=4096,
    )
    assert c.default_max_tokens == 4096


def test_openai_without_default_stays_none_so_sdk_param_is_omitted() -> None:
    """Direct OpenAIClient(api_key=...) callers should keep Phase 1 behavior:
    `_make_api_request` must not populate `max_tokens` in the request dict
    when no default is configured."""
    c = OpenAIClient(api_key="sk", api_base="https://example.test", model="m")
    assert c.default_max_tokens is None


async def test_openai_make_api_request_omits_max_tokens_when_none() -> None:
    """Direct introspection: when `max_tokens=None`, the SDK param dict
    must NOT include `max_tokens`. Capture the outgoing params by stubbing
    the SDK client."""

    class _StubChatCompletions:
        def __init__(self) -> None:
            self.last_params: dict | None = None

        async def create(self, **params):
            self.last_params = params

            class _Choice:
                class message:
                    content = "ok"
                    tool_calls = None
                finish_reason = "stop"

            class _Resp:
                choices = [_Choice]
                usage = None

            return _Resp()

    class _StubClient:
        def __init__(self) -> None:
            self.chat = type("Chat", (), {})()
            self.chat.completions = _StubChatCompletions()

    c = OpenAIClient(api_key="sk", api_base="https://example.test", model="m")
    c.client = _StubClient()  # type: ignore[assignment]
    # Disable retry so we get a single deterministic call.
    c.retry_config.enabled = False

    await c._make_api_request(api_messages=[{"role": "user", "content": "hi"}], max_tokens=None)
    params = c.client.chat.completions.last_params
    assert params is not None
    assert "max_tokens" not in params, (
        f"OpenAI request must NOT include max_tokens when caller didn't set one; "
        f"got {params}"
    )


async def test_openai_make_api_request_includes_max_tokens_when_set() -> None:
    """Sanity check — a configured max_tokens must show up."""

    class _StubChatCompletions:
        def __init__(self) -> None:
            self.last_params: dict | None = None

        async def create(self, **params):
            self.last_params = params

            class _Choice:
                class message:
                    content = "ok"
                    tool_calls = None
                finish_reason = "stop"

            class _Resp:
                choices = [_Choice]
                usage = None

            return _Resp()

    class _StubClient:
        def __init__(self) -> None:
            self.chat = type("Chat", (), {})()
            self.chat.completions = _StubChatCompletions()

    c = OpenAIClient(api_key="sk", api_base="https://example.test", model="m")
    c.client = _StubClient()  # type: ignore[assignment]
    c.retry_config.enabled = False

    await c._make_api_request(
        api_messages=[{"role": "user", "content": "hi"}],
        max_tokens=2048,
    )
    params = c.client.chat.completions.last_params
    assert params is not None
    assert params.get("max_tokens") == 2048
