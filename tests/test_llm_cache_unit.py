"""Offline unit tests for the IMPROVEMENT_04 cache-aware client logic.

These tests do not hit any LLM API. They cover:
- AnthropicClient._parse_response correctly maps both
  DeepSeek-shape and Anthropic-standard cache fields into TokenUsage.
- AnthropicClient._convert_messages attaches BP #4 only when
  enable_cache_control=True AND attach_message_bp=True, with the three
  edge cases from §4.2 changes 4:
    a) string content → upgraded to a single text block + cache_control
    b) thinking-only blocks → BP #4 skipped (no marker landed on thinking)
    c) enable_cache_control=False → all cache_control stripped
- OpenAIClient flattens an Anthropic-shape system list[dict] payload
  into a plain string and strips cache_control.
- OpenAIClient._parse_response maps DeepSeek prompt_cache_*
  and OpenAI prompt_tokens_details.cached_tokens into TokenUsage.
"""

from __future__ import annotations

from types import SimpleNamespace

from mini_agent.llm.anthropic_client import (
    AnthropicClient,
    _strip_cache_control_from_messages,
    _strip_cache_control_from_system,
    _strip_cache_control_from_tools,
)
from mini_agent.llm.openai_client import OpenAIClient
from mini_agent.schema import Message


def _fake_anthropic_response(content_blocks, usage, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(**c) for c in content_blocks],
        usage=usage,
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------
# AnthropicClient._parse_response: cache fields
# ---------------------------------------------------------------------


def test_anthropic_usage_parses_deepseek_prompt_cache_fields():
    """DeepSeek Anthropic-compatible endpoint may surface
    prompt_cache_hit_tokens / prompt_cache_miss_tokens."""
    client = AnthropicClient(api_key="sk", api_base="https://api.test", model="deepseek-v4-pro")
    fake = _fake_anthropic_response(
        content_blocks=[{"type": "text", "text": "hi"}],
        usage=SimpleNamespace(
            prompt_cache_hit_tokens=8000,
            prompt_cache_miss_tokens=1500,
            output_tokens=200,
        ),
    )
    resp = client._parse_response(fake)
    assert resp.usage is not None
    # prompt_tokens == total input = hit + miss
    assert resp.usage.prompt_tokens == 9500
    assert resp.usage.cache_read_tokens == 8000
    assert resp.usage.cache_creation_tokens == 0
    assert resp.usage.cache_miss_tokens == 1500
    assert resp.usage.completion_tokens == 200
    assert resp.usage.total_tokens == 9700


def test_anthropic_usage_parses_standard_cache_fields():
    """Anthropic-official response shape: input_tokens +
    cache_read_input_tokens + cache_creation_input_tokens."""
    client = AnthropicClient(api_key="sk", api_base="https://api.test", model="claude-sonnet-4-6")
    fake = _fake_anthropic_response(
        content_blocks=[{"type": "text", "text": "hi"}],
        usage=SimpleNamespace(
            input_tokens=1000,  # uncached
            cache_read_input_tokens=6000,
            cache_creation_input_tokens=500,
            output_tokens=300,
        ),
    )
    resp = client._parse_response(fake)
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 7500  # total input = 1000+6000+500
    assert resp.usage.cache_read_tokens == 6000
    assert resp.usage.cache_creation_tokens == 500
    assert resp.usage.cache_miss_tokens == 1000
    assert resp.usage.completion_tokens == 300
    assert resp.usage.total_tokens == 7800


def test_anthropic_usage_with_no_cache_fields_keeps_legacy_behavior():
    """When neither DeepSeek nor Anthropic cache fields are present,
    prompt_tokens still reflects total input (just == input_tokens)."""
    client = AnthropicClient(api_key="sk", api_base="https://api.test", model="claude-sonnet-4-6")
    fake = _fake_anthropic_response(
        content_blocks=[{"type": "text", "text": "hi"}],
        usage=SimpleNamespace(input_tokens=1200, output_tokens=80),
    )
    resp = client._parse_response(fake)
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 1200
    assert resp.usage.cache_read_tokens == 0
    assert resp.usage.cache_creation_tokens == 0
    assert resp.usage.cache_miss_tokens == 1200


# ---------------------------------------------------------------------
# AnthropicClient._convert_messages: BP #4 and cache_control stripping
# ---------------------------------------------------------------------


def _client() -> AnthropicClient:
    return AnthropicClient(api_key="sk", api_base="https://api.test", model="claude-sonnet-4-6")


def test_bp4_attached_when_cache_control_enabled_and_attach_message_bp_true():
    """Main-path call: BP #4 attaches to the last stable assistant message."""
    client = _client()
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi there"),
    ]
    _, api_msgs = client._convert_messages(
        messages,
        attach_message_bp=True,
        enable_cache_control=True,
    )
    # The assistant content was a string → upgraded to a single text block.
    asst = api_msgs[-1]
    assert isinstance(asst["content"], list)
    assert asst["content"][0]["type"] == "text"
    assert asst["content"][0]["text"] == "hi there"
    assert asst["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_bp4_not_attached_when_attach_message_bp_false():
    """Summary/internal_call path: no BP #4 on dropped messages."""
    client = _client()
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="hi there"),
    ]
    _, api_msgs = client._convert_messages(
        messages,
        attach_message_bp=False,
        enable_cache_control=True,
    )
    asst = api_msgs[-1]
    # String content survives unchanged (not upgraded to block list).
    assert asst["content"] == "hi there"


def test_bp4_skips_when_only_thinking_blocks():
    """An assistant whose only blocks are ``thinking`` (no text/tool_use)
    must not get cache_control — Anthropic 400s if it lands on thinking."""
    client = _client()
    # Build via _convert_messages of an assistant with thinking only.
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="", thinking="reasoning..."),
    ]
    _, api_msgs = client._convert_messages(
        messages,
        attach_message_bp=True,
        enable_cache_control=True,
    )
    asst = api_msgs[-1]
    # The thinking block survives, and no cache_control was attached.
    assert isinstance(asst["content"], list)
    assert all(block.get("type") == "thinking" for block in asst["content"])
    assert all("cache_control" not in block for block in asst["content"])


def test_bp4_attached_to_last_text_block_skipping_thinking():
    """When the assistant has [thinking, text], BP #4 must land on text."""
    client = _client()
    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="visible answer", thinking="reasoning..."),
    ]
    _, api_msgs = client._convert_messages(
        messages,
        attach_message_bp=True,
        enable_cache_control=True,
    )
    asst = api_msgs[-1]
    text_blocks = [b for b in asst["content"] if b.get("type") == "text"]
    thinking_blocks = [b for b in asst["content"] if b.get("type") == "thinking"]
    assert len(text_blocks) == 1
    assert len(thinking_blocks) == 1
    assert "cache_control" in text_blocks[0]
    assert "cache_control" not in thinking_blocks[0]


def test_bp4_attach_does_not_mutate_original_list_content():
    """BP #4 attach must not mutate the original Message.content list."""
    client = _client()
    msg_list = [{"type": "text", "text": "hello"}]
    messages = [
        Message(role="user", content="q"),
        Message(role="assistant", content=msg_list),
    ]
    _, api = client._convert_messages(
        messages,
        attach_message_bp=True,
        enable_cache_control=True,
    )
    assert msg_list == [{"type": "text", "text": "hello"}], (
        f"original list mutated: {msg_list}"
    )
    assert "cache_control" in api[-1]["content"][-1]


def test_cache_control_stripped_when_enable_cache_control_false():
    """Non-Anthropic-explicit nodes get cache_control scrubbed from
    system + nested content blocks."""
    client = _client()
    # System message arrives as a list[dict] from Agent.render_for_provider().
    sys_blocks = [
        {"type": "text", "text": "base", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "pinned"},
    ]
    messages = [
        Message(role="system", content=sys_blocks),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    system_message, api_msgs = client._convert_messages(
        messages,
        attach_message_bp=True,
        enable_cache_control=False,
    )
    # System list[dict] was stripped of cache_control.
    assert isinstance(system_message, list)
    for block in system_message:
        assert "cache_control" not in block
    # BP #4 was NOT attached because enable_cache_control=False.
    asst = api_msgs[-1]
    assert asst["content"] == "hello"


def test_convert_tools_shallow_copies_when_caching_last_tool():
    """Adding cache_control must not mutate the caller's tool dict."""
    client = _client()
    tool_dict = {
        "name": "last_tool",
        "description": "",
        "input_schema": {"type": "object", "properties": {}},
    }
    tools = [
        {"name": "first", "description": "", "input_schema": {"type": "object", "properties": {}}},
        tool_dict,
    ]
    out = client._convert_tools(tools, cache_last_tool=True)
    # Output last tool has cache_control...
    assert out[-1]["cache_control"] == {"type": "ephemeral"}
    # ...but the caller's original dict is untouched.
    assert "cache_control" not in tool_dict


def test_strip_helpers_are_idempotent():
    """Stripping a system block / message / tool with no cache_control
    must be a no-op (defensive: cross-family fallback may call it on
    payloads that never had a marker)."""
    plain_system = [{"type": "text", "text": "base"}]
    assert _strip_cache_control_from_system(plain_system) == plain_system

    plain_messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert _strip_cache_control_from_messages(plain_messages) == plain_messages

    plain_tools = [{"name": "t", "description": "", "input_schema": {}}]
    assert _strip_cache_control_from_tools(plain_tools) == plain_tools


# ---------------------------------------------------------------------
# OpenAIClient: system list flatten + DeepSeek cache parse
# ---------------------------------------------------------------------


def test_openai_convert_messages_flattens_anthropic_system_blocks():
    """OpenAI's API rejects list-shape system content, so the client
    must flatten it to a string and drop cache_control."""
    client = OpenAIClient(api_key="sk", api_base="https://api.test", model="deepseek-v4-pro")
    messages = [
        Message(role="system", content=[
            {"type": "text", "text": "base", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "pinned"},
        ]),
        Message(role="user", content="hi"),
    ]
    _, api_msgs = client._convert_messages(messages)
    system_msg = next(m for m in api_msgs if m["role"] == "system")
    assert isinstance(system_msg["content"], str)
    assert "base" in system_msg["content"]
    assert "pinned" in system_msg["content"]
    # No leaked cache_control.
    assert "cache_control" not in system_msg["content"]


def test_openai_usage_parses_deepseek_prompt_cache_fields():
    """DeepSeek's OpenAI-compatible endpoint surfaces the same hit/miss fields."""
    client = OpenAIClient(api_key="sk", api_base="https://api.test", model="deepseek-v4-pro")
    fake = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="hi",
                tool_calls=None,
                reasoning_details=None,
            ),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=10000,
            completion_tokens=200,
            total_tokens=10200,
            prompt_cache_hit_tokens=8000,
            prompt_cache_miss_tokens=2000,
        ),
    )
    resp = client._parse_response(fake)
    assert resp.usage is not None
    assert resp.usage.cache_read_tokens == 8000
    assert resp.usage.cache_miss_tokens == 2000


def test_openai_usage_parses_prompt_tokens_details_cached_tokens():
    """OpenAI native usage surfaces cached_tokens under prompt_tokens_details."""
    client = OpenAIClient(api_key="sk", api_base="https://api.test", model="gpt-4o")
    fake = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="hi", tool_calls=None, reasoning_details=None),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=10000,
            completion_tokens=100,
            total_tokens=10100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=7500),
        ),
    )
    resp = client._parse_response(fake)
    assert resp.usage is not None
    assert resp.usage.cache_read_tokens == 7500
    # Miss = prompt_total - cached_tokens
    assert resp.usage.cache_miss_tokens == 2500


def test_openai_usage_with_no_cache_detail_falls_back_to_zero():
    """No cache telemetry → zeros; legacy callers see no behavior change."""
    client = OpenAIClient(api_key="sk", api_base="https://api.test", model="gpt-3.5")
    fake = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="hi", tool_calls=None, reasoning_details=None),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        ),
    )
    resp = client._parse_response(fake)
    assert resp.usage is not None
    assert resp.usage.cache_read_tokens == 0
    assert resp.usage.cache_miss_tokens == 0
    assert resp.usage.prompt_tokens == 100
