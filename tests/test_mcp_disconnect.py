"""Unit tests for `MCPServerConnection.disconnect()`.

Ticket: P1 MCP teardown leaks `CancelledError`.

Python 3.8+ split `asyncio.CancelledError` off from `Exception` onto
`BaseException`, so a bare `except Exception:` clause no longer catches
it. The cleanup path in `disconnect()` is specifically called from
`finally` / shutdown contexts where the `ExitStack`'s underlying anyio
cancel scope can legitimately raise `CancelledError` (when the scope is
closed from a different task context during shutdown). Letting that
escape paints every shutdown path red — which is exactly what
`tests/test_mcp.py` was seeing before this fix.

These unit tests pin down the contract deterministically, without
needing a real MCP subprocess:

  - A `CancelledError` raised during `aclose()` must be swallowed.
  - An `ExceptionGroup[CancelledError]` (anyio's older shape) must also
    be swallowed — the in-code comment already documents that path.
  - A regular `Exception` from `aclose()` is still swallowed (no
    behavior change from before).
  - In every case, `exit_stack` and `session` must be cleared so the
    connection is in a clean post-disconnect state.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from mini_agent.tools.mcp_loader import MCPServerConnection


class _FakeStack:
    """Minimal stand-in for `AsyncExitStack` — raises whatever we tell it to."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self._raises = raises
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        if self._raises is not None:
            raise self._raises


def _new_conn() -> MCPServerConnection:
    # A STDIO shape is fine; `disconnect()` doesn't look at command/args.
    return MCPServerConnection(name="unit-test", command="echo", args=["hello"])


@pytest.mark.asyncio
async def test_disconnect_swallows_cancelled_error() -> None:
    """Bare `CancelledError` — the shape that breaks `except Exception`."""
    conn = _new_conn()
    conn.exit_stack = _FakeStack(raises=asyncio.CancelledError("shutdown race"))
    # Populate `session` so we can assert it gets cleared.
    conn.session = object()  # type: ignore[assignment]

    # MUST NOT raise.
    await conn.disconnect()

    assert conn.exit_stack is None, "exit_stack should be cleared after disconnect"
    assert conn.session is None, "session should be cleared after disconnect"


@pytest.mark.asyncio
async def test_disconnect_swallows_exception_group_of_cancelled() -> None:
    """anyio older behavior wraps CancelledError in an ExceptionGroup."""
    if sys.version_info < (3, 11):
        pytest.skip("ExceptionGroup requires Python 3.11+")

    conn = _new_conn()
    group = BaseExceptionGroup(  # type: ignore[name-defined]  # noqa: F821
        "stack close",
        [asyncio.CancelledError("inner")],
    )
    conn.exit_stack = _FakeStack(raises=group)

    # MUST NOT raise.
    await conn.disconnect()

    assert conn.exit_stack is None
    assert conn.session is None


@pytest.mark.asyncio
async def test_disconnect_swallows_regular_exception() -> None:
    """Pre-existing behavior — any plain Exception during aclose is eaten."""
    conn = _new_conn()
    conn.exit_stack = _FakeStack(raises=RuntimeError("anyio cancel scope"))

    await conn.disconnect()

    assert conn.exit_stack is None
    assert conn.session is None


@pytest.mark.asyncio
async def test_disconnect_clears_state_on_happy_path() -> None:
    conn = _new_conn()
    stack = _FakeStack(raises=None)
    conn.exit_stack = stack
    conn.session = object()  # type: ignore[assignment]

    await conn.disconnect()

    assert stack.closed, "aclose() should have been awaited"
    assert conn.exit_stack is None
    assert conn.session is None


@pytest.mark.asyncio
async def test_disconnect_noop_when_never_connected() -> None:
    """`disconnect()` on a fresh connection is a safe no-op."""
    conn = _new_conn()
    assert conn.exit_stack is None
    assert conn.session is None

    await conn.disconnect()  # must not raise

    assert conn.exit_stack is None
    assert conn.session is None
