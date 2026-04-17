"""Error classification for routing decisions.

The classifier is duck-typed so it works for both the Anthropic and OpenAI
SDKs without importing them here. It inspects the exception's class name,
any `status_code`/`code` attributes, and falls back to UNKNOWN.
"""

from __future__ import annotations

from enum import Enum

from ...retry import RetryExhaustedError


class ErrorCategory(str, Enum):
    """Outcome categories for an LLM call failure."""

    TRANSIENT = "transient"  # timeouts, 5xx, connection errors — retry + failover OK
    RATE_LIMIT = "rate_limit"  # 429 / capacity — failover OK, briefly deprioritize node
    AUTH = "auth"  # 401/403 — do NOT retry node; mark node unhealthy
    NODE_UNAVAILABLE = "node_unavailable"  # 404/405 — endpoint or model missing on THIS node
    REQUEST_MALFORMED = "request_malformed"  # 400 schema / tool format — program bug, don't mask
    CAPACITY = "capacity"  # context-overflow style 400 — upstream concern
    UNKNOWN = "unknown"  # conservative default: retry + failover


class PoolExhaustedError(Exception):
    """Raised by the router when every candidate node has failed."""

    def __init__(self, attempts: list[tuple[str, "ErrorCategory", Exception]]):
        self.attempts = attempts
        last = attempts[-1] if attempts else None
        self.last_exception: Exception | None = last[2] if last else None
        self.last_category: ErrorCategory | None = last[1] if last else None

        summary = ", ".join(f"{nid}({cat.value})" for nid, cat, _ in attempts) or "<none>"
        last_msg = f"{type(last[2]).__name__}: {last[2]}" if last else "unknown"
        super().__init__(
            f"All {len(attempts)} model node(s) exhausted. Attempts: [{summary}]. Last error: {last_msg}"
        )


def _unwrap(exc: Exception) -> Exception:
    """Peel off known wrappers so classification sees the underlying error."""
    if isinstance(exc, RetryExhaustedError) and exc.last_exception is not None:
        return exc.last_exception
    return exc


def _status_code_of(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from SDK errors."""
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    # anthropic SDK exposes .response.status_code on APIStatusError
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _looks_like_capacity(message: str) -> bool:
    """Heuristic: does the error message smell like a context/token overflow?"""
    lowered = message.lower()
    capacity_hints = (
        "context length",
        "context_length",
        "maximum context",
        "context window",
        "too many tokens",
        "token limit",
        "prompt is too long",
        "maximum tokens",
        "max_tokens",
    )
    return any(hint in lowered for hint in capacity_hints)


def classify_error(exc: Exception) -> ErrorCategory:
    """Classify an exception raised by a provider client.

    Duck-typed: works for anthropic.* and openai.* error classes without
    importing either SDK.
    """
    exc = _unwrap(exc)
    name = type(exc).__name__.lower()
    message = str(exc)
    status = _status_code_of(exc)

    # Explicit status codes take precedence.
    if status is not None:
        if status in (401, 403):
            return ErrorCategory.AUTH
        if status == 429:
            return ErrorCategory.RATE_LIMIT
        if status == 400:
            return ErrorCategory.CAPACITY if _looks_like_capacity(message) else ErrorCategory.REQUEST_MALFORMED
        if status == 408:
            return ErrorCategory.TRANSIENT
        # 404/405 are node-local configuration problems (wrong api_base, model
        # not deployed on this account, stale endpoint). Another pool node
        # with a different endpoint/model can still serve the request, so
        # these must be switchable — NOT bundled with 400 malformed-request.
        if status in (404, 405):
            return ErrorCategory.NODE_UNAVAILABLE
        if status >= 500:
            return ErrorCategory.TRANSIENT
        if 400 <= status < 500:
            return ErrorCategory.REQUEST_MALFORMED

    # Class-name heuristics (fallback when status is unavailable).
    if "authentication" in name or "permissiondenied" in name or "forbidden" in name:
        return ErrorCategory.AUTH
    if "ratelimit" in name:
        return ErrorCategory.RATE_LIMIT
    if "badrequest" in name or "invalidrequest" in name or "unprocessable" in name:
        return ErrorCategory.CAPACITY if _looks_like_capacity(message) else ErrorCategory.REQUEST_MALFORMED
    if (
        "timeout" in name
        or "connection" in name
        or "apiconnection" in name
        or "serviceunavailable" in name
        or "internalservererror" in name
        or "apierror" in name
    ):
        return ErrorCategory.TRANSIENT

    # Built-in networking errors.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return ErrorCategory.TRANSIENT

    return ErrorCategory.UNKNOWN


def is_retryable(category: ErrorCategory) -> bool:
    """In-node retry eligibility.

    AUTH, NODE_UNAVAILABLE, and REQUEST_MALFORMED are terminal for the
    current node — retrying the same call on the same node will fail the
    same way, so we skip backoff entirely and let the router fail over
    (when the error is also switchable).
    """
    return category in (ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT, ErrorCategory.UNKNOWN)


def is_node_switchable(category: ErrorCategory) -> bool:
    """Whether failing over to another node is appropriate.

    REQUEST_MALFORMED is not switchable: if our request is wrong, no other
    node will make it right — switching masks the bug (see doc §11 risk 2).

    CAPACITY is not switchable in Phase 1: without capability-aware routing
    we'd just cycle through nodes with similar windows and burn quota. The
    caller should compress context first (doc §6.3). Capability-aware
    capacity failover is Phase 3.

    NODE_UNAVAILABLE (404/405) IS switchable — it's the whole point of
    having a pool: a node whose endpoint/model is broken for this account
    should hand off to the next.
    """
    return category in (
        ErrorCategory.TRANSIENT,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.AUTH,
        ErrorCategory.NODE_UNAVAILABLE,
        ErrorCategory.UNKNOWN,
    )
