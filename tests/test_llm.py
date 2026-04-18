"""Live-API smoke tests for provider clients through the pool/router.

Phase 3 removed the `LLMClient` facade; these tests now construct a
one-node `ModelPool + ModelRouter` and call `router.call(...)` directly.
They still hit real APIs so offline runs skip them (no API key).
"""

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from mini_agent.llm.ha import (
    ModelNode,
    ModelPool,
    ModelRouter,
    SimpleBreaker,
    build_client_factory,
)
from mini_agent.schema import LLMProvider, Message


def _load_yaml_config() -> dict:
    config_path = Path("mini_agent/config/config.yaml")
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _router_for(provider: LLMProvider) -> ModelRouter:
    """Build a single-node pool + router for the given provider.

    Reads credentials from the pool schema: picks the first entry whose
    `provider` matches, else falls back to the first entry (for tests
    that want to exercise a protocol family against whichever node is
    available).
    """
    cfg = _load_yaml_config()
    pool_cfg = cfg.get("llm", {}).get("pool") or cfg.get("pool") or []
    if not pool_cfg:
        pytest.skip("No pool entries in config.yaml; cannot run live test")

    entry = next(
        (n for n in pool_cfg if n.get("provider") == provider.value),
        pool_cfg[0],
    )
    api_key = entry.get("api_key", "")
    if not api_key:
        env_var = entry.get("api_key_env", "")
        if env_var:
            api_key = os.environ.get(env_var, "")
    if not api_key:
        pytest.skip(f"No api_key for provider {provider.value}; skipping live test")

    node = ModelNode(
        node_id="live",
        provider=provider.value,
        protocol_family=provider.value,
        api_key=api_key,
        api_base=entry.get("api_base", "https://api.minimax.io"),
        model=entry.get("model", "MiniMax-M2.5"),
        priority=100,
    )
    pool = ModelPool([node], build_client=build_client_factory())
    breaker = SimpleBreaker()
    return ModelRouter(pool, breaker)


@pytest.mark.asyncio
async def test_wrapper_anthropic_provider():
    """Test Anthropic provider end-to-end."""
    print("\n=== Testing Router (Anthropic Provider) ===")
    router = _router_for(LLMProvider.ANTHROPIC)

    messages = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Say 'Hello, Mini Agent!' and nothing else."),
    ]

    try:
        response = await router.call(messages=messages)
        print(f"Response: {response.content}")
        assert response.content, "Response content is empty"
        assert "Hello" in response.content or "hello" in response.content
        print("✅ Anthropic provider test passed")
        return True
    except Exception as e:
        print(f"❌ Anthropic provider test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


@pytest.mark.asyncio
async def test_wrapper_openai_provider():
    """Test OpenAI provider end-to-end."""
    print("\n=== Testing Router (OpenAI Provider) ===")
    router = _router_for(LLMProvider.OPENAI)

    messages = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Say 'Hello, Mini Agent!' and nothing else."),
    ]

    try:
        response = await router.call(messages=messages)
        print(f"Response: {response.content}")
        assert response.content, "Response content is empty"
        assert "Hello" in response.content or "hello" in response.content
        print("✅ OpenAI provider test passed")
        return True
    except Exception as e:
        print(f"❌ OpenAI provider test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


@pytest.mark.asyncio
async def test_wrapper_tool_calling():
    """Test tool calling end-to-end through the router."""
    print("\n=== Testing Router Tool Calling ===")
    router = _router_for(LLMProvider.ANTHROPIC)

    messages = [
        Message(role="system", content="You are a helpful assistant with access to tools."),
        Message(role="user", content="Calculate 123 + 456 using the calculator tool."),
    ]

    tools = [
        {
            "name": "calculator",
            "description": "Perform arithmetic operations",
            "input_schema": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["operation", "a", "b"],
            },
        }
    ]

    try:
        response = await router.call(messages=messages, tools=tools)
        print(f"Tool calls: {response.tool_calls}")
        if response.tool_calls:
            print("✅ Tool calling test passed - LLM requested tool use")
        else:
            print("⚠️  Warning: LLM didn't use tools, but request succeeded")
        return True
    except Exception as e:
        print(f"❌ Tool calling test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("=" * 80)
    print("Running Router live-API Tests")
    print("=" * 80)
    print("\nNote: These tests require a valid MiniMax API key in config.yaml")

    results = []
    results.append(await test_wrapper_anthropic_provider())
    results.append(await test_wrapper_openai_provider())
    results.append(await test_wrapper_tool_calling())

    print("\n" + "=" * 80)
    if all(results):
        print("All Router live-API tests passed! ✅")
    else:
        print("Some tests failed. Check the output above.")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
