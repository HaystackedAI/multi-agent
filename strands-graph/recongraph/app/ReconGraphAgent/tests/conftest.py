"""Shared fixtures: an in-memory seeded store made active for the tools, and scripted models."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("DEMO_TODAY", "2026-09-12")
os.environ["AGENT_BACKEND"] = "local"
os.environ["SWEEP_INTERVAL_SECONDS"] = "0"
os.environ["CHASER_DB_BACKEND"] = "sqlite"  # keep tests offline even if the agent .env selects postgres

from chaser import service  # noqa: E402
from chaser.context import set_store  # noqa: E402
from chaser.store import Store  # noqa: E402


@pytest.fixture
def store() -> Iterator[Store]:
    """Fresh in-memory store seeded with the demo data and installed as the active store."""
    s = Store(":memory:")
    service.seed(s)
    set_store(s)
    try:
        yield s
    finally:
        set_store(None)
        s.close()


@pytest.fixture
def empty_store() -> Iterator[Store]:
    s = Store(":memory:")
    set_store(s)
    try:
        yield s
    finally:
        set_store(None)
        s.close()
