"""SQLite-backed persistence for Chaser.

The ``Store`` class is the only thing that touches the database. Its method surface is
intentionally small and plain (dicts in, dicts out) so that a DynamoDB implementation
could replace it: one table per entity here maps to one table (or one single-table
partition) there, and every method is a simple key lookup, filtered scan, or put.

Tables: clients, client_notes, invoices, reminders, drafts, outbox, transactions,
receipts, emails, todos, decisions, actions, reports, cycles, meta.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .config import days_between

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, contact_name TEXT, email TEXT, ap_email TEXT,
    terms_days INTEGER NOT NULL DEFAULT 30, late_fee_clause INTEGER NOT NULL DEFAULT 0,
    late_fee_pct_month REAL, notes TEXT
);
CREATE TABLE IF NOT EXISTS client_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL, note TEXT NOT NULL,
    source TEXT NOT NULL, created_at TEXT NOT NULL, cycle_id TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, description TEXT, amount REAL NOT NULL,
    amount_paid REAL NOT NULL DEFAULT 0, issued TEXT NOT NULL, due TEXT NOT NULL,
    status TEXT NOT NULL, paid_on TEXT, payment_link TEXT, sent_to TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT NOT NULL, tier INTEGER NOT NULL,
    sent_at TEXT NOT NULL, subject TEXT, cycle_id TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, tier INTEGER NOT NULL, subject TEXT,
    body TEXT NOT NULL, created_at TEXT NOT NULL, cycle_id TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, invoice_id TEXT, to_addr TEXT NOT NULL, subject TEXT NOT NULL,
    body TEXT NOT NULL, sent_at TEXT NOT NULL, decision_id TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, date TEXT NOT NULL, amount REAL NOT NULL, kind TEXT NOT NULL,
    memo TEXT, merchant TEXT, category TEXT, status TEXT NOT NULL, receipt_id TEXT,
    allocations TEXT NOT NULL DEFAULT '[]', note TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY, date TEXT NOT NULL, merchant TEXT, amount REAL NOT NULL,
    file TEXT, note TEXT, expense_id TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY, date TEXT NOT NULL, from_name TEXT, from_email TEXT, client_id TEXT,
    subject TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, due_date TEXT, note TEXT, status TEXT NOT NULL,
    created_at TEXT NOT NULL, cycle_id TEXT, dedupe_key TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, agent TEXT, tool_name TEXT NOT NULL,
    tool_input TEXT NOT NULL, summary TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'normal',
    client_id TEXT, invoice_id TEXT, tier INTEGER, dedupe_key TEXT, status TEXT NOT NULL,
    created_at TEXT NOT NULL, resolved_at TEXT, response TEXT, edits TEXT, result TEXT, cycle_id TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, tool_name TEXT NOT NULL,
    tool_input TEXT NOT NULL, summary TEXT, status TEXT NOT NULL, kind TEXT NOT NULL,
    created_at TEXT NOT NULL, cycle_id TEXT, decision_id TEXT
);
CREATE TABLE IF NOT EXISTS progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, agent TEXT NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, created_at TEXT NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

READ_ONLY_TOOL_PREFIXES = ("list_", "get_")


def utcnow() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """Short, URL-safe identifier such as ``dec_3f9a1c2b``."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class Store:
    """Thread-safe SQLite store. One instance per process; guarded by an RLock."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset(self) -> None:
        """Drop all rows (schema is kept)."""
        tables = [
            "clients",
            "client_notes",
            "invoices",
            "reminders",
            "drafts",
            "outbox",
            "transactions",
            "receipts",
            "emails",
            "todos",
            "decisions",
            "actions",
            "reports",
            "cycles",
            "meta",
        ]
        with self._lock, self._conn:
            for table in tables:
                self._conn.execute(f"DELETE FROM {table}")

    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return _row(self._conn.execute(sql, params).fetchone())

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(self._conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------------ seeding
    def seed(
        self,
        clients: list[dict],
        invoices: list[dict],
        transactions: list[dict],
        receipts: list[dict],
        emails: list[dict],
    ) -> None:
        """Replace all domain data with the given records (decisions and audit are cleared too)."""
        self.reset()
        with self._lock, self._conn:
            for c in clients:
                self._conn.execute(
                    "INSERT INTO clients VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        c["id"],
                        c["name"],
                        c.get("contact_name"),
                        c.get("email"),
                        c.get("ap_email"),
                        int(c.get("terms_days", 30)),
                        int(bool(c.get("late_fee_clause"))),
                        c.get("late_fee_pct_month"),
                        c.get("notes"),
                    ),
                )
            for inv in invoices:
                self._conn.execute(
                    "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        inv["id"],
                        inv["client_id"],
                        inv.get("description"),
                        float(inv["amount"]),
                        float(inv.get("amount_paid", 0)),
                        inv["issued"],
                        inv["due"],
                        inv["status"],
                        inv.get("paid_on"),
                        inv.get("payment_link"),
                        inv.get("sent_to"),
                    ),
                )
                for r in inv.get("reminders_sent", []):
                    self._conn.execute(
                        "INSERT INTO reminders (invoice_id, tier, sent_at, subject) VALUES (?,?,?,?)",
                        (inv["id"], int(r["tier"]), r["sent_at"], r.get("subject")),
                    )
            for tx in transactions:
                kind = tx.get("kind", "deposit" if tx["amount"] > 0 else "expense")
                status = "unmatched" if kind == "deposit" else ("ignored" if kind == "transfer" else "uncategorized")
                self._conn.execute(
                    "INSERT INTO transactions (id, date, amount, kind, memo, merchant, category, status, receipt_id,"
                    " allocations, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tx["id"],
                        tx["date"],
                        float(tx["amount"]),
                        kind,
                        tx.get("memo"),
                        tx.get("merchant"),
                        tx.get("category"),
                        status,
                        None,
                        "[]",
                        None,
                    ),
                )
            for r in receipts:
                self._conn.execute(
                    "INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
                    (r["id"], r["date"], r.get("merchant"), float(r["amount"]), r.get("file"), r.get("note"), None),
                )
            for e in emails:
                self._conn.execute(
                    "INSERT INTO emails VALUES (?,?,?,?,?,?,?)",
                    (
                        e["id"],
                        e["date"],
                        e.get("from_name"),
                        e.get("from_email"),
                        e.get("client_id"),
                        e.get("subject"),
                        e.get("body"),
                    ),
                )
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_at', ?)", (utcnow(),))

    # ------------------------------------------------------------------ clients
    def list_clients(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM clients ORDER BY name")

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM clients WHERE id = ?", (client_id,))

    def add_client_note(self, client_id: str, note: str, source: str, cycle_id: str | None = None) -> None:
        self._exec(
            "INSERT INTO client_notes (client_id, note, source, created_at, cycle_id) VALUES (?,?,?,?,?)",
            (client_id, note, source, utcnow(), cycle_id),
        )

    def list_client_notes(self, client_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM client_notes WHERE client_id = ? ORDER BY id DESC LIMIT 20", (client_id,))

    # ------------------------------------------------------------------ invoices
    def list_invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._all("SELECT * FROM invoices WHERE status = ? ORDER BY due", (status,))
        return self._all("SELECT * FROM invoices ORDER BY due")

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM invoices WHERE id = ?", (invoice_id,))

    def list_open_invoices(self) -> list[dict[str, Any]]:
        """Invoices with an outstanding balance (status open or partial)."""
        return self._all("SELECT * FROM invoices WHERE status IN ('open','partial') ORDER BY due")

    def list_overdue_invoices(self, today: date) -> list[dict[str, Any]]:
        rows = self.list_open_invoices()
        return [r for r in rows if days_between(r["due"], today) > 0]

    def count_overdue_open_invoices(self, today: date) -> int:
        return len(self.list_overdue_invoices(today))

    def update_invoice(self, invoice_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE invoices SET {cols} WHERE id = ?", (*fields.values(), invoice_id))

    def record_payment(
        self,
        invoice_id: str,
        amount: float,
        deposit_id: str,
        today: date,
        note: str | None = None,
        fee: float = 0.0,
    ) -> dict[str, Any]:
        """Apply ``amount`` of cash from ``deposit_id`` to an invoice.

        ``fee`` is a processor/bank fee absorbed on this payment: the invoice is credited
        ``amount + fee`` (so it can close) while only ``amount`` is allocated from the deposit.
        """
        inv = self.get_invoice(invoice_id)
        if inv is None:
            raise KeyError(invoice_id)
        paid = round(inv["amount_paid"] + amount + fee, 2)
        outstanding = round(inv["amount"] - paid, 2)
        status = "paid" if outstanding <= 0.005 else "partial"
        self.update_invoice(
            invoice_id,
            amount_paid=paid,
            status=status,
            paid_on=today.isoformat() if status == "paid" else inv.get("paid_on"),
        )
        tx = self.get_transaction(deposit_id)
        if tx is not None:
            allocations = _loads(tx["allocations"], [])
            allocations.append({"invoice_id": invoice_id, "amount": amount, "fee": fee, "note": note})
            allocated = round(sum(a["amount"] for a in allocations), 2)
            tx_status = "matched" if allocated >= tx["amount"] - 0.005 else "partially_allocated"
            self.update_transaction(
                deposit_id, allocations=json.dumps(allocations), status=tx_status, note=note, category="client_revenue"
            )
        return self.get_invoice(invoice_id) or {}

    # ------------------------------------------------------------------ reminders / drafts / outbox
    def list_reminders(self, invoice_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM reminders WHERE invoice_id = ? ORDER BY sent_at", (invoice_id,))

    def count_reminders_for_client(self, client_id: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM reminders r JOIN invoices i ON i.id = r.invoice_id WHERE i.client_id = ?",
            (client_id,),
        )
        return int(row["n"]) if row else 0

    def add_reminder(self, invoice_id: str, tier: int, sent_at: str, subject: str, cycle_id: str | None) -> None:
        self._exec(
            "INSERT INTO reminders (invoice_id, tier, sent_at, subject, cycle_id) VALUES (?,?,?,?,?)",
            (invoice_id, tier, sent_at, subject, cycle_id),
        )

    def save_draft(self, invoice_id: str, tier: int, subject: str, body: str, cycle_id: str | None) -> dict:
        draft_id = new_id("draft")
        self._exec(
            "INSERT INTO drafts VALUES (?,?,?,?,?,?,?)",
            (draft_id, invoice_id, tier, subject, body, utcnow(), cycle_id),
        )
        return self.get_draft(draft_id) or {}

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM drafts WHERE id = ?", (draft_id,))

    def latest_draft(self, invoice_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM drafts WHERE invoice_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (invoice_id,)
        )

    def add_outbox(
        self, invoice_id: str | None, to_addr: str, subject: str, body: str, decision_id: str | None
    ) -> dict:
        msg_id = new_id("msg")
        self._exec(
            "INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
            (msg_id, invoice_id, to_addr, subject, body, utcnow(), decision_id),
        )
        return self._one("SELECT * FROM outbox WHERE id = ?", (msg_id,)) or {}

    def list_outbox(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM outbox ORDER BY sent_at DESC")

    # ------------------------------------------------------------------ transactions / receipts
    def list_transactions(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = (
            self._all("SELECT * FROM transactions WHERE kind = ? ORDER BY date DESC", (kind,))
            if kind
            else self._all("SELECT * FROM transactions ORDER BY date DESC")
        )
        for r in rows:
            r["allocations"] = _loads(r["allocations"], [])
        return rows

    def get_transaction(self, tx_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM transactions WHERE id = ?", (tx_id,))
        if row:
            row["allocations"] = _loads(row["allocations"], [])
        return row

    def update_transaction(self, tx_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE transactions SET {cols} WHERE id = ?", (*fields.values(), tx_id))

    def list_unmatched_deposits(self) -> list[dict[str, Any]]:
        return [t for t in self.list_transactions("deposit") if t["status"] in ("unmatched", "partially_allocated")]

    def list_uncategorized_expenses(self) -> list[dict[str, Any]]:
        return [t for t in self.list_transactions("expense") if not t["category"]]

    def list_expenses_missing_receipts(self, today: date, older_than_days: int = 7) -> list[dict[str, Any]]:
        out = []
        for t in self.list_transactions("expense"):
            if t["receipt_id"]:
                continue
            age = days_between(t["date"], today)
            if age >= older_than_days:
                out.append({**t, "age_days": age})
        return out

    def list_receipts(self, unmatched_only: bool = False) -> list[dict[str, Any]]:
        if unmatched_only:
            return self._all("SELECT * FROM receipts WHERE expense_id IS NULL ORDER BY date")
        return self._all("SELECT * FROM receipts ORDER BY date")

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM receipts WHERE id = ?", (receipt_id,))

    def link_receipt(self, expense_id: str, receipt_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE receipts SET expense_id = ? WHERE id = ?", (expense_id, receipt_id))
            self._conn.execute("UPDATE transactions SET receipt_id = ? WHERE id = ?", (receipt_id, expense_id))

    # ------------------------------------------------------------------ emails
    def list_emails(self, since: date | None = None) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM emails ORDER BY date DESC")
        if since is None:
            return rows
        return [r for r in rows if date.fromisoformat(r["date"]) >= since]

    def latest_email_for_client(self, client_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM emails WHERE client_id = ? ORDER BY date DESC LIMIT 1", (client_id,))

    def list_emails_for_client(self, client_id: str, since: date | None = None) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM emails WHERE client_id = ? ORDER BY date DESC", (client_id,))
        if since is None:
            return rows
        return [r for r in rows if date.fromisoformat(r["date"]) >= since]

    # ------------------------------------------------------------------ todos
    def add_todo(
        self, title: str, due_date: str | None, note: str | None, cycle_id: str | None, dedupe_key: str | None = None
    ) -> dict[str, Any]:
        if dedupe_key:
            existing = self._one("SELECT * FROM todos WHERE dedupe_key = ? AND status = 'open'", (dedupe_key,))
            if existing:
                return {**existing, "duplicate": True}
        todo_id = new_id("todo")
        self._exec(
            "INSERT INTO todos VALUES (?,?,?,?,?,?,?,?)",
            (todo_id, title, due_date, note, "open", utcnow(), cycle_id, dedupe_key),
        )
        return self._one("SELECT * FROM todos WHERE id = ?", (todo_id,)) or {}

    def list_todos(self, open_only: bool = True) -> list[dict[str, Any]]:
        if open_only:
            return self._all("SELECT * FROM todos WHERE status = 'open' ORDER BY due_date, created_at")
        return self._all("SELECT * FROM todos ORDER BY created_at DESC")

    def complete_todo(self, todo_id: str) -> None:
        self._exec("UPDATE todos SET status = 'done' WHERE id = ?", (todo_id,))

    # ------------------------------------------------------------------ decisions
    def create_decision(
        self,
        *,
        kind: str,
        agent: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
        summary: str,
        severity: str = "normal",
        client_id: str | None = None,
        invoice_id: str | None = None,
        tier: int | None = None,
        dedupe_key: str | None = None,
        cycle_id: str | None = None,
    ) -> dict[str, Any]:
        decision_id = new_id("dec")
        self._exec(
            "INSERT INTO decisions (id, kind, agent, tool_name, tool_input, summary, severity, client_id, invoice_id,"
            " tier, dedupe_key, status, created_at, cycle_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                kind,
                agent,
                tool_name,
                json.dumps(tool_input),
                summary,
                severity,
                client_id,
                invoice_id,
                tier,
                dedupe_key,
                "pending",
                utcnow(),
                cycle_id,
            ),
        )
        return self.get_decision(decision_id) or {}

    def _decode_decision(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["tool_input"] = _loads(row["tool_input"], {})
        row["edits"] = _loads(row["edits"], None)
        row["result"] = _loads(row["result"], row["result"])
        return row

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        return self._decode_decision(self._one("SELECT * FROM decisions WHERE id = ?", (decision_id,)))

    def find_pending_decision(self, dedupe_key: str) -> dict[str, Any] | None:
        return self._decode_decision(
            self._one("SELECT * FROM decisions WHERE dedupe_key = ? AND status = 'pending'", (dedupe_key,))
        )

    def list_decisions(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            rows = self._all(
                "SELECT * FROM decisions WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
            )
        else:
            rows = self._all("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._decode_decision(r) for r in rows]  # type: ignore[misc]

    def list_pending_decisions(self) -> list[dict[str, Any]]:
        return self.list_decisions("pending")

    def resolve_decision(
        self,
        decision_id: str,
        status: str,
        response: str | None,
        edits: dict[str, Any] | None,
        result: Any,
    ) -> dict[str, Any]:
        self._exec(
            "UPDATE decisions SET status = ?, resolved_at = ?, response = ?, edits = ?, result = ? WHERE id = ?",
            (
                status,
                utcnow(),
                response,
                json.dumps(edits) if edits is not None else None,
                json.dumps(result, default=str) if result is not None else None,
                decision_id,
            ),
        )
        return self.get_decision(decision_id) or {}

    # ------------------------------------------------------------------ actions (audit log)
    def add_action(
        self,
        *,
        agent: str,
        tool_name: str,
        tool_input: dict[str, Any],
        summary: str | None,
        status: str,
        cycle_id: str | None,
        kind: str | None = None,
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        if kind is None:
            kind = "read" if tool_name.startswith(READ_ONLY_TOOL_PREFIXES) else "write"
        cur = self._exec(
            "INSERT INTO actions (agent, tool_name, tool_input, summary, status, kind, created_at, cycle_id,"
            " decision_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                agent,
                tool_name,
                json.dumps(tool_input, default=str),
                summary,
                status,
                kind,
                utcnow(),
                cycle_id,
                decision_id,
            ),
        )
        row = self._one("SELECT * FROM actions WHERE id = ?", (cur.lastrowid,)) or {}
        row["tool_input"] = _loads(row.get("tool_input"), {})
        return row

    def list_actions(
        self, limit: int = 100, cycle_id: str | None = None, kinds: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM actions"
        clauses, params = [], []
        if cycle_id:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._all(sql, tuple(params))
        for r in rows:
            r["tool_input"] = _loads(r["tool_input"], {})
        return rows

    # ------------------------------------------------------------------ progress (live narration)
    def add_progress(self, cycle_id: str | None, agent: str, kind: str, text: str) -> None:
        """What an agent said or is about to call, written as it happens so the UI can show it live."""
        self._exec(
            "INSERT INTO progress (cycle_id, agent, kind, text, created_at) VALUES (?,?,?,?,?)",
            (cycle_id, agent, kind, text[:600], utcnow()),
        )

    def list_progress(self, cycle_id: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
        """Most recent narration lines, oldest first."""
        sql, params = "SELECT * FROM progress", []
        if cycle_id:
            sql += " WHERE cycle_id = ?"
            params.append(cycle_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return list(reversed(self._all(sql, tuple(params))))

    # ------------------------------------------------------------------ reports / cycles / meta
    def save_report(self, cycle_id: str, report: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO reports (cycle_id, created_at, report) VALUES (?,?,?)",
            (cycle_id, utcnow(), json.dumps(report, default=str)),
        )

    def last_report(self) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM reports ORDER BY id DESC LIMIT 1")
        if not row:
            return None
        report = _loads(row["report"], {})
        report["_cycle_id"] = row["cycle_id"]
        report["_created_at"] = row["created_at"]
        return report

    def start_cycle(self) -> str:
        cycle_id = new_id("cycle")
        self._exec("INSERT INTO cycles (id, started_at, status) VALUES (?,?,?)", (cycle_id, utcnow(), "running"))
        return cycle_id

    def finish_cycle(self, cycle_id: str, status: str, detail: str | None = None) -> None:
        self._exec(
            "UPDATE cycles SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (utcnow(), status, detail, cycle_id),
        )
        self.set_meta("last_sweep_at", utcnow())
        self.set_meta("last_sweep_status", status)

    def last_cycle(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1")

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    # ------------------------------------------------------------------ aggregates
    def aging_buckets(self, today: date) -> dict[str, Any]:
        """Outstanding receivables grouped by days past due."""
        buckets: dict[str, dict[str, Any]] = {
            k: {"total": 0.0, "count": 0, "invoices": []} for k in ("current", "d1_30", "d31_60", "d61_plus")
        }
        for inv in self.list_open_invoices():
            outstanding = round(inv["amount"] - inv["amount_paid"], 2)
            age = days_between(inv["due"], today)
            key = "current" if age <= 0 else "d1_30" if age <= 30 else "d31_60" if age <= 60 else "d61_plus"
            client = self.get_client(inv["client_id"]) or {}
            buckets[key]["total"] = round(buckets[key]["total"] + outstanding, 2)
            buckets[key]["count"] += 1
            buckets[key]["invoices"].append(
                {
                    "id": inv["id"],
                    "client": client.get("name", inv["client_id"]),
                    "outstanding": outstanding,
                    "days_overdue": max(age, 0),
                    "due": inv["due"],
                    "status": inv["status"],
                }
            )
        return buckets

    def cash_collected(self, since: date, until: date) -> float:
        total = 0.0
        for t in self.list_transactions("deposit"):
            if t["status"] in ("matched", "partially_allocated") or t["category"] == "client_revenue":
                d = date.fromisoformat(t["date"])
                if since <= d <= until:
                    total += sum(a["amount"] for a in t["allocations"])
        return round(total, 2)

    def counts(self) -> dict[str, int]:
        c = {
            "clients": len(self.list_clients()),
            "invoices_open": len(self.list_open_invoices()),
            "decisions_pending": len(self.list_pending_decisions()),
            "decisions_resolved": len([d for d in self.list_decisions() if d["status"] != "pending"]),
            "todos_open": len(self.list_todos()),
            "unmatched_deposits": len(self.list_unmatched_deposits()),
            "uncategorized_expenses": len(self.list_uncategorized_expenses()),
            "actions": int((self._one("SELECT COUNT(*) AS n FROM actions") or {"n": 0})["n"]),
        }
        return c
