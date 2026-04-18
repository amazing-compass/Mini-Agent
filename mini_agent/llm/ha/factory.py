"""Client factory for the node pool.

Phase 3 consolidates the pieces that used to live inside the deleted
`LLMClient` facade:

- `normalize_api_base()` — MiniMax global/China endpoints need the
  protocol suffix (`/anthropic` or `/v1`) appended based on provider;
  third-party OpenAI-compatible URLs (siliconflow, etc.) pass through
  unchanged.
- `build_client_factory()` — returns a callable `(ModelNode) ->
  LLMClientBase` suitable for passing to `ModelPool(nodes, build_client=
  ...)`. The factory wires `default_max_tokens=node.max_output_tokens`
  into every underlying client so router-issued requests honor the
  per-node output cap, and direct callers still get a sensible legacy
  default (see base.py / anthropic_client.py notes).

Design §1.0 + §4.3 + §13.7 step 1: eager per-node clients, all
provider-specific normalization done at pool-construction time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...retry import RetryConfig
from ...schema import LLMProvider
from .models import ModelNode

if TYPE_CHECKING:
    from ..base import LLMClientBase


# MiniMax API domains that need automatic protocol-suffix handling.
MINIMAX_DOMAINS = ("api.minimax.io", "api.minimaxi.com")


def normalize_api_base(api_base: str, provider: str) -> str:
    """Resolve the final API base URL.

    MiniMax's global (`api.minimax.io`) and China (`api.minimaxi.com`)
    endpoints serve Anthropic-compatible traffic under `/anthropic` and
    OpenAI-compatible traffic under `/v1`. Users may paste either the
    bare domain or an already-suffixed URL — this helper makes both
    work identically.

    Third-party OpenAI-compatible gateways (siliconflow, etc.) are
    returned verbatim.
    """
    api_base = api_base.rstrip("/")
    is_minimax = any(domain in api_base for domain in MINIMAX_DOMAINS)
    if not is_minimax:
        return api_base

    # Strip any existing suffix so we don't double-append when users pass
    # URLs that already include the provider suffix.
    stripped = api_base.replace("/anthropic", "").replace("/v1", "")
    if provider == LLMProvider.ANTHROPIC.value:
        return f"{stripped}/anthropic"
    if provider == LLMProvider.OPENAI.value:
        return f"{stripped}/v1"
    raise ValueError(f"Unsupported provider for MiniMax normalization: {provider!r}")


def build_client_factory(
    retry_config: RetryConfig | None = None,
) -> Callable[[ModelNode], "LLMClientBase"]:
    """Return a `(ModelNode) -> LLMClientBase` callable.

    Closes over the shared `retry_config` so every pool node receives
    the same retry envelope. Per-node knobs (provider, api_base, model,
    max_output_tokens) come from the node itself. MiniMax URL
    normalization happens inside the closure so callers can pass the
    bare domain.

    Provider-client imports are deferred to inside the returned
    closure to avoid the circular import cycle
    `ha/__init__.py → factory.py → anthropic_client.py → ha.errors`.
    """

    def build_client(node: ModelNode) -> "LLMClientBase":
        from ..anthropic_client import AnthropicClient  # noqa: PLC0415
        from ..openai_client import OpenAIClient  # noqa: PLC0415

        provider = node.provider.lower()
        normalized_base = normalize_api_base(node.api_base, provider)

        if provider == LLMProvider.ANTHROPIC.value:
            return AnthropicClient(
                api_key=node.api_key,
                api_base=normalized_base,
                model=node.model,
                retry_config=retry_config,
                default_max_tokens=node.max_output_tokens,
            )
        if provider == LLMProvider.OPENAI.value:
            return OpenAIClient(
                api_key=node.api_key,
                api_base=normalized_base,
                model=node.model,
                retry_config=retry_config,
                default_max_tokens=node.max_output_tokens,
            )
        raise ValueError(
            f"Unsupported provider for node {node.node_id!r}: {node.provider!r}"
        )

    return build_client
