"""High-availability layer for the LLM access path.

Phase 2 delivers the full model-access governance surface:
- Model node pool with priority-based failover
- 3-state circuit breaker (closed / open / half-open) via SimpleBreaker
- TokenBudget + three-bucket routing (healthy×fits / healthy-no-fit /
  unhealthy) so context-overflow never gets mistaken for node failure
- Typed error hierarchy so retry/router don't see SDK exceptions
- Router.internal_call bypass for agent-internal LLM calls (L4 summary)

See docs/IMPROVEMENT_02_HA_DESIGN_DECISIONS.md for the full design.
"""

from .budget import TokenBudget
from .errors import (
    AllNodesFailedError,
    AuthError,
    BadRequestError,
    ContextOverflowError,
    ErrorCategory,
    LLMError,
    NoAvailableNodeError,
    NodeUnavailableError,
    PoolExhaustedError,
    RateLimitError,
    TransientError,
    classify_error,
    is_node_switchable,
    is_retryable,
    normalize_sdk_error,
)
from .health import HealthRegistry, NodeHealth, SimpleBreaker
from .models import ModelNode, NodeHealthSnapshot, RoutingDecision
from .pool import ModelPool
from .router import ModelRouter

__all__ = [
    "AllNodesFailedError",
    "AuthError",
    "BadRequestError",
    "ContextOverflowError",
    "ErrorCategory",
    "HealthRegistry",
    "LLMError",
    "ModelNode",
    "ModelPool",
    "ModelRouter",
    "NoAvailableNodeError",
    "NodeHealth",
    "NodeHealthSnapshot",
    "NodeUnavailableError",
    "PoolExhaustedError",
    "RateLimitError",
    "RoutingDecision",
    "SimpleBreaker",
    "TokenBudget",
    "TransientError",
    "classify_error",
    "is_node_switchable",
    "is_retryable",
    "normalize_sdk_error",
]
