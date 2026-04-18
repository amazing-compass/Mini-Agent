"""Tests for `mini_agent.cli.resolve_workspace_dir`.

The CLI must honor its three-tier priority: `--workspace` flag, then
`config.agent.workspace_dir`, then current working directory. Prior to
Phase 3 the config value was silently dropped — regressing this is the
whole reason the function is now separately testable.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_agent.cli import resolve_workspace_dir


def _stub_config(workspace_dir: str | None):
    """Minimal stand-in for `Config.load()` — only `.agent.workspace_dir` is touched."""
    agent = SimpleNamespace(workspace_dir=workspace_dir)
    cfg = SimpleNamespace(agent=agent)
    return lambda: cfg


def test_cli_flag_beats_config(tmp_path: Path) -> None:
    flag = tmp_path / "from_flag"
    cfg_ws = tmp_path / "from_config"
    resolved = resolve_workspace_dir(
        str(flag),
        config_loader=_stub_config(str(cfg_ws)),
    )
    assert resolved == flag.absolute()


def test_config_is_used_when_flag_absent(tmp_path: Path) -> None:
    cfg_ws = tmp_path / "from_config"
    resolved = resolve_workspace_dir(
        None,
        config_loader=_stub_config(str(cfg_ws)),
    )
    assert resolved == cfg_ws.absolute()


def test_falls_back_to_cwd_when_neither_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_workspace_dir(None, config_loader=_stub_config(None))
    assert resolved == tmp_path.resolve()


def test_config_load_failure_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken config must not crash the CLI's workspace resolution path."""
    monkeypatch.chdir(tmp_path)

    def raiser():
        raise FileNotFoundError("no config.yaml here")

    resolved = resolve_workspace_dir(None, config_loader=raiser)
    assert resolved == tmp_path.resolve()


def test_tilde_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = resolve_workspace_dir("~/ws_under_home", config_loader=_stub_config(None))
    assert resolved == (tmp_path / "ws_under_home").absolute()


def test_tilde_also_expanded_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = resolve_workspace_dir(
        None,
        config_loader=_stub_config("~/cfg_ws"),
    )
    assert resolved == (tmp_path / "cfg_ws").absolute()
