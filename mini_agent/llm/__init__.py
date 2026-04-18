"""LLM clients package supporting both Anthropic and OpenAI protocols.

Phase 3: the `LLMClient` facade has been removed. Provider clients
(`AnthropicClient`, `OpenAIClient`) are materialized inside
`mini_agent.llm.ha.ModelPool` via `build_client_factory`, and Agent
talks to the pool through `mini_agent.llm.ha.ModelRouter`.
"""

from .anthropic_client import AnthropicClient
from .base import LLMClientBase
from .openai_client import OpenAIClient

__all__ = ["LLMClientBase", "AnthropicClient", "OpenAIClient"]
