"""Process-wide runtime context shared by tools, hooks and the approval gate.

Tools are plain functions decorated with ``@tool``; they need a way to reach the
active :class:`~chaser.store.Store` and the id of the sweep cycle currently running.
Sweeps are serialized with a lock, so a single module-level context is sufficient.
Tests swap the store with :func:`use_store`.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .config import DEFAULT_DB_PATH
from .store import Store

_lock = threading.RLock()
_store: Store | None = None
_cycle_id: str | None = None

_POSTGRES_BACKENDS = ("postgres", "postgresql", "pg")


def _make_default_store() -> Store:
    """Build the default store: Postgres (Aiven) when opted in, else a local SQLite file.

    Runtime uses Postgres by setting ``CHASER_DB_BACKEND=postgres`` (the agent deployment).
    Tests leave it unset (or force ``sqlite``) so they stay fully offline.
    """
    if os.getenv("CHASER_DB_BACKEND", "sqlite").lower() in _POSTGRES_BACKENDS:
        from .db import runtime_url

        return Store(runtime_url())
    return Store(DEFAULT_DB_PATH)


def get_store() -> Store:
    """Return the active store, creating the default store lazily (see ``_make_default_store``)."""
    global _store
    with _lock:
        if _store is None:
            _store = _make_default_store()
        return _store


def set_store(store: Store | None) -> None:
    """Replace the active store (used by tests and by the CLI ``--db`` option)."""
    global _store
    with _lock:
        _store = store


@contextmanager
def use_store(store: Store) -> Iterator[Store]:
    """Temporarily make ``store`` the active store."""
    previous = _store
    set_store(store)
    try:
        yield store
    finally:
        set_store(previous)


def current_cycle_id() -> str | None:
    """Id of the sweep cycle in progress, or None outside a sweep."""
    return _cycle_id


def set_cycle_id(cycle_id: str | None) -> None:
    """Set the sweep cycle id that tools and hooks tag their rows with."""
    global _cycle_id
    _cycle_id = cycle_id
