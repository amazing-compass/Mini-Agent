"""Data models for the HA layer (model nodes, health snapshots, routing decisions)."""

from pydantic import BaseModel, Field


class ModelNode(BaseModel):
    """A single model endpoint in the pool.

    A node is the combination of (provider + api_base + api_key + model).
    Two nodes with the same model name but different keys/accounts are
    considered distinct nodes.
    """

    node_id: str
    provider: str  # "anthropic" or "openai"
    protocol_family: str  # "anthropic" or "openai" — usually matches provider
    api_key: str
    api_base: str
    model: str
    priority: int = 100
    weight: int = 10
    context_window: int = 128000
    # Upper bound on `max_tokens` the node advertises — Router uses
    # min(node.max_output_tokens, context_window - estimate - margin) to
    # derive the actual per-request budget (see design §7.4).
    max_output_tokens: int = 8192
    supports_tools: bool = True
    supports_thinking: bool = True
    enabled: bool = True


class NodeHealthSnapshot(BaseModel):
    """Immutable snapshot of a node's health state, safe to log or return."""

    node_id: str
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_at: float | None = None  # unix timestamp (seconds)
    last_success_at: float | None = None
    last_error_category: str | None = None
    last_error_message: str | None = None
    is_healthy: bool = True
    # Phase 2 circuit-breaker fields (closed / open / half-open).
    circuit_state: str = "closed"
    cooldown_until: float | None = None


class RoutingDecision(BaseModel):
    """Explains why a given node was selected (or why all candidates failed)."""

    selected_node_id: str | None
    candidate_node_ids: list[str] = Field(default_factory=list)
    fallback_level: int = 0
    reason: str = ""
