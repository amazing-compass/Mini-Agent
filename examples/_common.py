"""Shared helpers for the `examples/` scripts.

Phase 3 removed the `LLMClient` facade — examples now assemble a real
`ModelRouter` from the configured pool. This module centralizes that
wiring so each demo stays focused on its topic (agent loop, session
notes, MCP tools, provider clients) instead of re-declaring the same
~20 lines of HA setup.
"""

from __future__ import annotations

from pathlib import Path

from mini_agent.config import Config
from mini_agent.llm.anthropic_client import AnthropicClient
from mini_agent.llm.base import LLMClientBase
from mini_agent.llm.ha import (
    ModelNode,
    ModelPool,
    ModelRouter,
    SimpleBreaker,
    build_client_factory,
    normalize_api_base,
)
from mini_agent.llm.openai_client import OpenAIClient
from mini_agent.schema import LLMProvider


DEFAULT_CONFIG_PATH = Path("mini_agent/config/config.yaml")
DEFAULT_SYSTEM_PROMPT_PATH = Path("mini_agent/config/system_prompt.md")


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config | None:
    """Load `config.yaml` or return `None` with a friendly message."""
    p = Path(path)
    if not p.exists():
        print(f"❌ {p} not found. Please set up your API key first.")
        print(f"   Run: cp mini_agent/config/config-example.yaml {p}")
        return None
    return Config.from_yaml(p)


def load_system_prompt(
    path: Path | str = DEFAULT_SYSTEM_PROMPT_PATH,
    fallback: str = "You are a helpful AI assistant that can use tools.",
) -> str:
    p = Path(path)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return fallback


def _entry_to_node(entry) -> ModelNode:
    return ModelNode(
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


def build_router(config: Config) -> ModelRouter:
    """Materialize a `ModelRouter` from `config.llm`.

    Equivalent to the pool + breaker + router assembly that lives inside
    `mini_agent.cli.run_agent` — examples that drive `Agent` need the
    same plumbing, minus the CLI-only retry / failover callbacks.
    """
    nodes = [_entry_to_node(e) for e in config.llm.pool]
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


def build_direct_client(config: Config, provider: LLMProvider) -> LLMClientBase:
    """Pick a pool node matching `provider` and return a raw provider client.

    Examples that want to demo provider-specific request shapes (05, 06)
    skip the router and talk to the underlying client directly. If the
    pool doesn't contain a node for the requested provider, we fall back
    to the highest-priority node's credentials — useful for MiniMax
    which serves both protocols from the same key.
    """
    pool_entries = config.llm.pool
    if not pool_entries:
        raise RuntimeError("config.llm.pool is empty; add at least one node")

    # Prefer an exact provider match; otherwise reuse the first node's key
    # and point at the right protocol suffix.
    match = next(
        (e for e in pool_entries if e.provider.lower() == provider.value),
        None,
    )
    source = match or pool_entries[0]

    api_base = normalize_api_base(source.api_base, provider.value)
    if provider == LLMProvider.ANTHROPIC:
        return AnthropicClient(
            api_key=source.api_key or "",
            api_base=api_base,
            model=source.model,
            default_max_tokens=source.max_output_tokens,
        )
    if provider == LLMProvider.OPENAI:
        return OpenAIClient(
            api_key=source.api_key or "",
            api_base=api_base,
            model=source.model,
            default_max_tokens=source.max_output_tokens,
        )
    raise ValueError(f"Unsupported provider: {provider!r}")
