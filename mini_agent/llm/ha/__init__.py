"""High-availability layer for the LLM access path.

Phase 1 scope:
- Model node pool with priority-based failover
- Passive health tracking (no circuit breaker yet)
- Error classification to drive routing decisions

See docs/IMPROVEMENT_02_MODEL_FALLBACK_HA_CN.md for the full design.
"""

from .errors import ErrorCategory, PoolExhaustedError, classify_error, is_node_switchable, is_retryable
from .health import HealthRegistry, NodeHealth
from .models import ModelNode, NodeHealthSnapshot, RoutingDecision
from .pool import ModelPool
from .router import ModelRouter

__all__ = [
    "ErrorCategory",
    "HealthRegistry",
    "ModelNode",
    "ModelPool",
    "ModelRouter",
    "NodeHealth",
    "NodeHealthSnapshot",
    "PoolExhaustedError",
    "RoutingDecision",
    "classify_error",
    "is_node_switchable",
    "is_retryable",
]
