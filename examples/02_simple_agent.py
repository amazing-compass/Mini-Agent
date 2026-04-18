"""Example 2: Simple Agent Usage

Demonstrates how to create and run a basic agent to perform simple
file operations.

Phase 3: Agent owns a `ModelRouter` directly (no more `LLMClient`
facade). The router-wiring boilerplate is hidden in `examples/_common.py`
so this file can focus on the agent loop itself.

Based on: tests/test_agent.py
"""

import asyncio
import tempfile
from pathlib import Path

from _common import build_router, load_config, load_system_prompt

from mini_agent.agent import Agent
from mini_agent.tools import BashTool, EditTool, ReadTool, WriteTool


async def demo_file_creation():
    """Demo: Agent creates a file based on user request."""
    print("\n" + "=" * 60)
    print("Demo: Agent-Driven File Creation")
    print("=" * 60)

    config = load_config()
    if config is None:
        return
    router = build_router(config)
    system_prompt = load_system_prompt()

    with tempfile.TemporaryDirectory() as workspace_dir:
        print(f"📁 Workspace: {workspace_dir}\n")

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

        task = """
        Create a Python file named 'hello.py' that:
        1. Defines a function called greet(name)
        2. The function prints "Hello, {name}!"
        3. Calls the function with name="Mini Agent"
        """

        print("📝 Task:")
        print(task)
        print("\n" + "=" * 60)
        print("🤖 Agent is working...\n")

        agent.add_user_message(task)

        try:
            result = await agent.run()

            print("\n" + "=" * 60)
            print("✅ Agent completed the task!")
            print("=" * 60)
            print(f"\nAgent's response:\n{result}\n")

            hello_file = Path(workspace_dir) / "hello.py"
            if hello_file.exists():
                print("=" * 60)
                print("📄 Created file content:")
                print("=" * 60)
                print(hello_file.read_text())
                print("=" * 60)
            else:
                print("⚠️  File was not created (but agent may have completed differently)")

        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback

            traceback.print_exc()


async def demo_bash_task():
    """Demo: Agent executes bash commands."""
    print("\n" + "=" * 60)
    print("Demo: Agent-Driven Bash Commands")
    print("=" * 60)

    config = load_config()
    if config is None:
        return
    router = build_router(config)
    system_prompt = load_system_prompt()

    with tempfile.TemporaryDirectory() as workspace_dir:
        print(f"📁 Workspace: {workspace_dir}\n")

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

        task = """
        Use bash commands to:
        1. Show the current date and time
        2. List all Python files in the current directory
        3. Count how many Python files exist
        """

        print("📝 Task:")
        print(task)
        print("\n" + "=" * 60)
        print("🤖 Agent is working...\n")

        agent.add_user_message(task)

        try:
            result = await agent.run()

            print("\n" + "=" * 60)
            print("✅ Agent completed!")
            print("=" * 60)
            print(f"\nAgent's response:\n{result}\n")

        except Exception as e:
            print(f"❌ Error: {e}")


async def main():
    """Run all demos."""
    print("=" * 60)
    print("Simple Agent Usage Examples")
    print("=" * 60)
    print("\nThese examples show how to create an agent and give it tasks.")
    print("The agent uses LLM (via ModelRouter) to decide which tools to call.\n")

    await demo_file_creation()
    print("\n" * 2)
    await demo_bash_task()

    print("\n" + "=" * 60)
    print("All demos completed! ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
