"""Alembic environment for Chaser's Postgres (Aiven) backend.

Migrations always run against the ADMIN role (DDL). The target schema is the SQLAlchemy
metadata in ``chaser.schema``. The URL comes from ``chaser.db.admin_url()`` (Aiven, sync
psycopg, sslmode=require) — it is NOT read from alembic.ini, so the password never has to
survive ConfigParser ``%`` interpolation.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the `chaser` package importable (src layout), mirroring main.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaser.db import admin_url, make_engine  # noqa: E402
from chaser.schema import metadata as target_metadata  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DB_URL = admin_url()


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (alembic upgrade --sql)."""
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the live Aiven database over the admin connection."""
    connectable = make_engine(DB_URL)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
