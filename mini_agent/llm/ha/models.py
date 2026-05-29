# ✅
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
    # Cache capability flags (IMPROVEMENT_04). Both default to False so
    # unknown providers are treated conservatively. DeepSeek nodes set
    # explicit=False + automatic=True; Anthropic-official sets
    # explicit=True; MiniMax / fallback stay at the safe default.
    supports_explicit_cache_control: bool = False
    supports_automatic_context_cache: bool = False


class NodeHealthSnapshot(BaseModel):
    """Immutable snapshot of a node's health state, safe to log or return."""

    node_id: str
    consecutive_failures: int = 0   # 连续失败次数，达到阈值时熔断器打开
    consecutive_successes: int = 0  # 连续成功次数
    total_failures: int = 0      #生命周期累计值，用于长期监控
    total_successes: int = 0     
    last_failure_at: float | None = None  # Unix 时间戳，方便计算距上次失败多久
    last_success_at: float | None = None
    last_error_category: str | None = None   # 最后一次失败的分类和消息，调试用
    last_error_message: str | None = None
    is_healthy: bool = True
    # Phase 2 circuit-breaker fields (closed / open / half-open).
    circuit_state: str = "closed"        # 三态
    cooldown_until: float | None = None   # OPEN 状态下的冷却截止时间戳，过了这个时间才允许探测


class RoutingDecision(BaseModel):
    """Explains why a given node was selected (or why all candidates failed)."""

    selected_node_id: str | None     # 最终成功的节点 ID
    candidate_node_ids: list[str] = Field(default_factory=list)   # 本次参与竞选的所有节点 ID 列表
    fallback_level: int = 0     # 0 = 第一优先级节点直接成功；1 = failover 到第二个，以此类推
    reason: str = ""    # 人类可读的说明，如 "success on primary node 'minimax-primary'"
