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


def test_legacy_single_node_config_still_loads(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        {
            "api_key": "sk-legacy",
            "api_base": "https://api.minimaxi.com",
            "provider": "anthropic",
            "model": "MiniMax-M2.5",
        },
    )
    cfg = Config.from_yaml(p)
    assert len(cfg.llm.pool) == 1
    assert cfg.llm.pool[0].node_id == "default"
    assert cfg.llm.pool[0].api_key == "sk-legacy"
    assert cfg.llm.pool[0].model == "MiniMax-M2.5"


def test_three_explicit_nodes_in_pool(tmp_path: Path) -> None:
    """Main use case: three MiniMax nodes, each with its own key (same value OK)."""
    shared_key = "sk-shared-but-written-three-times"
    p = _write_yaml(
        tmp_path,
        {
            "api_key": shared_key,  # legacy/primary surface
            "api_base": "https://api.minimaxi.com",
            "provider": "anthropic",
            "model": "MiniMax-M2.7",
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
    """We deliberately don't inherit api_key from the top level: each node must be explicit."""
    p = _write_yaml(
        tmp_path,
        {
            "api_key": "sk-top-level",  # present but should NOT propagate into pool entries
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
            "api_key": "sk-fallback",
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
            "api_key": "YOUR_API_KEY_HERE",
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
    assert cfg.llm.routing.failure_threshold == 3


def test_pool_routing_overrides_honored(tmp_path: Path) -> None:
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
    cfg = Config.from_yaml(p)
    assert cfg.llm.routing.failure_threshold == 5


def test_real_config_yaml_loads_with_three_nodes() -> None:
    """End-to-end: the working config.yaml now exposes a 3-node MiniMax pool."""
    project_root = Path(__file__).resolve().parent.parent
    cfg = Config.from_yaml(project_root / "mini_agent" / "config" / "config.yaml")
    assert len(cfg.llm.pool) == 3
    node_ids = [n.node_id for n in cfg.llm.pool]
    assert node_ids == ["minimax-m27", "minimax-m25", "minimax-m21"]
    # Every node declares its own key — we do NOT rely on inheritance.
    for node in cfg.llm.pool:
        assert node.api_key, f"node {node.node_id} missing api_key"
        assert node.api_base, f"node {node.node_id} missing api_base"
        assert node.provider == "anthropic"
