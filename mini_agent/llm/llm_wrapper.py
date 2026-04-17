"""LLM client wrapper with pool-based failover routing.

Public surface preserved from Phase 0:
- `LLMClient(api_key=..., provider=..., api_base=..., model=..., retry_config=...)`
  continues to construct a single-node client.
- `.generate(messages, tools)` still returns an `LLMResponse`.
- `.retry_callback` still propagates to the underlying provider clients.

Phase 1 additions:
- `LLMClient.from_nodes(nodes, retry_config, ...)` for pool-based usage.
- Internally every instance owns a `ModelRouter` + `ModelPool`; single-node
  mode is just a pool of size 1.
- `.last_routing_decision` exposes the most recent routing outcome for logs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..retry import RetryConfig
from ..schema import LLMProvider, LLMResponse, Message
from .anthropic_client import AnthropicClient
from .base import LLMClientBase
from .ha import (
    ErrorCategory,
    HealthRegistry,
    ModelNode,
    ModelPool,
    ModelRouter,
    RoutingDecision,
    classify_error,
    is_retryable,
)
from .openai_client import OpenAIClient

logger = logging.getLogger(__name__)


# MiniMax API domains that need automatic suffix handling.
MINIMAX_DOMAINS = ("api.minimax.io", "api.minimaxi.com")


def _normalize_api_base(api_base: str, provider: str) -> str:
    """Resolve the final API base URL, handling MiniMax suffix auto-append."""
    api_base = api_base.rstrip("/")
    is_minimax = any(domain in api_base for domain in MINIMAX_DOMAINS)
    if not is_minimax:
        return api_base

    # Strip any existing suffix so we don't double-append.
    stripped = api_base.replace("/anthropic", "").replace("/v1", "")
    if provider == LLMProvider.ANTHROPIC.value:
        return f"{stripped}/anthropic"
    if provider == LLMProvider.OPENAI.value:
        return f"{stripped}/v1"
    raise ValueError(f"Unsupported provider for MiniMax normalization: {provider!r}")


def _provider_value(provider: LLMProvider | str) -> str:
    if isinstance(provider, LLMProvider):
        return provider.value
    return str(provider).lower()


class LLMClient:
    """Pool-aware LLM client.

    For MiniMax API (api.minimax.io / api.minimaxi.com) the appropriate
    endpoint suffix (`/anthropic` or `/v1`) is auto-appended based on
    provider. Third-party OpenAI-compatible URLs (e.g. siliconflow) are
    used as-is.
    """

    MINIMAX_DOMAINS = MINIMAX_DOMAINS

    def __init__(
        self,
        api_key: str | None = None,
        provider: LLMProvider | str = LLMProvider.ANTHROPIC,
        api_base: str = "https://api.minimaxi.com",
        model: str = "MiniMax-M2.5",
        retry_config: RetryConfig | None = None,
        *,
        nodes: list[ModelNode] | None = None,
        strategy: str = "priority",
        failure_threshold: int = 3,
    ) -> None:
        """Initialize the client.

        Single-node mode (legacy): pass api_key/provider/api_base/model.
        Pool mode: pass `nodes=[...]` (already api-key-resolved).

        Args:
            api_key: API key for the legacy single-node path.
            provider: Provider of the legacy single-node path.
            api_base: Base URL for the legacy single-node path.
            model: Model name for the legacy single-node path.
            retry_config: Retry config applied to *every* underlying provider
                client (each node gets its own retry envelope).
            nodes: Explicit pool of ModelNode; overrides the legacy args.
            strategy: Router strategy (Phase 1: only "priority").
            failure_threshold: Consecutive failures before a node is
                considered unhealthy.
        """
        self.retry_config = retry_config or RetryConfig()

        if nodes is None:
            if api_key is None:
                raise ValueError("Either `nodes` or `api_key` must be provided")
            provider_str = _provider_value(provider)
            full_api_base = _normalize_api_base(api_base, provider_str)
            nodes = [
                ModelNode(
                    node_id="default",
                    provider=provider_str,
                    protocol_family=provider_str,
                    api_key=api_key,
                    api_base=full_api_base,
                    model=model,
                    priority=100,
                )
            ]

        self.pool = ModelPool(nodes)
        self.health_registry = HealthRegistry(failure_threshold=failure_threshold)
        self.router = ModelRouter(self.pool, self.health_registry, strategy=strategy)

        self._clients: dict[str, LLMClientBase] = {}
        self._retry_callback: Callable[[Exception, int], None] | None = None
        self.last_routing_decision: RoutingDecision | None = None
        self.on_failover: Callable[[ModelNode, ModelNode, Exception, ErrorCategory], None] | None = None
        self.router.on_failover = self._handle_failover

        # Surface "primary" node details for backward compat (agent code and
        # logs still read llm_client.model / .provider / .api_base). Phase 0
        # exposed `provider` as an LLMProvider enum — preserve that type so
        # isinstance-checks and type hints on existing callers still hold.
        primary = self.pool.all()[0]
        try:
            self.provider: LLMProvider | str = LLMProvider(primary.provider)
        except ValueError:
            # Unknown provider name — keep the raw string so the value is
            # still inspectable in logs rather than hidden behind a fallback.
            self.provider = primary.provider
        self.api_key = primary.api_key
        self.api_base = primary.api_base
        self.model = primary.model

        logger.info(
            "Initialized LLM client pool: %d node(s), strategy=%s, primary=%s (%s @ %s)",
            len(self.pool),
            strategy,
            primary.node_id,
            primary.model,
            primary.api_base,
        )

    # ------------------------------------------------------------------ API

    @classmethod
    def from_nodes(
        cls,
        nodes: list[ModelNode],
        retry_config: RetryConfig | None = None,
        *,
        strategy: str = "priority",
        failure_threshold: int = 3,
    ) -> "LLMClient":
        """Construct a pool-based client from pre-resolved ModelNode entries."""
        return cls(
            nodes=nodes,
            retry_config=retry_config,
            strategy=strategy,
            failure_threshold=failure_threshold,
        )

    @property
    def retry_callback(self) -> Callable[[Exception, int], None] | None:
        return self._retry_callback

    @retry_callback.setter
    def retry_callback(self, value: Callable[[Exception, int], None] | None) -> None:
        self._retry_callback = value
        for client in self._clients.values():
            client.retry_callback = value

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> LLMResponse:
        """Route the request through the pool, failing over on error."""

        async def call_on_node(node: ModelNode) -> LLMResponse:
            client = self._get_or_create_client(node)
            return await client.generate(messages, tools)

        response, decision = await self.router.execute(call_on_node)
        self.last_routing_decision = decision
        return response

    def health_snapshots(self) -> dict[str, Any]:
        """Expose per-node health snapshots for logs / /stats commands."""
        return self.health_registry.snapshots()

    # --------------------------------------------------------------- helpers

    def _get_or_create_client(self, node: ModelNode) -> LLMClientBase:
        cached = self._clients.get(node.node_id)
        if cached is not None:
            return cached

        provider = node.provider.lower()
        # Centralized MiniMax URL normalization: the node's api_base may be
        # `https://api.minimax.io` (as it appears in config.yaml) without a
        # protocol suffix. Previously only the legacy __init__ path handled
        # this; doing it here means `LLMClient.from_nodes(...)` works the
        # same way without callers needing to pre-normalize.
        normalized_base = _normalize_api_base(node.api_base, provider)

        if provider == LLMProvider.ANTHROPIC.value:
            client: LLMClientBase = AnthropicClient(
                api_key=node.api_key,
                api_base=normalized_base,
                model=node.model,
                retry_config=self.retry_config,
            )
        elif provider == LLMProvider.OPENAI.value:
            client = OpenAIClient(
                api_key=node.api_key,
                api_base=normalized_base,
                model=node.model,
                retry_config=self.retry_config,
            )
        else:
            raise ValueError(f"Unsupported provider for node {node.node_id!r}: {node.provider!r}")

        client.retry_callback = self._retry_callback
        # Classification-aware in-node retry: skip retry for terminal errors
        # so the router can fail over without burning the full backoff budget.
        client.should_retry = lambda exc: is_retryable(classify_error(exc))
        self._clients[node.node_id] = client
        return client

    def _handle_failover(
        self,
        failed: ModelNode,
        next_node: ModelNode,
        exc: Exception,
        category: ErrorCategory,
    ) -> None:
        """Forward router failover events to the user-supplied callback."""
        if self.on_failover is not None:
            try:
                self.on_failover(failed, next_node, exc, category)
            except Exception:
                logger.exception("on_failover callback raised")
