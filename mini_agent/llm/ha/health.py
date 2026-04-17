"""Passive health tracking for model nodes.

Phase 1 keeps this intentionally minimal: we count consecutive failures and
successes, stamp the most recent events, and expose a single `is_healthy`
flag driven by a failure threshold. Circuit-breaker state machines,
cooldowns, and half-open probing belong to Phase 2.
"""

from __future__ import annotations

import threading
import time

from .errors import ErrorCategory
from .models import NodeHealthSnapshot


class NodeHealth:
    """Mutable, thread-safe health state for a single node."""

    def __init__(self, node_id: str, failure_threshold: int = 3) -> None:
        self.node_id = node_id
        self._failure_threshold = max(1, failure_threshold)
        self._lock = threading.Lock()

        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.total_failures = 0
        self.total_successes = 0
        self.last_failure_at: float | None = None
        self.last_success_at: float | None = None
        self.last_error_category: ErrorCategory | None = None
        self.last_error_message: str | None = None

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            self.total_successes += 1
            self.last_success_at = time.time()
            self.last_error_category = None
            self.last_error_message = None

    def record_failure(self, category: ErrorCategory, exc: Exception) -> None:
        with self._lock:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            self.total_failures += 1
            self.last_failure_at = time.time()
            self.last_error_category = category
            # Truncate so noisy stack-like messages don't bloat logs.
            self.last_error_message = f"{type(exc).__name__}: {exc}"[:200]

    @property
    def is_healthy(self) -> bool:
        return self.consecutive_failures < self._failure_threshold

    def snapshot(self) -> NodeHealthSnapshot:
        with self._lock:
            return NodeHealthSnapshot(
                node_id=self.node_id,
                consecutive_failures=self.consecutive_failures,
                consecutive_successes=self.consecutive_successes,
                total_failures=self.total_failures,
                total_successes=self.total_successes,
                last_failure_at=self.last_failure_at,
                last_success_at=self.last_success_at,
                last_error_category=self.last_error_category.value if self.last_error_category else None,
                last_error_message=self.last_error_message,
                is_healthy=self.consecutive_failures < self._failure_threshold,
            )


class HealthRegistry:
    """Lazy registry of NodeHealth instances keyed by node_id."""

    def __init__(self, failure_threshold: int = 3) -> None:
        self._failure_threshold = failure_threshold
        self._healths: dict[str, NodeHealth] = {}
        self._lock = threading.Lock()

    def get(self, node_id: str) -> NodeHealth:
        with self._lock:
            health = self._healths.get(node_id)
            if health is None:
                health = NodeHealth(node_id, self._failure_threshold)
                self._healths[node_id] = health
            return health

    def snapshots(self) -> dict[str, NodeHealthSnapshot]:
        with self._lock:
            return {nid: h.snapshot() for nid, h in self._healths.items()}
