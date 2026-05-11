"""Config parsing tests for the Phase 1 pool."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from mini_agent.config import Config


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return p


def test_legacy_flat_config_is_rejected(tmp_path: Path) -> None:
    """Phase 3 removes the legacy flat shape — top-level LLM fields must be rejected."""
    p = _write_yaml(
        tmp_path,
        {
            "api_key": "sk-legacy",
            "api_base": "https://api.minimaxi.com",
            "provider": "anthropic",
            "model": "MiniMax-M2.5",
        },
    )
    with pytest.raises(ValueError, match="Top-level LLM fields"):
        Config.from_yaml(p)


def test_missing_pool_is_rejected(tmp_path: Path) -> None:
    """Phase 3 requires an explicit `pool:` block."""
    p = _write_yaml(tmp_path, {"routing": {"strategy": "priority"}})
    with pytest.raises(ValueError, match="pool"):
        Config.from_yaml(p)


def test_three_explicit_nodes_in_pool(tmp_path: Path) -> None:
    """Main use case: three MiniMax nodes, each with its own key (same value OK)."""
    shared_key = "sk-shared-but-written-three-times"
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "m27",
                    "provider": "anthropic",
                    "api_key": shared_key,
                    "api_base": "https://api.minimaxi.com",
                    "model": "MiniMax-M2.7",
                    "priority": 100,
                },
                {
                    "node_id": "m25",
                    "provider": "anthropic",
                    "api_key": shared_key,
                    "api_base": "https://api.minimaxi.com",
                    "model": "MiniMax-M2.5",
                    "priority": 80,
                },
                {
                    "node_id": "m21",
                    "provider": "anthropic",
                    "api_key": shared_key,
                    "api_base": "https://api.minimaxi.com",
                    "model": "MiniMax-M2.1",
                    "priority": 60,
                },
            ],
        },
    )
    cfg = Config.from_yaml(p)

    assert [n.node_id for n in cfg.llm.pool] == ["m27", "m25", "m21"]
    assert [n.model for n in cfg.llm.pool] == ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1"]
    assert [n.priority for n in cfg.llm.pool] == [100, 80, 60]
    assert all(n.api_key == shared_key for n in cfg.llm.pool)


def test_pool_entry_without_api_key_raises(tmp_path: Path) -> None:
    """Each pool entry must declare its own `api_key` — the pool does NOT inherit."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "orphan",
                    "provider": "anthropic",
                    "api_base": "https://api.minimaxi.com",
                    "model": "MiniMax-M2.7",
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="api_key is required"):
        Config.from_yaml(p)


def test_cross_provider_pool(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "primary",
                    "provider": "anthropic",
                    "api_key": "sk-anthropic",
                    "api_base": "https://api.minimax.io",
                    "model": "MiniMax-M2.7",
                },
                {
                    "node_id": "backup",
                    "provider": "openai",
                    "api_key": "sk-openai",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5",
                },
            ],
        },
    )
    cfg = Config.from_yaml(p)
    primary, backup = cfg.llm.pool
    assert primary.provider == "anthropic" and primary.api_key == "sk-anthropic"
    assert backup.provider == "openai" and backup.api_key == "sk-openai"


def test_api_key_env_resolution(tmp_path: Path) -> None:
    os.environ["TEST_CONFIG_POOL_KEY"] = "resolved-env-value"
    try:
        p = _write_yaml(
            tmp_path,
            {
                "pool": [
                    {
                        "node_id": "a",
                        "provider": "anthropic",
                        "api_key_env": "TEST_CONFIG_POOL_KEY",
                        "api_base": "https://api.minimax.io",
                        "model": "MiniMax-M2.7",
                    }
                ],
            },
        )
        cfg = Config.from_yaml(p)
        assert cfg.llm.pool[0].api_key == "resolved-env-value"
    finally:
        del os.environ["TEST_CONFIG_POOL_KEY"]


def test_placeholder_api_key_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "a",
                    "provider": "anthropic",
                    "api_key": "YOUR_API_KEY_HERE",
                    "api_base": "https://api.minimax.io",
                    "model": "MiniMax-M2.7",
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="valid API Key"):
        Config.from_yaml(p)


def test_pool_routing_defaults_applied(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "a",
                    "provider": "anthropic",
                    "api_key": "sk-x",
                    "api_base": "https://api.minimax.io",
                    "model": "MiniMax-M2.7",
                }
            ],
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.llm.routing.strategy == "priority"
    # Phase 2: breaker owns failure_threshold; routing doesn't carry it.
    assert cfg.llm.breaker.failure_threshold == 3


def test_pool_breaker_overrides_honored(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        {
            "routing": {"strategy": "priority"},
            "breaker": {"failure_threshold": 5, "cooldown_seconds": 30.0},
            "pool": [
                {
                    "node_id": "a",
                    "provider": "anthropic",
                    "api_key": "sk-x",
                    "api_base": "https://api.minimax.io",
                    "model": "MiniMax-M2.7",
                }
            ],
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.llm.breaker.failure_threshold == 5
    assert cfg.llm.breaker.cooldown_seconds == 30.0


def test_routing_failure_threshold_is_rejected(tmp_path: Path) -> None:
    """Loud failure instead of a silent no-op — design §5.8 moved the knob to breaker."""
    p = _write_yaml(
        tmp_path,
        {
            "routing": {"strategy": "priority", "failure_threshold": 5},
            "pool": [
                {
                    "node_id": "a",
                    "provider": "anthropic",
                    "api_key": "sk-x",
                    "api_base": "https://api.minimax.io",
                    "model": "MiniMax-M2.7",
                }
            ],
        },
    )
    import pytest

    with pytest.raises(ValueError, match="routing.failure_threshold"):
        Config.from_yaml(p)


def test_deepseek_pool_entry_preserves_cache_flags(tmp_path: Path) -> None:
    """DeepSeek V4 Pro config keeps the cache capability split intact.

    IMPROVEMENT_04 depends on this exact capability shape: DeepSeek does
    not use Anthropic explicit cache_control markers, but it does have
    automatic Context Caching, so the DP policy must keep hit/miss
    pricing enabled instead of degrading to cache==input.
    """
    p = _write_yaml(
        tmp_path,
        {
            "pool": [
                {
                    "node_id": "deepseek-v4-pro",
                    "provider": "anthropic",
                    "api_key": "sk-deepseek",
                    "api_base": "https://api.deepseek.com/anthropic",
                    "model": "deepseek-v4-pro",
                    "priority": 110,
                    "context_window": 1_000_000,
                    "max_output_tokens": 8192,
                    "supports_explicit_cache_control": False,
                    "supports_automatic_context_cache": True,
                }
            ],
        },
    )
    cfg = Config.from_yaml(p)
    assert len(cfg.llm.pool) == 1
    node = cfg.llm.pool[0]
    assert node.node_id == "deepseek-v4-pro"
    assert node.provider == "anthropic"
    assert node.protocol_family == "anthropic"
    assert node.api_key
    assert node.api_base == "https://api.deepseek.com/anthropic"
    assert node.model == "deepseek-v4-pro"
    assert node.supports_explicit_cache_control is False
    assert node.supports_automatic_context_cache is True


# ----------------------------------------------------------------------
# Agent section — nested `agent:` block is the blessed form.
# Top-level keys are kept as a back-compat shim (mirrors retry/routing/
# breaker which also accept either nested or top-level), but nested
# MUST win when both are present.
# ----------------------------------------------------------------------

_MINIMAL_POOL: list[dict] = [
    {
        "node_id": "a",
        "provider": "anthropic",
        "api_key": "sk-x",
        "api_base": "https://api.minimax.io",
        "model": "MiniMax-M2.7",
    }
]


def test_nested_agent_block_is_honored(tmp_path: Path) -> None:
    """A YAML with `agent: {max_steps: 7, ...}` must NOT be silently dropped."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": _MINIMAL_POOL,
            "agent": {
                "max_steps": 7,
                "workspace_dir": "/tmp/nested-ws",
                "system_prompt_path": "nested_prompt.md",
            },
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.agent.max_steps == 7
    assert cfg.agent.workspace_dir == "/tmp/nested-ws"
    assert cfg.agent.system_prompt_path == "nested_prompt.md"


def test_top_level_agent_fields_still_work_as_legacy_shim(tmp_path: Path) -> None:
    """Legacy top-level shape must keep loading (back-compat)."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": _MINIMAL_POOL,
            "max_steps": 11,
            "workspace_dir": "/tmp/legacy-ws",
            "system_prompt_path": "legacy_prompt.md",
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.agent.max_steps == 11
    assert cfg.agent.workspace_dir == "/tmp/legacy-ws"
    assert cfg.agent.system_prompt_path == "legacy_prompt.md"


def test_nested_agent_wins_over_top_level(tmp_path: Path) -> None:
    """When both forms appear, the nested `agent:` block takes precedence."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": _MINIMAL_POOL,
            "max_steps": 11,            # legacy
            "workspace_dir": "/legacy",  # legacy
            "agent": {
                "max_steps": 7,          # should win
                "workspace_dir": "/nested",  # should win
            },
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.agent.max_steps == 7
    assert cfg.agent.workspace_dir == "/nested"


def test_agent_block_partial_fills_from_defaults(tmp_path: Path) -> None:
    """Partial `agent:` blocks fall back to AgentConfig defaults for missing fields."""
    p = _write_yaml(
        tmp_path,
        {
            "pool": _MINIMAL_POOL,
            "agent": {"max_steps": 42},
        },
    )
    cfg = Config.from_yaml(p)
    assert cfg.agent.max_steps == 42
    # `workspace_dir` default is `None` — CLI uses this to decide
    # whether to fall back to cwd when `--workspace` is not given.
    assert cfg.agent.workspace_dir is None
    assert cfg.agent.system_prompt_path == "system_prompt.md"  # default
