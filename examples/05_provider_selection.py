"""Example 5: Talking to Providers Directly

Phase 3 removed the `LLMClient` facade. This demo shows the two paths
that replaced it:

1. `build_direct_client(config, LLMProvider.X)` — pull credentials from
   the first matching pool entry and instantiate a provider client
   (Anthropic or OpenAI) for one-shot requests.
2. `build_router(config)` — the router wraps the whole pool and hands
   Agent a unified interface; a direct client is what you want when the
   router's breaker/failover semantics would be overkill.

MiniMax serves both protocols from the same credentials, so the pool
usually only carries `provider: anthropic` nodes and the OpenAI demo
still works by pointing at the `/v1` suffix. If your pool contains an
explicit `provider: openai` node, that node's key is used instead.
"""

import asyncio

from _common import build_direct_client, load_config

from mini_agent.schema import LLMProvider, Message


async def demo_anthropic_provider():
    """Demo: raw AnthropicClient."""
    print("\n" + "=" * 60)
    print("DEMO: AnthropicClient (Messages API)")
    print("=" * 60)

    config = load_config()
    if config is None:
        return

    client = build_direct_client(config, LLMProvider.ANTHROPIC)

    print(f"Provider: anthropic")
    print(f"API Base: {client.api_base}")
    print(f"Model:    {client.model}")

    messages = [Message(role="user", content="Say 'Hello from Anthropic!'")]
    print(f"\n👤 User: {messages[0].content}")

    try:
        response = await client.generate(messages)
        if response.thinking:
            print(f"💭 Thinking: {response.thinking}")
        print(f"💬 Model: {response.content}")
        print("✅ Anthropic provider demo completed")
    except Exception as e:
        print(f"❌ Error: {e}")


async def demo_openai_provider():
    """Demo: raw OpenAIClient."""
    print("\n" + "=" * 60)
    print("DEMO: OpenAIClient (Chat Completions API)")
    print("=" * 60)

    config = load_config()
    if config is None:
        return

    client = build_direct_client(config, LLMProvider.OPENAI)

    print(f"Provider: openai")
    print(f"API Base: {client.api_base}")
    print(f"Model:    {client.model}")

    messages = [Message(role="user", content="Say 'Hello from OpenAI!'")]
    print(f"\n👤 User: {messages[0].content}")

    try:
        response = await client.generate(messages)
        if response.thinking:
            print(f"💭 Thinking: {response.thinking}")
        print(f"💬 Model: {response.content}")
        print("✅ OpenAI provider demo completed")
    except Exception as e:
        print(f"❌ Error: {e}")


async def demo_provider_comparison():
    """Compare responses from both providers on the same prompt."""
    print("\n" + "=" * 60)
    print("DEMO: Provider Comparison")
    print("=" * 60)

    config = load_config()
    if config is None:
        return

    anthropic_client = build_direct_client(config, LLMProvider.ANTHROPIC)
    openai_client = build_direct_client(config, LLMProvider.OPENAI)

    messages = [Message(role="user", content="What is 2+2?")]
    print(f"\n👤 Question: {messages[0].content}\n")

    try:
        anthropic_response = await anthropic_client.generate(messages)
        print(f"🔵 Anthropic: {anthropic_response.content}")

        openai_response = await openai_client.generate(messages)
        print(f"🟢 OpenAI: {openai_response.content}")

        print("\n✅ Provider comparison completed")
    except Exception as e:
        print(f"❌ Error: {e}")


async def main():
    """Run all demos."""
    print("\n🚀 Provider Selection Demo (Phase 3)")
    print("This demo shows how to instantiate provider-specific clients")
    print("from a Phase 3 pool-based config.")
    print("Make sure you have configured an API key in config.yaml.")

    try:
        await demo_anthropic_provider()
        await demo_openai_provider()
        await demo_provider_comparison()

        print("\n✅ All demos completed successfully!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
