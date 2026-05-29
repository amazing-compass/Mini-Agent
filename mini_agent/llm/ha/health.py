# ✅
"""Passive health tracking + 3-state circuit breaker for model nodes.

Phase 2 extends the Phase 1 minimal NodeHealth (consecutive failures +
is_healthy boolean) into a full closed / open / half-open state machine
with cooldown, as described in design §3 and §5.8.

Three interfaces follow the command-query-separation pattern (§3.4):
    * `is_passable(node_id)`  — pure read; "may business traffic pass now"
      (closed OR half-open OR open-with-cooldown-elapsed). Used by
      `Router.call`'s pre-scan bucket classification.
    * `is_serving(node_id)`   — pure read; "is the node actually serving
      right now" (closed OR half-open only). Stricter; used by
      `Router.internal_call` (the cache-aligned summary bypass) so it
      never burns a probe slot.
    * `on_attempt(node_id)`   — write; the only place that performs the
      open → half-open transition. Business main loop calls it
      immediately before `client.generate(...)`.

`record_success` / `record_failure` drive closed → open and half-open →
closed transitions. `HealthRegistry` remains the Phase 1 alias for
backward compatibility (the old consecutive-failure health check is
preserved as `is_healthy`).
"""

from __future__ import annotations

import threading
import time

from .errors import ErrorCategory
from .models import NodeHealthSnapshot


CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half-open"


class NodeHealth:
    """Mutable, thread-safe health state for a single node.

    Owns the three circuit-breaker fields (`circuit_state`,
    `cooldown_until`, `consecutive_failures`) plus the informational
    counters used for snapshots/logging.
    """

    def __init__(
        self,
        node_id: str,    # 字节唯一标识
        failure_threshold: int = 3,   # 连续失败几次触发熔断
        cooldown_seconds: float = 60.0,   # OPEN状态冷却时长
    ) -> None:
        self.node_id = node_id
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown_seconds = max(0.0, cooldown_seconds)
        self._lock = threading.Lock()    # 线程锁 -- 所有读写操作都加锁

        self.consecutive_failures = 0    # 连续失败次数
        self.consecutive_successes = 0    # 连续成功次数
        self.total_failures = 0    # 生命周期累计值，用于长期监控
        self.total_successes = 0    # 生命周期累计值，用于长期监控
        self.last_failure_at: float | None = None    # 最后一次失败的时间戳
        self.last_success_at: float | None = None    # 最后一次成功的时间戳
        self.last_error_category: ErrorCategory | None = None    # 最后一次失败的分类
        self.last_error_message: str | None = None    # 最后一次失败的错误消息

        # Phase 2: circuit state machine
        self.circuit_state: str = CLOSED     # 当前熔断器状态 -- 三种：CLOSED, OPEN, HALF_OPEN
        self.cooldown_until: float | None = None    # OPEN状态下的冷却截止时间戳
        # Exclusivity flag: set True by `on_attempt` when the state is
        # pushed into HALF_OPEN so concurrent callers (most notably ACP
        # cross-session traffic) don't flood a still-broken node during
        # its probe window. Cleared by record_success / record_failure.
        self._probe_in_flight: bool = False    # 是否正在探测 -- 如果正在探测，则返回False -- 因为探测时不能通过

    # ---- reads (no side effects) ----
    # 路由层用 -- 断路器是否允许本次请求通过
    def is_passable(self) -> bool:
        """Closed / half-open / open-with-cooldown-elapsed → True.

        Special case: a HALF_OPEN node whose probe is currently in flight
        returns False — the probe slot is reserved for its originator,
        so concurrent callers skip this node until the probe resolves.
        """
        with self._lock:
            if self.circuit_state == CLOSED:
                return True
            if self.circuit_state == HALF_OPEN:
                return not self._probe_in_flight
            # OPEN: cooldown elapsed means "eligible to probe" to the
            # first caller; whoever wins will call on_attempt and lock
            # the probe slot.
            return self.cooldown_until is not None and time.time() >= self.cooldown_until

    # 内部调用（如摘要统计）专用的严格版 -- 只有CLOSE或HALF_OPEN状态才能通过
    def is_serving(self) -> bool:
        """Strict: closed / half-open (no probe in flight). Used by internal_call.

        Internal callers (summary etc.) must NEVER steal a probe slot,
        so an in-flight HALF_OPEN is treated as not-serving.
        """
        with self._lock:
            if self.circuit_state == CLOSED:
                return True
            if self.circuit_state == HALF_OPEN:
                return not self._probe_in_flight
            return False

    # ---- writes (state transitions) ----
    # 真正发出探针请求前调用 -- 将OPEN -> HALF_OPEN 并锁定探针槽
    def on_attempt(self) -> None:    # 业务主循环用 -- 在即将发出真实探测请求时，将状态从OPEN+冷却结束 → HALF_OPEN
        """Transition open+cooldown-elapsed → half-open exactly when the
        business main loop is about to issue a real probe request. No-op
        in other states.

        Claims the probe slot (`_probe_in_flight = True`) so concurrent
        callers that arrive after the transition see `is_passable() =
        False` and skip this node. Cleared when `record_success` or
        `record_failure` completes the probe.
        """
        with self._lock:
            if (
                self.circuit_state == OPEN
                and self.cooldown_until is not None
                and time.time() >= self.cooldown_until
            ):
                self.circuit_state = HALF_OPEN
                self._probe_in_flight = True

    # 记录一次成功
    def record_success(self) -> None:
        with self._lock:
            # A success means the node is working — clear any residual
            # open state. Normal path: `on_attempt` already moved us to
            # half-open before the call. Direct-call / test path may go
            # straight from open → success; treat that as full recovery
            # instead of leaving the breaker stuck open with zero failures.
            if self.circuit_state != CLOSED:
                self.circuit_state = CLOSED
                self.cooldown_until = None
            self.consecutive_failures = 0
            self.consecutive_successes += 1
            self.total_successes += 1
            self.last_success_at = time.time()
            self.last_error_category = None
            self.last_error_message = None
            self._probe_in_flight = False  # probe (if any) completed

    # 记录一次失败
    def record_failure(self, category: ErrorCategory, exc: Exception) -> None:
        with self._lock:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            self.total_failures += 1
            self.last_failure_at = time.time()
            self.last_error_category = category
            self.last_error_message = f"{type(exc).__name__}: {exc}"[:200]

            # half-open probe fails → immediately re-open + refresh cooldown.
            # (Explicit check: don't rely on the threshold to be re-crossed.)
            # closed → open only once the threshold is crossed.
            if self.circuit_state == HALF_OPEN or self.consecutive_failures >= self._failure_threshold:
                self.circuit_state = OPEN
                self.cooldown_until = time.time() + self._cooldown_seconds
            self._probe_in_flight = False  # probe (if any) completed — slot released

    # ---- introspection ----

    @property
    def is_healthy(self) -> bool:
        """Backward-compatible Phase 1 predicate.

        A node is "healthy" when the breaker isn't currently blocking
        traffic. This mirrors `is_passable` — closed / half-open / cooldown-
        elapsed open all count as healthy because the router can pass
        business through them.
        """
        return self.is_passable()

    # 在锁保护下拍一份只读的健康状态快照
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
                is_healthy=self.circuit_state != OPEN
                or (self.cooldown_until is not None and time.time() >= self.cooldown_until),
                circuit_state=self.circuit_state,
                cooldown_until=self.cooldown_until,
            )


class SimpleBreaker:
    """The authoritative owner of `dict[str, NodeHealth]`.

    Provides the three-interface CQS surface described in design §3.4
    plus the usual `record_success` / `record_failure` and snapshots.
    Lazily instantiates `NodeHealth` entries on first touch so there's
    no setup ceremony before the first request.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._failure_threshold = failure_threshold  # 连续失败几次触发熔断
        self._cooldown_seconds = cooldown_seconds     # 冷却时长，透传给每个 NodeHealth
        self._healths: dict[str, NodeHealth] = {}   # 核心：node_id → NodeHealth 的映射
        self._lock = threading.Lock()

    def get(self, node_id: str) -> NodeHealth:
        with self._lock:
            health = self._healths.get(node_id)
            if health is None:
                health = NodeHealth(
                    node_id,
                    failure_threshold=self._failure_threshold,
                    cooldown_seconds=self._cooldown_seconds,
                )
                self._healths[node_id] = health
            return health

    # ---- the three CQS interfaces ----

    def is_passable(self, node_id: str) -> bool:
        return self.get(node_id).is_passable()

    def is_serving(self, node_id: str) -> bool:
        return self.get(node_id).is_serving()

    def on_attempt(self, node_id: str) -> None:
        self.get(node_id).on_attempt()

    # ---- recording ----

    def record_success(self, node_id: str) -> None:
        self.get(node_id).record_success()

    def record_failure(self, node_id: str, category: ErrorCategory, exc: Exception) -> None:
        self.get(node_id).record_failure(category, exc)

    # ---- observation ----

    def snapshots(self) -> dict[str, NodeHealthSnapshot]:
        with self._lock:
            return {nid: h.snapshot() for nid, h in self._healths.items()}


# Backward-compatible alias — Phase 1 code constructs `HealthRegistry(...)`.
# The new class subsumes both roles; old callers get breaker semantics for
# free because `is_healthy` now maps to `is_passable`.
class HealthRegistry(SimpleBreaker):
    """Alias kept for Phase 1 call sites. Prefer `SimpleBreaker`."""

    # Phase 1 call sites construct HealthRegistry(failure_threshold=...)
    # without a cooldown arg. Default to 60s to preserve "unhealthy after
    # threshold stays unhealthy" until recovery — matches the old
    # is_healthy behavior which returned False purely on counter crossing.
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        super().__init__(failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
