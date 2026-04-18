"""Smoke tests for every script under `examples/`.

The reason this exists: when Phase 3 removed the `LLMClient` facade
from `mini_agent.__init__`, every example kept importing it and the
whole `examples/` directory silently broke. Nothing in CI caught it.

These tests only load the modules — they do NOT call `main()` or hit
any network. The goal is minimal: if an example can be imported, its
top-level wiring (imports, class definitions, helper references) is
at least syntactically and symbolically valid.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# The numbered demos — `_common.py` is a library module, not a
# standalone script, so import it explicitly as part of the same
# sys.path setup and exclude it from the per-file loop below.
EXAMPLE_SCRIPTS = sorted(
    p for p in EXAMPLES_DIR.glob("*.py")
    if p.name not in {"_common.py"}
)


@pytest.fixture(autouse=True)
def _examples_on_syspath():
    """Make `from _common import ...` resolve the way it does at runtime."""
    path_entry = str(EXAMPLES_DIR)
    inserted = False
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)
        inserted = True
    try:
        yield
    finally:
        if inserted:
            sys.path.remove(path_entry)


def _load_module(path: Path):
    """Execute the module's top-level code (imports + defs, no main())."""
    spec = importlib.util.spec_from_file_location(f"examples_smoke_{path.stem}", path)
    assert spec is not None and spec.loader is not None, f"cannot build spec for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_common_module_imports_cleanly() -> None:
    """`examples/_common.py` is the shared helper module — it must load."""
    module = _load_module(EXAMPLES_DIR / "_common.py")
    # Public surface the examples depend on.
    assert hasattr(module, "load_config")
    assert hasattr(module, "load_system_prompt")
    assert hasattr(module, "build_router")
    assert hasattr(module, "build_direct_client")


@pytest.mark.parametrize(
    "script_path",
    EXAMPLE_SCRIPTS,
    ids=[p.name for p in EXAMPLE_SCRIPTS],
)
def test_example_script_imports_cleanly(script_path: Path) -> None:
    """Every example must at least load — no ImportError, no NameError."""
    module = _load_module(script_path)
    # Every numbered demo exposes `main()` as its entry point.
    assert callable(getattr(module, "main", None)), (
        f"{script_path.name} is missing an async `main()` entry point"
    )
