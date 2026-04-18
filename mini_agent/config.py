"""Configuration management module

Provides unified configuration loading and management functionality.

Phase 1 introduces a model node pool alongside the legacy single-node
config. Both shapes are accepted:

Legacy (single node, flat fields under top level):
    api_key: ...
    api_base: ...
    model: ...
    provider: ...

Pool (nested under `llm:` or mixed with top level):
    llm:
      routing:
        strategy: priority
      retry: {...}
      pool:
        - node_id: minimax-primary
          provider: anthropic
          api_key_env: MINIMAX_API_KEY
          api_base: https://api.minimax.io
          model: MiniMax-M2.5
          priority: 100
        - node_id: openai-backup
          provider: openai
          api_key_env: OPENAI_API_KEY
          api_base: https://api.openai.com/v1
          model: gpt-5
          priority: 80
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RetryConfig(BaseModel):
    """Retry configuration"""

    enabled: bool = True
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0


class RoutingConfig(BaseModel):
    """Pool-level routing configuration.

    Phase 2 moved `failure_threshold` into `BreakerConfig` because the
    breaker is the authoritative owner of node health state (design
    §5.8). Keeping the field here would be a dead option that silently
    changes semantics for anyone still setting it.

    Phase 3 adds `cross_family_fallback`: when True, after every node in
    the current protocol family has failed the router bridges `messages`
    to a node from the other family (stripping `thinking` blocks and
    cleaning up orphan `tool_use` references first). Default False keeps
    Phase 2's same-family-only behavior. Design §6.
    """

    strategy: str = "priority"  # Phase 1 only implements priority
    cross_family_fallback: bool = False  # Phase 3: opt-in cross-family hop


class BreakerConfig(BaseModel):
    """3-state circuit-breaker settings (Phase 2)."""

    failure_threshold: int = 3  # consecutive failures → closed → open
    cooldown_seconds: float = 60.0  # dead time before open → half-open probe


class ModelNodeConfig(BaseModel):
    """One endpoint in the model pool."""

    node_id: str
    provider: str = "anthropic"  # "anthropic" or "openai"
    protocol_family: str | None = None  # defaults to provider if unspecified
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str = "https://api.minimax.io"
    model: str = "MiniMax-M2.5"
    priority: int = 100
    weight: int = 10
    context_window: int = 128000
    max_output_tokens: int = 8192
    supports_tools: bool = True
    supports_thinking: bool = True
    enabled: bool = True


class LLMConfig(BaseModel):
    """LLM configuration — Phase 3 is pool-only.

    The deleted `LLMClient` facade used to surface the primary node's
    credentials as top-level `api_key` / `api_base` / `model` /
    `provider` attributes; those are gone now. Everyone reads from
    `pool` directly.
    """

    retry: RetryConfig = Field(default_factory=RetryConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    pool: list[ModelNodeConfig] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Agent configuration.

    `workspace_dir` defaults to `None` (not `"./workspace"`) so callers —
    the CLI in particular — can distinguish "user didn't configure one"
    from "user explicitly chose ./workspace". When unset, the CLI falls
    back to the current working directory, preserving the pre-Phase-3
    default experience for users who never touched the config.
    """

    max_steps: int = 50
    workspace_dir: str | None = None
    system_prompt_path: str = "system_prompt.md"


class MCPConfig(BaseModel):
    """MCP (Model Context Protocol) timeout configuration"""

    connect_timeout: float = 10.0  # Connection timeout (seconds)
    execute_timeout: float = 60.0  # Tool execution timeout (seconds)
    sse_read_timeout: float = 120.0  # SSE read timeout (seconds)


class ToolsConfig(BaseModel):
    """Tools configuration"""

    # Basic tools (file operations, bash)
    enable_file_tools: bool = True
    enable_bash: bool = True
    enable_note: bool = True

    # Skills
    enable_skills: bool = True
    skills_dir: str = "./skills"

    # MCP tools
    enable_mcp: bool = True
    mcp_config_path: str = "mcp.json"
    mcp: MCPConfig = Field(default_factory=MCPConfig)


def _resolve_api_key(raw: Any, env_var: str | None, placeholder_ok: bool = False) -> str:
    """Resolve an api_key from either a literal value or an env var reference."""
    if env_var:
        value = os.environ.get(env_var)
        if not value:
            raise ValueError(f"Environment variable {env_var!r} is not set or empty")
        return value
    if raw is None:
        raise ValueError("api_key is required (provide `api_key` or `api_key_env`)")
    value = str(raw)
    if not placeholder_ok and value == "YOUR_API_KEY_HERE":
        raise ValueError("Please configure a valid API Key")
    return value


def _build_retry_config(data: dict[str, Any]) -> RetryConfig:
    return RetryConfig(
        enabled=data.get("enabled", True),
        max_retries=data.get("max_retries", 3),
        initial_delay=data.get("initial_delay", 1.0),
        max_delay=data.get("max_delay", 60.0),
        exponential_base=data.get("exponential_base", 2.0),
    )


def _build_routing_config(data: dict[str, Any]) -> RoutingConfig:
    if "failure_threshold" in data:
        # Loud failure instead of silent no-op. Design §5.8: breaker owns
        # failure_threshold exclusively; leaving the knob here would let
        # someone set it and wonder why nothing changed.
        raise ValueError(
            "`routing.failure_threshold` is no longer accepted — move it to "
            "`breaker.failure_threshold` (see config-example.yaml)."
        )
    return RoutingConfig(
        strategy=data.get("strategy", "priority"),
        cross_family_fallback=bool(data.get("cross_family_fallback", False)),
    )


def _build_breaker_config(data: dict[str, Any]) -> BreakerConfig:
    return BreakerConfig(
        failure_threshold=int(data.get("failure_threshold", 3)),
        cooldown_seconds=float(data.get("cooldown_seconds", 60.0)),
    )


def _build_pool_entries(pool_raw: list[dict[str, Any]]) -> list[ModelNodeConfig]:
    """Validate + materialize pool entries (api_key resolution happens lazily later).

    Each pool entry stands on its own — credentials, base URL, and provider
    must be declared per-node. That mirrors real heterogeneous pools where
    every provider/account has its own key; it also prevents silent
    misrouting when a future node legitimately needs a different key.
    """
    if not pool_raw:
        return []
    seen_ids: set[str] = set()
    entries: list[ModelNodeConfig] = []
    for idx, raw in enumerate(pool_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"pool[{idx}] must be a mapping, got {type(raw).__name__}")
        node_id = raw.get("node_id") or f"node-{idx}"
        if node_id in seen_ids:
            raise ValueError(f"Duplicate node_id in pool: {node_id!r}")
        seen_ids.add(node_id)
        entry = ModelNodeConfig(
            node_id=node_id,
            provider=raw.get("provider", "anthropic"),
            protocol_family=raw.get("protocol_family"),
            api_key=raw.get("api_key"),
            api_key_env=raw.get("api_key_env"),
            api_base=raw.get("api_base", "https://api.minimax.io"),
            model=raw.get("model", "MiniMax-M2.5"),
            priority=int(raw.get("priority", 100)),
            weight=int(raw.get("weight", 10)),
            context_window=int(raw.get("context_window", 128000)),
            max_output_tokens=int(raw.get("max_output_tokens", 8192)),
            supports_tools=bool(raw.get("supports_tools", True)),
            supports_thinking=bool(raw.get("supports_thinking", True)),
            enabled=bool(raw.get("enabled", True)),
        )
        entries.append(entry)
    return entries


def _resolve_pool_keys(pool: list[ModelNodeConfig]) -> list[ModelNodeConfig]:
    """Materialize api_keys for every pool entry, erroring on missing/placeholder values."""
    resolved: list[ModelNodeConfig] = []
    for entry in pool:
        api_key = _resolve_api_key(entry.api_key, entry.api_key_env)
        resolved_entry = entry.model_copy(
            update={
                "api_key": api_key,
                "api_key_env": None,
                "protocol_family": entry.protocol_family or entry.provider,
            }
        )
        resolved.append(resolved_entry)
    return resolved


class Config(BaseModel):
    """Main configuration class"""

    llm: LLMConfig
    agent: AgentConfig
    tools: ToolsConfig

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from the default search path."""
        config_path = cls.get_default_config_path()
        if not config_path.exists():
            raise FileNotFoundError("Configuration file not found. Run scripts/setup-config.sh or place config.yaml in mini_agent/config/.")
        return cls.from_yaml(config_path)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Config":
        """Load configuration from YAML file.

        Phase 3 requires the pool shape (`pool: [...]`). The legacy flat
        shape (top-level `api_key` / `api_base` / `model` / `provider`)
        was removed along with the `LLMClient` facade — see design
        §13.7 step 8 and §10 "不做向后兼容".
        """
        config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file does not exist: {config_path}")

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError("Configuration file is empty")

        llm_section: dict[str, Any] = data.get("llm") if isinstance(data.get("llm"), dict) else {}

        # Reject legacy flat fields loudly. Design §13.7 step 8 retires
        # the "synthesize a one-node pool from api_key/api_base/model"
        # code path; keeping it silently working would drift from the
        # authoritative `pool:` shape the rest of the codebase expects.
        _LEGACY_FLAT_FIELDS = ("api_key", "api_base", "model", "provider", "api_key_env")
        legacy_hits = [f for f in _LEGACY_FLAT_FIELDS if f in data]
        if legacy_hits:
            raise ValueError(
                "Top-level LLM fields are no longer supported — migrate to a "
                f"`pool: [...]` block. Offending fields: {sorted(legacy_hits)}. "
                "See config-example.yaml for the new shape."
            )

        # Pool can live under `llm.pool` or top-level `pool` (YAML flexibility).
        pool_raw = llm_section.get("pool") if llm_section else None
        if pool_raw is None:
            pool_raw = data.get("pool")

        # Retry / routing / breaker blocks: also accept either nested or top-level.
        retry_raw = (llm_section.get("retry") if llm_section else None) or data.get("retry") or {}
        routing_raw = (llm_section.get("routing") if llm_section else None) or data.get("routing") or {}
        breaker_raw = (llm_section.get("breaker") if llm_section else None) or data.get("breaker") or {}

        retry_config = _build_retry_config(retry_raw)
        routing_config = _build_routing_config(routing_raw)
        breaker_config = _build_breaker_config(breaker_raw)

        if not pool_raw:
            raise ValueError(
                "Configuration file missing `pool: [...]`. Phase 3 is pool-only; "
                "see config-example.yaml."
            )

        pool_entries = _build_pool_entries(pool_raw)
        pool_entries = _resolve_pool_keys(pool_entries)

        llm_config = LLMConfig(
            retry=retry_config,
            routing=routing_config,
            breaker=breaker_config,
            pool=pool_entries,
        )

        # Parse Agent configuration.
        #
        # Blessed form is a nested `agent:` block (matches the dataclass
        # shape callers use at runtime via `config.agent.max_steps`).
        # Top-level `max_steps` / `workspace_dir` / `system_prompt_path`
        # stay as a legacy shim — same leniency we apply to retry /
        # routing / breaker above — but when BOTH forms appear the
        # nested block wins so users get what they wrote.
        agent_raw: dict[str, Any] = data.get("agent") if isinstance(data.get("agent"), dict) else {}
        defaults = AgentConfig()
        agent_config = AgentConfig(
            max_steps=agent_raw.get(
                "max_steps", data.get("max_steps", defaults.max_steps)
            ),
            workspace_dir=agent_raw.get(
                "workspace_dir", data.get("workspace_dir", defaults.workspace_dir)
            ),
            system_prompt_path=agent_raw.get(
                "system_prompt_path",
                data.get("system_prompt_path", defaults.system_prompt_path),
            ),
        )

        # Parse tools configuration
        tools_data = data.get("tools", {})

        # Parse MCP configuration
        mcp_data = tools_data.get("mcp", {})
        mcp_config = MCPConfig(
            connect_timeout=mcp_data.get("connect_timeout", 10.0),
            execute_timeout=mcp_data.get("execute_timeout", 60.0),
            sse_read_timeout=mcp_data.get("sse_read_timeout", 120.0),
        )

        tools_config = ToolsConfig(
            enable_file_tools=tools_data.get("enable_file_tools", True),
            enable_bash=tools_data.get("enable_bash", True),
            enable_note=tools_data.get("enable_note", True),
            enable_skills=tools_data.get("enable_skills", True),
            skills_dir=tools_data.get("skills_dir", "./skills"),
            enable_mcp=tools_data.get("enable_mcp", True),
            mcp_config_path=tools_data.get("mcp_config_path", "mcp.json"),
            mcp=mcp_config,
        )

        return cls(
            llm=llm_config,
            agent=agent_config,
            tools=tools_config,
        )

    @staticmethod
    def get_package_dir() -> Path:
        """Get the package installation directory

        Returns:
            Path to the mini_agent package directory
        """
        # Get the directory where this config.py file is located
        return Path(__file__).parent

    @classmethod
    def find_config_file(cls, filename: str) -> Path | None:
        """Find configuration file with priority order

        Search for config file in the following order of priority:
        1) mini_agent/config/{filename} in current directory (development mode)
        2) ~/.mini-agent/config/{filename} in user home directory
        3) {package}/mini_agent/config/{filename} in package installation directory

        Args:
            filename: Configuration file name (e.g., "config.yaml", "mcp.json", "system_prompt.md")

        Returns:
            Path to found config file, or None if not found
        """
        # Priority 1: Development mode - current directory's config/ subdirectory
        dev_config = Path.cwd() / "mini_agent" / "config" / filename
        if dev_config.exists():
            return dev_config

        # Priority 2: User config directory
        user_config = Path.home() / ".mini-agent" / "config" / filename
        if user_config.exists():
            return user_config

        # Priority 3: Package installation directory's config/ subdirectory
        package_config = cls.get_package_dir() / "config" / filename
        if package_config.exists():
            return package_config

        return None

    @classmethod
    def get_default_config_path(cls) -> Path:
        """Get the default config file path with priority search

        Returns:
            Path to config.yaml (prioritizes: dev config/ > user config/ > package config/)
        """
        config_path = cls.find_config_file("config.yaml")
        if config_path:
            return config_path

        # Fallback to package config directory for error message purposes
        return cls.get_package_dir() / "config" / "config.yaml"
