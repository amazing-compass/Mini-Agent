"""Example 3: Session Note Tool Usage

Demonstrates the Session Note Tool — the feature that lets agents
maintain memory across sessions.

Phase 3: Agent uses a `ModelRouter` instead of the removed `LLMClient`
facade; router construction is hidden in `examples/_common.py`.

Based on: tests/test_note_tool.py, tests/test_integration.py
"""

import asyncio
import json
import tempfile
from pathlib import Path

from _common import build_router, load_config, load_system_prompt

from mini_agent.agent import Agent
from mini_agent.tools import BashTool, ReadTool, WriteTool
from mini_agent.tools.note_tool import RecallNoteTool, SessionNoteTool


async def demo_direct_note_usage():
    """Demo: Direct usage of Session Note tools."""
    print("\n" + "=" * 60)
    print("Demo 1: Direct Session Note Tool Usage")
    print("=" * 60)

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
        note_file = f.name

    try:
        record_tool = SessionNoteTool(memory_file=note_file)
        recall_tool = RecallNoteTool(memory_file=note_file)

        print("\n📝 Recording notes...")

        result = await record_tool.execute(
            content="User is a Python developer working on agent systems",
            category="user_info",
        )
        print(f"  ✓ {result.content}")

        result = await record_tool.execute(
            content="Project name: mini-agent, Tech: Python 3.12 + async",
            category="project_info",
        )
        print(f"  ✓ {result.content}")

        result = await record_tool.execute(
            content="User prefers concise, well-documented code",
            category="user_preference",
        )
        print(f"  ✓ {result.content}")

        print("\n🔍 Recalling all notes...")
        result = await recall_tool.execute()
        print(result.content)

        print("\n🔍 Recalling user preferences only...")
        result = await recall_tool.execute(category="user_preference")
        print(result.content)

        print("\n📄 Memory file content:")
        print("=" * 60)
        notes = json.loads(Path(note_file).read_text())
        print(json.dumps(notes, indent=2, ensure_ascii=False))
        print("=" * 60)

    finally:
        Path(note_file).unlink(missing_ok=True)


async def demo_agent_with_notes():
    """Demo: Agent using Session Notes to remember context."""
    print("\n" + "=" * 60)
    print("Demo 2: Agent with Session Memory")
    print("=" * 60)

    config = load_config()
    if config is None:
        return
    router = build_router(config)

    with tempfile.TemporaryDirectory() as workspace_dir:
        print(f"📁 Workspace: {workspace_dir}\n")

        system_prompt = load_system_prompt(
            fallback="You are a helpful AI assistant.",
        )

        note_instructions = """

IMPORTANT - Session Note Management:
You have access to record_note and recall_notes tools. Use them to:
- record_note: Save important facts, preferences, decisions that should persist
- recall_notes: Retrieve previously saved notes

Guidelines:
- Proactively record key information during conversations
- Recall notes at the start to restore context
- Categories: user_info, user_preference, project_info, decision, etc.
"""
        system_prompt += note_instructions

        memory_file = Path(workspace_dir) / ".agent_memory.json"

        tools = [
            ReadTool(workspace_dir=workspace_dir),
            WriteTool(workspace_dir=workspace_dir),
            BashTool(),
            SessionNoteTool(memory_file=str(memory_file)),
            RecallNoteTool(memory_file=str(memory_file)),
        ]

        # === First Session ===
        print("=" * 60)
        print("Session 1: Teaching the agent about user preferences")
        print("=" * 60)

        agent1 = Agent(
            router=router,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=15,
            workspace_dir=workspace_dir,
        )

        task1 = """
        Hello! Let me introduce myself:
        - I'm Alex, a senior Python developer
        - I'm building an AI agent framework called "mini-agent"
        - I use Python 3.12 with asyncio
        - I prefer type hints and comprehensive docstrings
        - My coding style: clean, functional, well-tested

        Please remember this information for future conversations.
        Also, create a simple README.md file acknowledging you understood.
        """

        print(f"\n📝 User message:\n{task1}\n")
        print("🤖 Agent is working...\n")

        agent1.add_user_message(task1)

        try:
            result1 = await agent1.run()
            print("\n" + "=" * 60)
            print("Agent response:")
            print("=" * 60)
            print(result1)
            print("=" * 60)

            if memory_file.exists():
                notes = json.loads(memory_file.read_text())
                print(f"\n✅ Agent recorded {len(notes)} notes in memory")
                for note in notes:
                    print(f"  - [{note['category']}] {note['content'][:50]}...")
            else:
                print("\n⚠️  No notes found")

        except Exception as e:
            print(f"❌ Error: {e}")
            return

        # === Second Session (New Agent Instance) ===
        print("\n\n" + "=" * 60)
        print("Session 2: New agent instance (simulating new conversation)")
        print("=" * 60)

        agent2 = Agent(
            router=router,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=10,
            workspace_dir=workspace_dir,
        )

        task2 = """
        Hello! I'm back. Do you remember who I am and what project I'm working on?
        What were my code style preferences?
        """

        print(f"\n📝 User message:\n{task2}\n")
        print("🤖 Agent is working (should recall previous notes)...\n")

        agent2.add_user_message(task2)

        try:
            result2 = await agent2.run()
            print("\n" + "=" * 60)
            print("Agent response:")
            print("=" * 60)
            print(result2)
            print("=" * 60)

            print("\n✅ Session Note Demo completed!")
            print("\nKey Points:")
            print("  1. Agent in Session 1 recorded important information")
            print("  2. Agent in Session 2 recalled previous notes")
            print("  3. Memory persists across agent instances via file")

        except Exception as e:
            print(f"❌ Error: {e}")


async def main():
    """Run all demos."""
    print("=" * 60)
    print("Session Note Tool Examples")
    print("=" * 60)
    print("\nSession Notes allow agents to remember context across sessions.")
    print("This is a key feature for building production-ready agents.\n")

    await demo_direct_note_usage()
    print("\n" * 2)
    await demo_agent_with_notes()

    print("\n" + "=" * 60)
    print("All demos completed! ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
