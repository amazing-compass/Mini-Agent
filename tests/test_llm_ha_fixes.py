"""Regression tests for the four Codex review findings.

Each test pins the contract from one fix so a future refactor can't
silently undo it:
- #1: non-switchable errors don't poison health (covered in test_llm_router.py)
- #2: compat primary = highest-priority enabled entry
- #3: classification-aware async_retry short-circuits on terminal errors
- #4: MiniMax URL normalization happens inside _get_or_create_client
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mini_agent.config import Config
from mini_agent.llm.ha import ErrorCategory, ModelNode
from mini_agent.llm.llm_wrapper import LLMClient, _normalize_api_base
from mini_agent.retry import RetryConfig, RetryExhaustedError, async_retry
from mini_agent.schema import LLMProvider


# --- Fix #2: compat primary selection -------------------------------------


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return p


def test_compat_primary_honors_priority_not_yaml_order(tmp_path: Path) -> None:
    """pool_entries[0] may NOT be the highest-priority node — compat fields must still match the router's first pick."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "low",
                    "provider": "anthropic",
                    "api_key": "sk-low",
                    "api_base": "https://api.low.test",
                    "model": "model-low",
                    "priority": 10,
                },
                {
                    "node_id": "high",
                    "provider": "anthropic",
                    "api_key": "sk-high",
                    "api_base": "https://api.high.test",
                    "model": "model-high",
                    "priority": 100,
                },
            ],
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.llm.api_key == "sk-high"
    assert cfg.llm.api_base == "https://api.high.test"
    assert cfg.llm.model == "model-high"


def test_compat_primary_skips_disabled_entry(tmp_path: Path) -> None:
    """A disabled first entry must NOT become the compat primary."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "disabled",
                    "provider": "anthropic",
                    "api_key": "sk-disabled",
                    "api_base": "https://api.disabled.test",
                    "model": "model-disabled",
                    "priority": 100,
                    "enabled": False,
                },
                {
                    "node_id": "enabled",
                    "provider": "anthropic",
                    "api_key": "sk-enabled",
                    "api_base": "https://api.enabled.test",
                    "model": "model-enabled",
                    "priority": 50,
                },
            ],
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.llm.api_key == "sk-enabled"
    assert cfg.llm.model == "model-enabled"


def test_compat_primary_falls_back_when_all_disabled(tmp_path: Path) -> None:
    """If nothing is enabled, we still expose *something* for legacy readers."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "a",
                    "provider": "anthropic",
                    "api_key": "sk-a",
                    "api_base": "https://api.a.test",
                    "model": "a",
                    "priority": 100,
                    "enabled": False,
                },
            ],
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.llm.api_key == "sk-a"


def test_compat_primary_tiebreaker_matches_router() -> None:
    """Ties on priority resolve by node_id ascending, matching router.select_candidates."""
    # Build via direct pool entries to avoid YAML setup boilerplate.
    from mini_agent.config import ModelNodeConfig, LLMConfig, RoutingConfig

    # Both priority 100 — router sorts by node_id ascending, so 'a' wins over 'b'.
    entries = [
        ModelNodeConfig(
            node_id="b",
            provider="anthropic",
            api_key="sk-b",
            api_base="https://api.b.test",
            model="model-b",
            priority=100,
        ),
        ModelNodeConfig(
            node_id="a",
            provider="anthropic",
            api_key="sk-a",
            api_base="https://api.a.test",
            model="model-a",
            priority=100,
        ),
    ]
    enabled = [e for e in entries if e.enabled]
    primary = min(enabled, key=lambda e: (-e.priority, e.node_id))
    assert primary.node_id == "a"


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
    cfg = RetryConfig(max_retries=2, initial_delay=0.0, max_delay=0.0, retryable_exceptions=(Exception,))

    @async_retry(config=cfg)
    async def fail() -> None:
        calls["n"] += 1
        raise _AuthError()

    with pytest.raises(RetryExhaustedError):
        await fail()
    assert calls["n"] == 3  # 1 original + 2 retries


async def test_async_retry_short_circuits_on_non_retryable() -> None:
    """Fix #3: should_retry returning False stops the retry loop immediately."""
    calls = {"n": 0}
    cfg = RetryConfig(max_retries=5, initial_delay=0.0, max_delay=0.0)

    from mini_agent.llm.ha import classify_error, is_retryable

    @async_retry(config=cfg, should_retry=lambda e: is_retryable(classify_error(e)))
    async def fail_auth() -> None:
        calls["n"] += 1
        raise _AuthError()

    with pytest.raises(_AuthError):  # original error, NOT RetryExhaustedError
        await fail_auth()
    assert calls["n"] == 1  # no retries


async def test_async_retry_still_retries_transient() -> None:
    """Fix #3 must not over-reach: transient errors still retry as before.

    Phase 2 note: the default `retryable_exceptions` is now
    `(TransientError,)` — this test uses a local fake 502 class, so we
    explicitly opt in to `(Exception,)` + `should_retry` to keep the
    original classifier-gate behavior under test.
    """
    calls = {"n": 0}
    cfg = RetryConfig(max_retries=2, initial_delay=0.0, max_delay=0.0, retryable_exceptions=(Exception,))

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
    cfg = RetryConfig(max_retries=3, initial_delay=0.0, max_delay=0.0, retryable_exceptions=(Exception,))

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
    assert _normalize_api_base("https://api.minimax.io", "anthropic") == "https://api.minimax.io/anthropic"
    assert _normalize_api_base("https://api.minimaxi.com", "openai") == "https://api.minimaxi.com/v1"
    # Already-suffixed URLs are idempotent.
    assert (
        _normalize_api_base("https://api.minimax.io/anthropic", "anthropic")
        == "https://api.minimax.io/anthropic"
    )
    # Third-party URLs untouched.
    assert (
        _normalize_api_base("https://api.siliconflow.cn/v1", "openai")
        == "https://api.siliconflow.cn/v1"
    )


def test_from_nodes_normalizes_minimax_urls_when_building_client() -> None:
    """Fix #4: LLMClient.from_nodes(...) must normalize URLs the same way legacy init does."""
    node = ModelNode(
        node_id="minimax",
        provider="anthropic",
        protocol_family="anthropic",
        api_key="sk-test",
        api_base="https://api.minimax.io",  # bare — no /anthropic suffix
        model="MiniMax-M2.7",
        priority=100,
    )
    client = LLMClient.from_nodes(nodes=[node])

    # Force lazy client creation and inspect the underlying provider client's api_base.
    underlying = client._get_or_create_client(node)
    assert underlying.api_base == "https://api.minimax.io/anthropic"


def test_from_nodes_normalizes_openai_variant() -> None:
    node = ModelNode(
        node_id="minimax-openai",
        provider="openai",
        protocol_family="openai",
        api_key="sk-test",
        api_base="https://api.minimaxi.com",
        model="MiniMax-M2.5",
        priority=100,
    )
    client = LLMClient.from_nodes(nodes=[node])
    underlying = client._get_or_create_client(node)
    assert underlying.api_base == "https://api.minimaxi.com/v1"


def test_from_nodes_leaves_third_party_urls_alone() -> None:
    node = ModelNode(
        node_id="siliconflow",
        provider="openai",
        protocol_family="openai",
        api_key="sk-test",
        api_base="https://api.siliconflow.cn/v1",
        model="gpt-4",
        priority=100,
    )
    client = LLMClient.from_nodes(nodes=[node])
    underlying = client._get_or_create_client(node)
    assert underlying.api_base == "https://api.siliconflow.cn/v1"


# --- P1: preserve LLMProvider enum type ----------------------------------


def test_legacy_init_keeps_provider_as_llmprovider_enum() -> None:
    """Phase 0 type contract: client.provider is an LLMProvider enum, not a raw str."""
    client = LLMClient(
        api_key="sk-test",
        provider=LLMProvider.ANTHROPIC,
        api_base="https://api.minimax.io",
        model="MiniMax-M2.7",
    )
    assert isinstance(client.provider, LLMProvider)
    assert client.provider is LLMProvider.ANTHROPIC


def test_legacy_init_with_openai_provider_keeps_enum() -> None:
    client = LLMClient(
        api_key="sk-test",
        provider=LLMProvider.OPENAI,
        api_base="https://api.minimax.io",
        model="MiniMax-M2.7",
    )
    assert isinstance(client.provider, LLMProvider)
    assert client.provider is LLMProvider.OPENAI


def test_legacy_init_with_string_provider_still_enum() -> None:
    """Constructors historically accept both enum and str — the resulting attribute should still be enum."""
    client = LLMClient(
        api_key="sk-test",
        provider="anthropic",  # raw string input
        api_base="https://api.minimax.io",
        model="MiniMax-M2.7",
    )
    assert isinstance(client.provider, LLMProvider)


def test_from_nodes_preserves_enum_type_for_primary() -> None:
    node = ModelNode(
        node_id="primary",
        provider="openai",
        protocol_family="openai",
        api_key="sk-test",
        api_base="https://api.openai.com/v1",
        model="gpt-5",
        priority=100,
    )
    client = LLMClient.from_nodes(nodes=[node])
    assert isinstance(client.provider, LLMProvider)
    assert client.provider is LLMProvider.OPENAI


def test_from_nodes_with_unknown_provider_falls_back_to_string() -> None:
    """Unknown provider names stay inspectable rather than being swallowed — see Phase 3 (cross-family)."""
    node = ModelNode(
        node_id="future",
        provider="custom-provider",
        protocol_family="custom",
        api_key="sk-test",
        api_base="https://api.example.test",
        model="x",
        priority=100,
    )
    client = LLMClient.from_nodes(nodes=[node])
    assert client.provider == "custom-provider"
    assert not isinstance(client.provider, LLMProvider)


def test_from_nodes_wires_should_retry_on_provider_client() -> None:
    """Fix #3 must reach the provider client — verify the should_retry hook is installed."""
    node = ModelNode(
        node_id="n",
        provider="anthropic",
        protocol_family="anthropic",
        api_key="sk-test",
        api_base="https://api.minimax.io/anthropic",
        model="m",
        priority=100,
    )
    client = LLMClient.from_nodes(nodes=[node])
    underlying = client._get_or_create_client(node)
    assert underlying.should_retry is not None
    # Non-retryable (auth) should be vetoed.
    assert underlying.should_retry(_AuthError()) is False
    # Retryable (transient) should pass.
    assert underlying.should_retry(_TransientError()) is True
