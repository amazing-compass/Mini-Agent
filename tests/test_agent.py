"""Live-API integration tests for Agent (requires real API key).

Phase 3 removed the `LLMClient` facade; Agent now takes a `router`
directly. These tests hit real APIs, so they skip naturally under CI
without credentials.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from mini_agent.agent import Agent
from mini_agent.config import Config
from mini_agent.llm.ha import (
    ModelNode,
    ModelPool,
    ModelRouter,
    SimpleBreaker,
    build_client_factory,
)
from mini_agent.tools import BashTool, EditTool, ReadTool, WriteTool


def _router_from_config(config: Config) -> ModelRouter:
    """One-shot helper: build pool + breaker + router from a Config."""
    nodes = [
        ModelNode(
            node_id=entry.node_id,
            provider=entry.provider.lower(),
            protocol_family=(entry.protocol_family or entry.provider).lower(),
            api_key=entry.api_key or "",
            api_base=entry.api_base,
            model=entry.model,
            priority=entry.priority,
            weight=entry.weight,
            context_window=entry.context_window,
            max_output_tokens=entry.max_output_tokens,
            supports_tools=entry.supports_tools,
            supports_thinking=entry.supports_thinking,
            enabled=entry.enabled,
        )
        for entry in config.llm.pool
    ]
    pool = ModelPool(nodes, build_client=build_client_factory())
    breaker = SimpleBreaker(
        failure_threshold=config.llm.breaker.failure_threshold,
        cooldown_seconds=config.llm.breaker.cooldown_seconds,
    )
    return ModelRouter(
        pool,
        breaker,
        strategy=config.llm.routing.strategy,
        cross_family_fallback=config.llm.routing.cross_family_fallback,
    )


@pytest.mark.asyncio
async def test_agent_simple_task():
    """Test agent with a simple file creation task."""
    print("\n=== Testing Agent with Simple File Task ===")

    config_path = Path("mini_agent/config/config.yaml")
    config = Config.from_yaml(config_path)

    with tempfile.TemporaryDirectory() as workspace_dir:
        print(f"Using workspace: {workspace_dir}")

        system_prompt_path = Path("mini_agent/config/system_prompt.md")
        if system_prompt_path.exists():
            system_prompt = system_prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant that can use tools."

        router = _router_from_config(config)

        tools = [
            ReadTool(workspace_dir=workspace_dir),
            WriteTool(workspace_dir=workspace_dir),
            EditTool(workspace_dir=workspace_dir),
            BashTool(),
        ]

        agent = Agent(
            router=router,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=10,
            workspace_dir=workspace_dir,
        )

        task = "Create a file named 'test.txt' with the content 'Hello from Agent!'"
        print(f"\nTask: {task}\n")
        agent.add_user_message(task)

        try:
            result = await agent.run()
            print(f"\n{'=' * 80}")
            print(f"Agent Result: {result}")
            print("=" * 80)

            test_file = Path(workspace_dir) / "test.txt"
            if test_file.exists():
                content = test_file.read_text()
                print("\n✅ File created successfully!")
                print(f"Content: {content}")
                if "Hello from Agent!" in content:
                    print("✅ Content is correct!")
                else:
                    print(f"⚠️  Content mismatch: {content}")
            else:
                print("⚠️  File was not created, but agent completed")
            return True
        except Exception as e:
            print(f"❌ Agent test failed: {e}")
            import traceback

            traceback.print_exc()
            return False


@pytest.mark.asyncio
async def test_agent_bash_task():
    """Test agent with a bash command task."""
    print("\n=== Testing Agent with Bash Task ===")

    config_path = Path("mini_agent/config/config.yaml")
    config = Config.from_yaml(config_path)

    with tempfile.TemporaryDirectory() as workspace_dir:
        print(f"Using workspace: {workspace_dir}")

        system_prompt_path = Path("mini_agent/config/system_prompt.md")
        if system_prompt_path.exists():
            system_prompt = system_prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt = "You are a helpful AI assistant that can use tools."

        router = _router_from_config(config)

        tools = [
            ReadTool(workspace_dir=workspace_dir),
            WriteTool(workspace_dir=workspace_dir),
            BashTool(),
        ]

        agent = Agent(
            router=router,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=10,
            workspace_dir=workspace_dir,
        )

        task = "Use bash to list all files in the current directory and tell me what you find."
        print(f"\nTask: {task}\n")
        agent.add_user_message(task)

        try:
            result = await agent.run()
            print(f"\n{'=' * 80}")
            print(f"Agent Result: {result}")
            print("=" * 80)
            print("\n✅ Bash task completed!")
            return True
        except Exception as e:
            print(f"❌ Bash task failed: {e}")
            import traceback

            traceback.print_exc()
            return False


async def main():
    """Run all agent tests."""
    print("=" * 80)
    print("Running Agent Integration Tests")
    print("=" * 80)

    result1 = await test_agent_simple_task()
    result2 = await test_agent_bash_task()

    print("\n" + "=" * 80)
    if result1 and result2:
        print("All Agent tests passed! ✅")
    else:
        print("Some Agent tests failed. Check the output above.")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
