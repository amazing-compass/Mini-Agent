"""Tests for TokenBudget — estimate + fits predicate."""

from __future__ import annotations

from types import SimpleNamespace

from mini_agent.llm.ha.budget import TokenBudget
from mini_agent.schema import Message


def _node(context_window: int):
    return SimpleNamespace(context_window=context_window)


def test_estimate_is_positive_for_nonempty_messages() -> None:
    msgs = [Message(role="user", content="Hello world")]
    assert TokenBudget.estimate(msgs) > 0


def test_estimate_grows_with_message_size() -> None:
    small = [Message(role="user", content="hi")]
    big = [Message(role="user", content="hello " * 500)]
    assert TokenBudget.estimate(big) > TokenBudget.estimate(small)


def test_estimate_handles_tools_schema_dict() -> None:
    msgs = [Message(role="user", content="do thing")]
    tools = [
        {
            "name": "get_weather",
            "description": "returns current weather for a city",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    est_without = TokenBudget.estimate(msgs)
    est_with = TokenBudget.estimate(msgs, tools)
    assert est_with > est_without


def test_fits_true_when_window_is_huge() -> None:
    msgs = [Message(role="user", content="short")]
    assert TokenBudget.fits(msgs, None, _node(100_000), expected_output=2048)


def test_fits_false_when_window_too_small() -> None:
    msgs = [Message(role="user", content="a" * 100)]
    # 64-token window can't house messages + 2k expected output + margin.
    assert TokenBudget.fits(msgs, None, _node(64), expected_output=2048) is False


def test_fits_respects_expected_output_headroom() -> None:
    """A node that technically has a few hundred free tokens doesn't 'fit'
    an expected 2k output — the fit predicate must reserve headroom."""
    msgs = [Message(role="user", content="hi")]
    # Size the window to just barely exceed the message estimate.
    est = TokenBudget.estimate(msgs)
    barely = _node(est + 500)
    assert TokenBudget.fits(msgs, None, barely, expected_output=2048) is False
    # With a small expected_output it should fit.
    assert TokenBudget.fits(msgs, None, barely, expected_output=100, safety_margin=100)


def test_estimate_accepts_dict_messages_too() -> None:
    msgs = [{"role": "user", "content": "hey there"}]
    assert TokenBudget.estimate(msgs) > 0
