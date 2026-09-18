"""Database engine/config for the PostgreSQL (Aiven) backend.

The store is being migrated from SQLite to Postgres. This module owns the connection:
it reads the Aiven URLs from the environment, normalizes them to the *synchronous*
psycopg (v3) driver (the whole codebase is sync — asyncpg would force an async rewrite),
and hands out SQLAlchemy engines.

The agent's .env holds two Aiven URLs:
    MA_AIVEN_ADMIN  -> DDL / Alembic migrations AND, for now, the application runtime
    MA_AIVEN_RLS    -> reserved for a future restricted runtime role (currently unused)

For now the runtime uses the ADMIN role too (no RLS / privilege separation yet), so
``runtime_url()`` falls back to the admin URL. When a restricted role is set up later,
point ``runtime_url()`` at ``MA_AIVEN_RLS`` (or set ``CHASER_DB_URL``).

Override either with CHASER_DB_ADMIN_URL / CHASER_DB_URL.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text

# Load the agent-side .env (holds the Aiven URLs) regardless of CWD.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def normalize_url(raw: str) -> str:
    """Coerce any Postgres URL to the sync psycopg driver with SSL required (Aiven)."""
    url = raw.strip().strip("'\"")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
            break
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def admin_url() -> str:
    raw = os.getenv("CHASER_DB_ADMIN_URL") or os.getenv("MA_AIVEN_ADMIN")
    if not raw:
        raise RuntimeError("No admin DB URL: set CHASER_DB_ADMIN_URL or MA_AIVEN_ADMIN")
    return normalize_url(raw)


def runtime_url() -> str:
    # Runtime uses the admin role for now (no RLS). Falls back to the admin URL.
    raw = os.getenv("CHASER_DB_URL") or os.getenv("MA_AIVEN_ADMIN")
    if not raw:
        raise RuntimeError("No runtime DB URL: set CHASER_DB_URL or MA_AIVEN_ADMIN")
    return normalize_url(raw)


def make_engine(url: str, **kwargs) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True, **kwargs)


def check_connection(admin: bool = True) -> str:
    """Open a connection and return the server version string. Raises on failure."""
    url = admin_url() if admin else runtime_url()
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version()")).scalar_one()
    finally:
        engine.dispose()


if __name__ == "__main__":  # python -m chaser.db  -> smoke test
    print("admin :", check_connection(admin=True))
    print("rls   :", check_connection(admin=False))
