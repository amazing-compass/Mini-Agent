"""Demo: Using Tool schemas with the base Tool class.

Phase 3: the removed `LLMClient` facade is replaced by the provider
clients (Anthropic or OpenAI) that the router assembles under the hood.
This demo reaches for the Anthropic client directly because the focus
is tool-schema conversion, not routing.
"""

import asyncio
from typing import Any

from _common import build_direct_client, load_config

from mini_agent.schema import LLMProvider, Message
from mini_agent.tools.base import Tool, ToolResult


class WeatherTool(Tool):
    """Example weather tool."""

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "Get current weather information for a location. Returns temperature and conditions."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City and state, e.g. 'San Francisco, CA' or 'London, UK'",
                },
                "unit": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"],
                    "description": "Temperature unit (celsius or fahrenheit)",
                },
            },
            "required": ["location"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="Weather data")


class SearchTool(Tool):
    """Example search tool."""

    @property
    def name(self) -> str:
        return "search_web"

    @property
    def description(self) -> str:
        return "Search the web for information about a topic"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-10)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="Search results")


class CalculatorTool(Tool):
    """Example calculator tool."""

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return "Perform arithmetic calculations"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Mathematical expression to evaluate, e.g. '2 + 2' or '10 * 5'",
                }
            },
            "required": ["expression"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="Calculation result")


class TranslateTool(Tool):
    """Example translate tool."""

    @property
    def name(self) -> str:
        return "translate"

    @property
    def description(self) -> str:
        return "Translate text from one language to another"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to translate"},
                "target_language": {
                    "type": "string",
                    "description": "Target language code (e.g. 'en', 'es', 'fr')",
                },
            },
            "required": ["text", "target_language"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="Translation result")


async def demo_tool_schemas():
    """Demo: passing Tool objects to the Anthropic client."""
    config = load_config()
    if config is None:
        return

    print("=" * 60)
    print("Method 1: Using Tool Objects with LLM")
    print("=" * 60)

    weather_tool = WeatherTool()
    search_tool = SearchTool()

    client = build_direct_client(config, LLMProvider.ANTHROPIC)

    messages = [
        Message(
            role="user",
            content="What's the weather like in Tokyo? I want it in celsius.",
        )
    ]

    print("\nQuery: What's the weather like in Tokyo? I want it in celsius.")
    print("\nAvailable tools:")
    print(f"  1. {weather_tool.name}: {weather_tool.description}")
    print(f"  2. {search_tool.name}: {search_tool.description}")

    response = await client.generate(messages, tools=[weather_tool, search_tool])

    print(f"\nResponse content: {response.content}")

    if response.thinking:
        print(f"\nThinking: {response.thinking}")

    if response.tool_calls:
        print(f"\nTool calls made: {len(response.tool_calls)}")
        for tool_call in response.tool_calls:
            print(f"  - Function: {tool_call.function.name}")
            print(f"    Arguments: {tool_call.function.arguments}")


async def demo_multiple_tools():
    """Demo: multiple Tool instances passed to a single request."""
    config = load_config()
    if config is None:
        return

    print("\n" + "=" * 60)
    print("Method 2: Using Multiple Tool Instances")
    print("=" * 60)

    calculator_tool = CalculatorTool()
    translate_tool = TranslateTool()

    client = build_direct_client(config, LLMProvider.ANTHROPIC)

    messages = [Message(role="user", content="Calculate 15 * 23 for me")]

    print("\nQuery: Calculate 15 * 23 for me")
    print("\nAvailable tools:")
    print("  1. calculator (Tool)")
    print("  2. translate (Tool)")

    response = await client.generate(messages, tools=[calculator_tool, translate_tool])

    print(f"\nResponse content: {response.content}")

    if response.thinking:
        print(f"\nThinking: {response.thinking}")

    if response.tool_calls:
        print(f"\nTool calls made: {len(response.tool_calls)}")
        for tool_call in response.tool_calls:
            print(f"  - Function: {tool_call.function.name}")
            print(f"    Arguments: {tool_call.function.arguments}")


async def demo_tool_schema_methods():
    """Demo: schema-conversion methods (no LLM call)."""
    print("\n" + "=" * 60)
    print("Method 3: Tool Schema Conversion Methods")
    print("=" * 60)

    weather_tool = WeatherTool()

    print("\nTool to Anthropic schema (to_schema):")
    anthropic_schema = weather_tool.to_schema()
    print(f"  {anthropic_schema}")

    print("\nTool to OpenAI schema (to_openai_schema):")
    openai_schema = weather_tool.to_openai_schema()
    print(f"  {openai_schema}")

    print("\nSchema methods allow flexible tool usage with different LLM providers.")


async def main():
    """Run all demos."""
    print("\n🚀 Tool Schema Demo - Using Tool Base Class\n")

    try:
        await demo_tool_schemas()
        await demo_multiple_tools()
        await demo_tool_schema_methods()

        print("\n✅ All demos completed successfully!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
