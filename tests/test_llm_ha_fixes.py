"""Regression tests for historical HA fixes.

Phase 3 removed the `LLMClient` facade. Tests that pinned facade-specific
behavior (compat primary, enum preservation, should_retry injection)
have been dropped because the facade itself no longer exists. The
behaviors that still matter post-facade-removal are re-pinned here:

- MiniMax URL normalization: moved to `mini_agent.llm.ha.factory.normalize_api_base`
- `build_client_factory(...)` builds provider clients with correct URLs
- Classification-aware retry (now via default `retryable_exceptions=(TransientError,)`)
"""

from __future__ import annotations

import pytest

from mini_agent.llm.ha import (
    ModelNode,
    build_client_factory,
    normalize_api_base,
)
from mini_agent.retry import RetryConfig, RetryExhaustedError, async_retry


# --- Fix #3: classification-aware retry -----------------------------------


class _AuthError(Exception):
    """401-like error that should NOT be retried."""

    def __init__(self) -> None:
        super().__init__("unauthorized")
        self.status_code = 401


class _TransientError(Exception):
    """502-like error that SHOULD be retried."""

    def __init__(self) -> None:
        super().__init__("bad gateway")
        self.status_code = 502


async def test_async_retry_without_should_retry_retries_everything() -> None:
    """Legacy behavior — `retryable_exceptions=(Exception,)` catches everything.

    Phase 2 changed the DEFAULT to `(TransientError,)`, so to reproduce
    the old "retry everything" behavior tests must opt in explicitly
    by passing `retryable_exceptions=(Exception,)`.
    """
    calls = {"n": 0}
    cfg = RetryConfig(
        max_retries=2,
        initial_delay=0.0,
        max_delay=0.0,
        retryable_exceptions=(Exception,),
    )

    @async_retry(config=cfg)
    async def fail() -> None:
        calls["n"] += 1
        raise _AuthError()

    with pytest.raises(RetryExhaustedError):
        await fail()
    assert calls["n"] == 3  # 1 original + 2 retries


async def test_async_retry_short_circuits_on_non_retryable() -> None:
    """should_retry returning False stops the retry loop immediately.

    Phase 3 keeps `should_retry` as an optional plug for callers that
    still want a classifier gate; the default retry path no longer uses it.
    """
    calls = {"n": 0}
    cfg = RetryConfig(
        max_retries=5,
        initial_delay=0.0,
        max_delay=0.0,
        retryable_exceptions=(Exception,),
    )

    from mini_agent.llm.ha import classify_error, is_retryable

    @async_retry(config=cfg, should_retry=lambda e: is_retryable(classify_error(e)))
    async def fail_auth() -> None:
        calls["n"] += 1
        raise _AuthError()

    with pytest.raises(_AuthError):  # original error, NOT RetryExhaustedError
        await fail_auth()
    assert calls["n"] == 1  # no retries


async def test_async_retry_still_retries_transient() -> None:
    """Transient errors still retry as before when the classifier gate is used."""
    calls = {"n": 0}
    cfg = RetryConfig(
        max_retries=2,
        initial_delay=0.0,
        max_delay=0.0,
        retryable_exceptions=(Exception,),
    )

    from mini_agent.llm.ha import classify_error, is_retryable

    @async_retry(config=cfg, should_retry=lambda e: is_retryable(classify_error(e)))
    async def fail_transient() -> None:
        calls["n"] += 1
        raise _TransientError()

    with pytest.raises(RetryExhaustedError):
        await fail_transient()
    assert calls["n"] == 3  # retried fully


async def test_async_retry_recovers_when_transient_eventually_succeeds() -> None:
    calls = {"n": 0}
    cfg = RetryConfig(
        max_retries=3,
        initial_delay=0.0,
        max_delay=0.0,
        retryable_exceptions=(Exception,),
    )

    from mini_agent.llm.ha import classify_error, is_retryable

    @async_retry(config=cfg, should_retry=lambda e: is_retryable(classify_error(e)))
    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _TransientError()
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


# --- Fix #4: MiniMax URL normalization centralized ------------------------


def test_normalize_api_base_pure_helper() -> None:
    """Sanity check the helper isn't silently broken."""
    assert normalize_api_base("https://api.minimax.io", "anthropic") == "https://api.minimax.io/anthropic"
    assert normalize_api_base("https://api.minimaxi.com", "openai") == "https://api.minimaxi.com/v1"
    # Already-suffixed URLs are idempotent.
    assert (
        normalize_api_base("https://api.minimax.io/anthropic", "anthropic")
        == "https://api.minimax.io/anthropic"
    )
    # Third-party URLs untouched.
    assert (
        normalize_api_base("https://api.siliconflow.cn/v1", "openai")
        == "https://api.siliconflow.cn/v1"
    )


def test_build_client_factory_normalizes_minimax_urls() -> None:
    """Regression: the pool's client factory must normalize URLs the
    same way the deleted `LLMClient` facade used to."""
    node = ModelNode(
        node_id="minimax",
        provider="anthropic",
        protocol_family="anthropic",
        api_key="sk-test",
        api_base="https://api.minimax.io",  # bare — no /anthropic suffix
        model="MiniMax-M2.7",
        priority=100,
    )
    build = build_client_factory()
    client = build(node)
    assert client.api_base == "https://api.minimax.io/anthropic"


def test_build_client_factory_normalizes_openai_variant() -> None:
    node = ModelNode(
        node_id="minimax-openai",
        provider="openai",
        protocol_family="openai",
        api_key="sk-test",
        api_base="https://api.minimaxi.com",
        model="MiniMax-M2.5",
        priority=100,
    )
    build = build_client_factory()
    client = build(node)
    assert client.api_base == "https://api.minimaxi.com/v1"


def test_build_client_factory_leaves_third_party_urls_alone() -> None:
    node = ModelNode(
        node_id="siliconflow",
        provider="openai",
        protocol_family="openai",
        api_key="sk-test",
        api_base="https://api.siliconflow.cn/v1",
        model="gpt-4",
        priority=100,
    )
    build = build_client_factory()
    client = build(node)
    assert client.api_base == "https://api.siliconflow.cn/v1"


def test_build_client_factory_injects_per_node_max_output_tokens() -> None:
    """Per-node max_output_tokens must reach the underlying provider client."""
    node = ModelNode(
        node_id="n",
        provider="anthropic",
        protocol_family="anthropic",
        api_key="sk-test",
        api_base="https://api.minimax.io/anthropic",
        model="m",
        priority=100,
        max_output_tokens=7777,
    )
    build = build_client_factory()
    client = build(node)
    assert client.default_max_tokens == 7777


def test_build_client_factory_rejects_unknown_provider() -> None:
    node = ModelNode(
        node_id="future",
        provider="custom-provider",
        protocol_family="custom",
        api_key="sk-test",
        api_base="https://api.example.test",
        model="x",
        priority=100,
    )
    build = build_client_factory()
    with pytest.raises(ValueError, match="Unsupported provider"):
        build(node)
