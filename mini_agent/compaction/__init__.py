"""Cache-aware compaction package (IMPROVEMENT_04).

Exposes the data classes and policy types used by the Agent's
compaction integration. Everything here is intentionally LLM-free —
``CompactionPolicy.decide`` is a pure function over a snapshot, and
``CachePolicy.pricing_for_node`` is a static lookup.
"""

from .cache_policy import CachePolicy
from .models import CompactionDecision, CompactionSnapshot, ModelPricing
from .policy import CompactionPolicy

__all__ = [
    "CachePolicy",
    "CompactionDecision",
    "CompactionPolicy",
    "CompactionSnapshot",
    "ModelPricing",
]
