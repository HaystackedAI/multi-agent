"""SQLAlchemy Core metadata for the Postgres backend — mirrors the SQLite ``SCHEMA``.

One ``Table`` per entity, matching ``store.py::SCHEMA`` column-for-column, with two
deliberate type upgrades for Postgres:

* the six JSON-in-TEXT columns become native ``JSONB``
  (transactions.allocations, decisions.tool_input/edits/result, actions.tool_input,
  reports.report) — the store currently json.dumps/loads these by hand;
* ``INTEGER PRIMARY KEY AUTOINCREMENT`` becomes an identity ``Integer`` primary key.

Timestamps and dates stay as ISO ``Text`` (the code reads/writes them as strings and
orders on them lexically); changing them to real TIMESTAMP/DATE is a later, separate step.

This module defines *only* the schema. ``alembic`` (next step) points at ``metadata`` to
generate the migration; the rewritten ``Store`` (later step) runs queries against these tables.
"""

from __future__ import annotations

from sqlalchemy import JSON, Column, Float, Integer, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

# JSON columns render as native JSONB on Postgres and as SQLite's JSON (TEXT) on SQLite,
# so the same metadata drives the Aiven migration *and* the offline in-memory test store.
JSON_T = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")

clients = Table(
    "clients",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("contact_name", Text),
    Column("email", Text),
    Column("ap_email", Text),
    Column("terms_days", Integer, nullable=False, server_default=text("30")),
    Column("late_fee_clause", Integer, nullable=False, server_default=text("0")),
    Column("late_fee_pct_month", Float),
    Column("notes", Text),
)

client_notes = Table(
    "client_notes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("client_id", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("cycle_id", Text),
)

invoices = Table(
    "invoices",
    metadata,
    Column("id", Text, primary_key=True),
    Column("client_id", Text, nullable=False),
    Column("description", Text),
    Column("amount", Float, nullable=False),
    Column("amount_paid", Float, nullable=False, server_default=text("0")),
    Column("issued", Text, nullable=False),
    Column("due", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("paid_on", Text),
    Column("payment_link", Text),
    Column("sent_to", Text),
)

reminders = Table(
    "reminders",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("invoice_id", Text, nullable=False),
    Column("tier", Integer, nullable=False),
    Column("sent_at", Text, nullable=False),
    Column("subject", Text),
    Column("cycle_id", Text),
)

drafts = Table(
    "drafts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("invoice_id", Text, nullable=False),
    Column("tier", Integer, nullable=False),
    Column("subject", Text),
    Column("body", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("cycle_id", Text),
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", Text, primary_key=True),
    Column("invoice_id", Text),
    Column("to_addr", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("sent_at", Text, nullable=False),
    Column("decision_id", Text),
)

transactions = Table(
    "transactions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("date", Text, nullable=False),
    Column("amount", Float, nullable=False),
    Column("kind", Text, nullable=False),
    Column("memo", Text),
    Column("merchant", Text),
    Column("category", Text),
    Column("status", Text, nullable=False),
    Column("receipt_id", Text),
    Column("allocations", JSON_T, nullable=False, server_default=text("'[]'")),
    Column("note", Text),
)

receipts = Table(
    "receipts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("date", Text, nullable=False),
    Column("merchant", Text),
    Column("amount", Float, nullable=False),
    Column("file", Text),
    Column("note", Text),
    Column("expense_id", Text),
)

emails = Table(
    "emails",
    metadata,
    Column("id", Text, primary_key=True),
    Column("date", Text, nullable=False),
    Column("from_name", Text),
    Column("from_email", Text),
    Column("client_id", Text),
    Column("subject", Text),
    Column("body", Text),
)

todos = Table(
    "todos",
    metadata,
    Column("id", Text, primary_key=True),
    Column("title", Text, nullable=False),
    Column("due_date", Text),
    Column("note", Text),
    Column("status", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("cycle_id", Text),
    Column("dedupe_key", Text),
)

decisions = Table(
    "decisions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("agent", Text),
    Column("tool_name", Text, nullable=False),
    Column("tool_input", JSON_T, nullable=False),
    Column("summary", Text, nullable=False),
    Column("severity", Text, nullable=False, server_default=text("'normal'")),
    Column("client_id", Text),
    Column("invoice_id", Text),
    Column("tier", Integer),
    Column("dedupe_key", Text),
    Column("status", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("resolved_at", Text),
    Column("response", Text),
    Column("edits", JSON_T),
    Column("result", JSON_T),
    Column("cycle_id", Text),
)

actions = Table(
    "actions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent", Text, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("tool_input", JSON_T, nullable=False),
    Column("summary", Text),
    Column("status", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("cycle_id", Text),
    Column("decision_id", Text),
)

progress = Table(
    "progress",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cycle_id", Text),
    Column("agent", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

reports = Table(
    "reports",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cycle_id", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("report", JSON_T, nullable=False),
)

cycles = Table(
    "cycles",
    metadata,
    Column("id", Text, primary_key=True),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
    Column("status", Text, nullable=False),
    Column("detail", Text),
)

meta = Table(
    "meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

# All 16 tables, in dependency-free creation order (no FKs are declared, matching SQLite).
ALL_TABLES = (
    clients,
    client_notes,
    invoices,
    reminders,
    drafts,
    outbox,
    transactions,
    receipts,
    emails,
    todos,
    decisions,
    actions,
    progress,
    reports,
    cycles,
    meta,
)
