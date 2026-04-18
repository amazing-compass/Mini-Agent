"""High-availability layer for the LLM access path.

Phase 3 delivers the full model-access governance surface with Agent
directly holding `ModelRouter` (no more `LLMClient` facade) and
optional cross-protocol-family failover:

- Model node pool with priority-based failover
- 3-state circuit breaker (closed / open / half-open) via SimpleBreaker
- TokenBudget + three-bucket routing (healthy×fits / healthy-no-fit /
  unhealthy) so context-overflow never gets mistaken for node failure
- Typed error hierarchy so retry/router don't see SDK exceptions
- Router.internal_call bypass for agent-internal LLM calls (L4 summary)
- Cross-family failover (opt-in via `routing.cross_family_fallback`):
  drops `thinking` and cleans up orphan `tool_use` blocks before
  handing `messages` to a different protocol family.

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
from .factory import MINIMAX_DOMAINS, build_client_factory, normalize_api_base
from .health import NodeHealth, SimpleBreaker

# `HealthRegistry` remains importable from `.health` for any code that
# constructed it directly during Phase 1, but is intentionally NOT
# re-exported: design §13.7 step 7 makes `SimpleBreaker` the only
# blessed name at the package boundary.
from .models import ModelNode, NodeHealthSnapshot, RoutingDecision
from .pool import ModelPool
from .router import ModelRouter

__all__ = [
    "AllNodesFailedError",
    "AuthError",
    "BadRequestError",
    "ContextOverflowError",
    "ErrorCategory",
    "LLMError",
    "MINIMAX_DOMAINS",
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
    "build_client_factory",
    "classify_error",
    "is_node_switchable",
    "is_retryable",
    "normalize_api_base",
    "normalize_sdk_error",
]
