"""Tests for the HA error classification layer.

These tests deliberately construct exceptions that *look like* SDK errors
(anthropic / openai) without importing the SDKs themselves — the
classifier is duck-typed and should work on any object with the expected
shape.
"""

from __future__ import annotations

import pytest

from mini_agent.llm.ha.errors import (
    ErrorCategory,
    classify_error,
    is_node_switchable,
    is_retryable,
)
from mini_agent.retry import RetryExhaustedError


class _FakeHTTPError(Exception):
    def __init__(self, status_code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.status_code = status_code


class _FakeAuthenticationError(Exception):
    """Mimics anthropic.AuthenticationError / openai.AuthenticationError."""


class _FakeRateLimitError(Exception):
    """Mimics *.RateLimitError."""


class _FakeBadRequestError(Exception):
    """Mimics *.BadRequestError."""


class _FakeAPITimeoutError(Exception):
    """Mimics *.APITimeoutError."""


class _FakeAPIConnectionError(Exception):
    """Mimics *.APIConnectionError."""


def test_classifies_401_as_auth() -> None:
    assert classify_error(_FakeHTTPError(401)) == ErrorCategory.AUTH


def test_classifies_403_as_auth() -> None:
    assert classify_error(_FakeHTTPError(403)) == ErrorCategory.AUTH


def test_classifies_429_as_rate_limit() -> None:
    assert classify_error(_FakeHTTPError(429)) == ErrorCategory.RATE_LIMIT


def test_classifies_500_as_transient() -> None:
    assert classify_error(_FakeHTTPError(502)) == ErrorCategory.TRANSIENT
    assert classify_error(_FakeHTTPError(503)) == ErrorCategory.TRANSIENT


def test_classifies_404_as_node_unavailable() -> None:
    """404/405 are node-local (wrong endpoint, model missing) — must be switchable."""
    assert classify_error(_FakeHTTPError(404, "model not found")) == ErrorCategory.NODE_UNAVAILABLE


def test_classifies_405_as_node_unavailable() -> None:
    assert classify_error(_FakeHTTPError(405, "method not allowed")) == ErrorCategory.NODE_UNAVAILABLE


def test_classifies_400_as_malformed_by_default() -> None:
    assert classify_error(_FakeHTTPError(400, "bad tool schema")) == ErrorCategory.REQUEST_MALFORMED


def test_classifies_400_context_overflow_as_capacity() -> None:
    exc = _FakeHTTPError(400, "maximum context length exceeded: 200000 > 128000")
    assert classify_error(exc) == ErrorCategory.CAPACITY


def test_class_name_fallback_auth() -> None:
    assert classify_error(_FakeAuthenticationError("key invalid")) == ErrorCategory.AUTH


def test_class_name_fallback_rate_limit() -> None:
    assert classify_error(_FakeRateLimitError("slow down")) == ErrorCategory.RATE_LIMIT


def test_class_name_fallback_bad_request() -> None:
    assert classify_error(_FakeBadRequestError("schema nope")) == ErrorCategory.REQUEST_MALFORMED


def test_class_name_fallback_timeout() -> None:
    assert classify_error(_FakeAPITimeoutError("slow")) == ErrorCategory.TRANSIENT
    assert classify_error(_FakeAPIConnectionError("broken")) == ErrorCategory.TRANSIENT


def test_builtin_network_errors_are_transient() -> None:
    assert classify_error(TimeoutError("read timed out")) == ErrorCategory.TRANSIENT
    assert classify_error(ConnectionError("reset")) == ErrorCategory.TRANSIENT


def test_unwraps_retry_exhausted_error() -> None:
    inner = _FakeHTTPError(429, "too many")
    wrapped = RetryExhaustedError(inner, attempts=3)
    assert classify_error(wrapped) == ErrorCategory.RATE_LIMIT


def test_unknown_errors_default_to_unknown() -> None:
    class _Weird(Exception):
        pass

    assert classify_error(_Weird("???")) == ErrorCategory.UNKNOWN


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ErrorCategory.TRANSIENT, True),
        # Phase 2 semantics (design §5.5): rate-limit does NOT retry in-node —
        # it fails over immediately so the caller picks the next endpoint.
        (ErrorCategory.RATE_LIMIT, False),
        (ErrorCategory.UNKNOWN, True),
        (ErrorCategory.AUTH, False),
        (ErrorCategory.NODE_UNAVAILABLE, False),
        (ErrorCategory.REQUEST_MALFORMED, False),
        (ErrorCategory.CAPACITY, False),
    ],
)
def test_is_retryable(category: ErrorCategory, expected: bool) -> None:
    assert is_retryable(category) is expected


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ErrorCategory.TRANSIENT, True),
        (ErrorCategory.RATE_LIMIT, True),
        (ErrorCategory.AUTH, True),
        (ErrorCategory.NODE_UNAVAILABLE, True),  # the whole point of a pool
        (ErrorCategory.UNKNOWN, True),
        # Program bugs / capacity issues must NOT silently hop nodes.
        (ErrorCategory.REQUEST_MALFORMED, False),
        (ErrorCategory.CAPACITY, False),
    ],
)
def test_is_node_switchable(category: ErrorCategory, expected: bool) -> None:
    assert is_node_switchable(category) is expected
