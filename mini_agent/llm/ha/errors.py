"""Custom exceptions + error classification for the HA layer.

Phase 2 introduces explicit exception types for every error class that
matters to routing. Provider clients are responsible for translating
vendor-specific SDK exceptions into these types *inside their
`_make_api_request` methods* — the router and retry layer only ever see
these neutral types.

The `ErrorCategory` enum / `classify_error` helpers are kept as a
fallback (and for logging / snapshot output) so pre-Phase-2 callers or
SDK errors that escape the normalizer still classify sensibly.
"""

from __future__ import annotations

from enum import Enum

from ...retry import RetryExhaustedError


# ---------------------------------------------------------------------------
# Custom exception hierarchy — raised by provider clients after SDK errors
# are normalized. Router / retry speak only in these terms.
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Root of the neutral exception hierarchy."""


class TransientError(LLMError):
    """Network hiccup, timeout, or 5xx — worth retrying briefly."""


class RateLimitError(LLMError):
    """429 / explicit rate-limit signal — skip in-node retry, fail over."""


class AuthError(LLMError):
    """401 / 403 — credentials are bad for THIS node; fail over, record failure."""


class NodeUnavailableError(LLMError):
    """404 / 405 — endpoint or model missing on THIS node. Switchable."""


class BadRequestError(LLMError):
    """400 schema / malformed request — program bug, do NOT fail over."""


class ContextOverflowError(LLMError):
    """Context window exceeded. Caller (agent) must compress, not fail over."""


# ---------------------------------------------------------------------------
# Routing-level errors
# ---------------------------------------------------------------------------


class NoAvailableNodeError(LLMError):
    """The pool yielded no candidates (all disabled, nothing enabled, etc.)."""


class AllNodesFailedError(LLMError):
    """Every healthy + fitting candidate was tried and failed for real reasons."""

    def __init__(
        self,
        message: str = "",
        attempts: list[tuple[str, "ErrorCategory", Exception]] | None = None,
    ) -> None:
        self.attempts = list(attempts or [])
        last = self.attempts[-1] if self.attempts else None
        self.last_exception: Exception | None = last[2] if last else None
        self.last_category: ErrorCategory | None = last[1] if last else None

        if not message:
            summary = ", ".join(f"{nid}({cat.value})" for nid, cat, _ in self.attempts) or "<none>"
            last_msg = f"{type(last[2]).__name__}: {last[2]}" if last else "unknown"
            message = f"All {len(self.attempts)} candidate(s) exhausted. Attempts: [{summary}]. Last error: {last_msg}"
        super().__init__(message)


# Backward-compat alias — Phase 1 code imports this name.
PoolExhaustedError = AllNodesFailedError


# ---------------------------------------------------------------------------
# Legacy category system — still useful for logging / health snapshots
# ---------------------------------------------------------------------------


class ErrorCategory(str, Enum):
    """Outcome categories for an LLM call failure."""

    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    NODE_UNAVAILABLE = "node_unavailable"
    REQUEST_MALFORMED = "request_malformed"
    CAPACITY = "capacity"
    UNKNOWN = "unknown"


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
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


CAPACITY_HINTS = (
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


def _looks_like_capacity(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in CAPACITY_HINTS)


def classify_error(exc: Exception) -> ErrorCategory:
    """Classify an exception — custom types first, then SDK duck-typing.

    Once provider clients normalize their SDK errors to our custom types
    (Phase 2), this mostly short-circuits on the first isinstance check.
    The duck-typed fallback remains so escaped SDK errors still land in a
    sensible bucket.
    """
    exc = _unwrap(exc)

    # Phase 2: custom exception types classify directly.
    if isinstance(exc, ContextOverflowError):
        return ErrorCategory.CAPACITY
    if isinstance(exc, BadRequestError):
        return ErrorCategory.REQUEST_MALFORMED
    if isinstance(exc, AuthError):
        return ErrorCategory.AUTH
    if isinstance(exc, RateLimitError):
        return ErrorCategory.RATE_LIMIT
    if isinstance(exc, NodeUnavailableError):
        return ErrorCategory.NODE_UNAVAILABLE
    if isinstance(exc, TransientError):
        return ErrorCategory.TRANSIENT

    # Fallback: SDK-style duck typing.
    name = type(exc).__name__.lower()
    message = str(exc)
    status = _status_code_of(exc)

    if status is not None:
        if status in (401, 403):
            return ErrorCategory.AUTH
        if status == 429:
            return ErrorCategory.RATE_LIMIT
        if status == 400:
            return ErrorCategory.CAPACITY if _looks_like_capacity(message) else ErrorCategory.REQUEST_MALFORMED
        if status == 408:
            return ErrorCategory.TRANSIENT
        if status in (404, 405):
            return ErrorCategory.NODE_UNAVAILABLE
        if status >= 500:
            return ErrorCategory.TRANSIENT
        if 400 <= status < 500:
            return ErrorCategory.REQUEST_MALFORMED

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

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return ErrorCategory.TRANSIENT

    return ErrorCategory.UNKNOWN


def normalize_sdk_error(exc: Exception) -> Exception:
    """Translate an SDK exception into the nearest custom exception.

    Provider clients call this inside their `_make_api_request` method so
    the rest of the system only deals with `TransientError`,
    `RateLimitError`, etc. If `exc` is already one of our types it's
    returned unchanged.
    """
    if isinstance(exc, LLMError):
        return exc

    category = classify_error(exc)
    message = f"{type(exc).__name__}: {exc}"

    mapping = {
        ErrorCategory.TRANSIENT: TransientError,
        ErrorCategory.RATE_LIMIT: RateLimitError,
        ErrorCategory.AUTH: AuthError,
        ErrorCategory.NODE_UNAVAILABLE: NodeUnavailableError,
        ErrorCategory.REQUEST_MALFORMED: BadRequestError,
        ErrorCategory.CAPACITY: ContextOverflowError,
    }
    cls = mapping.get(category)
    if cls is None:
        # UNKNOWN: treat conservatively as transient so retry/failover
        # still gets a chance; the original exception rides along as cause.
        cls = TransientError
    new_exc = cls(message)
    new_exc.__cause__ = exc
    return new_exc


def is_retryable(category: ErrorCategory) -> bool:
    """In-node retry eligibility (category-based, for logging/fallback)."""
    return category in (ErrorCategory.TRANSIENT, ErrorCategory.UNKNOWN)


def is_node_switchable(category: ErrorCategory) -> bool:
    """Whether failing over to another node is appropriate.

    Phase 2 note: `CAPACITY` now produces `ContextOverflowError` which
    the router handles on its own path (three-bucket classification +
    direct raise); `is_node_switchable` still reports False for it,
    meaning "do not silently failover". `REQUEST_MALFORMED` is also
    non-switchable (program bug).
    """
    return category in (
        ErrorCategory.TRANSIENT,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.AUTH,
        ErrorCategory.NODE_UNAVAILABLE,
        ErrorCategory.UNKNOWN,
    )
