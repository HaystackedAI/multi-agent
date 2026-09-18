"""Persistence for Chaser — SQLAlchemy Core over Postgres (runtime) or SQLite (tests).

The ``Store`` class is the only thing that touches the database. Its method surface is
intentionally small and plain (dicts in, dicts out) so the backend can be swapped: the
same code drives Aiven Postgres in production and an in-memory SQLite database in tests.

Backend is chosen by the constructor argument:
    ``Store(":memory:")``            -> in-memory SQLite (tests), shared via StaticPool
    ``Store("/path/to/chaser.db")``  -> file-backed SQLite
    ``Store("postgresql://...")``    -> Postgres via chaser.db (sync psycopg, sslmode=require)

The table definitions live in ``chaser.schema`` (one ``MetaData``). The six JSON columns
(transactions.allocations, decisions.tool_input/edits/result, actions.tool_input,
reports.report) are native JSONB on Postgres and JSON on SQLite, so SQLAlchemy handles
(de)serialization — callers pass and receive plain Python dicts/lists, no json.dumps here.

Tables: clients, client_notes, invoices, reminders, drafts, outbox, transactions,
receipts, emails, todos, decisions, actions, progress, reports, cycles, meta (16).
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from . import schema
from .config import days_between
from .schema import metadata

READ_ONLY_TOOL_PREFIXES = ("list_", "get_")

# Tables cleared by reset(). Mirrors the original SQLite reset exactly: it drops rows from
# 15 tables and deliberately leaves `progress` (live narration) untouched.
_RESET_TABLES = (
    schema.clients,
    schema.client_notes,
    schema.invoices,
    schema.reminders,
    schema.drafts,
    schema.outbox,
    schema.transactions,
    schema.receipts,
    schema.emails,
    schema.todos,
    schema.decisions,
    schema.actions,
    schema.reports,
    schema.cycles,
    schema.meta,
)


def utcnow() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """Short, URL-safe identifier such as ``dec_3f9a1c2b``."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _json_dumps(obj: Any) -> str:
    """JSON serializer for the engine — ``default=str`` keeps non-JSON values from crashing writes."""
    return json.dumps(obj, default=str)


class Store:
    """Thread-safe store over SQLAlchemy Core. One instance per process; guarded by an RLock."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        p = self.path
        json_kw = {"json_serializer": _json_dumps, "json_deserializer": json.loads}

        if p.startswith("postgres"):
            from .db import make_engine, normalize_url

            self._engine = make_engine(normalize_url(p), **json_kw)
            self._is_sqlite = False
        elif p == ":memory:":
            self._engine = create_engine(
                "sqlite://",
                future=True,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
                **json_kw,
            )
            self._is_sqlite = True
        else:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            self._engine = create_engine(
                f"sqlite+pysqlite:///{p}",
                future=True,
                connect_args={"check_same_thread": False},
                **json_kw,
            )
            self._is_sqlite = True

        # SQLite owns its own DDL (create on connect). On Postgres, Alembic owns the schema.
        if self._is_sqlite:
            if p != ":memory:":
                with self._engine.begin() as conn:
                    conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            metadata.create_all(self._engine)

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self._lock:
            self._engine.dispose()

    def reset(self) -> None:
        """Drop all rows (schema is kept). Leaves `progress` intact, matching prior behavior."""
        with self._lock, self._engine.begin() as conn:
            for table in _RESET_TABLES:
                conn.execute(table.delete())

    # ------------------------------------------------------------------ low-level helpers
    def _one(self, stmt) -> dict[str, Any] | None:
        with self._lock, self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return dict(row) if row is not None else None

    def _all(self, stmt) -> list[dict[str, Any]]:
        with self._lock, self._engine.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings().all()]

    def _scalar(self, stmt) -> Any:
        with self._lock, self._engine.connect() as conn:
            return conn.execute(stmt).scalar()

    def _exec(self, stmt) -> None:
        with self._lock, self._engine.begin() as conn:
            conn.execute(stmt)

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
        with self._lock, self._engine.begin() as conn:
            for c in clients:
                conn.execute(
                    schema.clients.insert().values(
                        id=c["id"],
                        name=c["name"],
                        contact_name=c.get("contact_name"),
                        email=c.get("email"),
                        ap_email=c.get("ap_email"),
                        terms_days=int(c.get("terms_days", 30)),
                        late_fee_clause=int(bool(c.get("late_fee_clause"))),
                        late_fee_pct_month=c.get("late_fee_pct_month"),
                        notes=c.get("notes"),
                    )
                )
            for inv in invoices:
                conn.execute(
                    schema.invoices.insert().values(
                        id=inv["id"],
                        client_id=inv["client_id"],
                        description=inv.get("description"),
                        amount=float(inv["amount"]),
                        amount_paid=float(inv.get("amount_paid", 0)),
                        issued=inv["issued"],
                        due=inv["due"],
                        status=inv["status"],
                        paid_on=inv.get("paid_on"),
                        payment_link=inv.get("payment_link"),
                        sent_to=inv.get("sent_to"),
                    )
                )
                for r in inv.get("reminders_sent", []):
                    conn.execute(
                        schema.reminders.insert().values(
                            invoice_id=inv["id"], tier=int(r["tier"]), sent_at=r["sent_at"], subject=r.get("subject")
                        )
                    )
            for tx in transactions:
                kind = tx.get("kind", "deposit" if tx["amount"] > 0 else "expense")
                status = "unmatched" if kind == "deposit" else ("ignored" if kind == "transfer" else "uncategorized")
                conn.execute(
                    schema.transactions.insert().values(
                        id=tx["id"],
                        date=tx["date"],
                        amount=float(tx["amount"]),
                        kind=kind,
                        memo=tx.get("memo"),
                        merchant=tx.get("merchant"),
                        category=tx.get("category"),
                        status=status,
                        receipt_id=None,
                        allocations=[],
                        note=None,
                    )
                )
            for r in receipts:
                conn.execute(
                    schema.receipts.insert().values(
                        id=r["id"],
                        date=r["date"],
                        merchant=r.get("merchant"),
                        amount=float(r["amount"]),
                        file=r.get("file"),
                        note=r.get("note"),
                        expense_id=None,
                    )
                )
            for e in emails:
                conn.execute(
                    schema.emails.insert().values(
                        id=e["id"],
                        date=e["date"],
                        from_name=e.get("from_name"),
                        from_email=e.get("from_email"),
                        client_id=e.get("client_id"),
                        subject=e.get("subject"),
                        body=e.get("body"),
                    )
                )
        self.set_meta("seeded_at", utcnow())

    # ------------------------------------------------------------------ clients
    def list_clients(self) -> list[dict[str, Any]]:
        return self._all(select(schema.clients).order_by(schema.clients.c.name))

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self._one(select(schema.clients).where(schema.clients.c.id == client_id))

    def add_client_note(self, client_id: str, note: str, source: str, cycle_id: str | None = None) -> None:
        self._exec(
            schema.client_notes.insert().values(
                client_id=client_id, note=note, source=source, created_at=utcnow(), cycle_id=cycle_id
            )
        )

    def list_client_notes(self, client_id: str) -> list[dict[str, Any]]:
        return self._all(
            select(schema.client_notes)
            .where(schema.client_notes.c.client_id == client_id)
            .order_by(schema.client_notes.c.id.desc())
            .limit(20)
        )

    # ------------------------------------------------------------------ invoices
    def list_invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        stmt = select(schema.invoices)
        if status:
            stmt = stmt.where(schema.invoices.c.status == status)
        return self._all(stmt.order_by(schema.invoices.c.due))

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        return self._one(select(schema.invoices).where(schema.invoices.c.id == invoice_id))

    def list_open_invoices(self) -> list[dict[str, Any]]:
        """Invoices with an outstanding balance (status open or partial)."""
        return self._all(
            select(schema.invoices)
            .where(schema.invoices.c.status.in_(("open", "partial")))
            .order_by(schema.invoices.c.due)
        )

    def list_overdue_invoices(self, today: date) -> list[dict[str, Any]]:
        rows = self.list_open_invoices()
        return [r for r in rows if days_between(r["due"], today) > 0]

    def count_overdue_open_invoices(self, today: date) -> int:
        return len(self.list_overdue_invoices(today))

    def update_invoice(self, invoice_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._exec(schema.invoices.update().where(schema.invoices.c.id == invoice_id).values(**fields))

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
            allocations = list(tx["allocations"])
            allocations.append({"invoice_id": invoice_id, "amount": amount, "fee": fee, "note": note})
            allocated = round(sum(a["amount"] for a in allocations), 2)
            tx_status = "matched" if allocated >= tx["amount"] - 0.005 else "partially_allocated"
            self.update_transaction(
                deposit_id, allocations=allocations, status=tx_status, note=note, category="client_revenue"
            )
        return self.get_invoice(invoice_id) or {}

    # ------------------------------------------------------------------ reminders / drafts / outbox
    def list_reminders(self, invoice_id: str) -> list[dict[str, Any]]:
        return self._all(
            select(schema.reminders)
            .where(schema.reminders.c.invoice_id == invoice_id)
            .order_by(schema.reminders.c.sent_at)
        )

    def count_reminders_for_client(self, client_id: str) -> int:
        r, i = schema.reminders, schema.invoices
        stmt = (
            select(func.count())
            .select_from(r.join(i, i.c.id == r.c.invoice_id))
            .where(i.c.client_id == client_id)
        )
        return int(self._scalar(stmt) or 0)

    def add_reminder(self, invoice_id: str, tier: int, sent_at: str, subject: str, cycle_id: str | None) -> None:
        self._exec(
            schema.reminders.insert().values(
                invoice_id=invoice_id, tier=tier, sent_at=sent_at, subject=subject, cycle_id=cycle_id
            )
        )

    def save_draft(self, invoice_id: str, tier: int, subject: str, body: str, cycle_id: str | None) -> dict:
        draft_id = new_id("draft")
        self._exec(
            schema.drafts.insert().values(
                id=draft_id,
                invoice_id=invoice_id,
                tier=tier,
                subject=subject,
                body=body,
                created_at=utcnow(),
                cycle_id=cycle_id,
            )
        )
        return self.get_draft(draft_id) or {}

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        return self._one(select(schema.drafts).where(schema.drafts.c.id == draft_id))

    def latest_draft(self, invoice_id: str) -> dict[str, Any] | None:
        # created_at is second-precision; id.desc() is the deterministic tie-break (was sqlite rowid).
        return self._one(
            select(schema.drafts)
            .where(schema.drafts.c.invoice_id == invoice_id)
            .order_by(schema.drafts.c.created_at.desc(), schema.drafts.c.id.desc())
            .limit(1)
        )

    def add_outbox(
        self, invoice_id: str | None, to_addr: str, subject: str, body: str, decision_id: str | None
    ) -> dict:
        msg_id = new_id("msg")
        self._exec(
            schema.outbox.insert().values(
                id=msg_id,
                invoice_id=invoice_id,
                to_addr=to_addr,
                subject=subject,
                body=body,
                sent_at=utcnow(),
                decision_id=decision_id,
            )
        )
        return self._one(select(schema.outbox).where(schema.outbox.c.id == msg_id)) or {}

    def list_outbox(self) -> list[dict[str, Any]]:
        return self._all(select(schema.outbox).order_by(schema.outbox.c.sent_at.desc()))

    # ------------------------------------------------------------------ transactions / receipts
    def list_transactions(self, kind: str | None = None) -> list[dict[str, Any]]:
        stmt = select(schema.transactions)
        if kind:
            stmt = stmt.where(schema.transactions.c.kind == kind)
        return self._all(stmt.order_by(schema.transactions.c.date.desc()))

    def get_transaction(self, tx_id: str) -> dict[str, Any] | None:
        return self._one(select(schema.transactions).where(schema.transactions.c.id == tx_id))

    def update_transaction(self, tx_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._exec(schema.transactions.update().where(schema.transactions.c.id == tx_id).values(**fields))

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
        stmt = select(schema.receipts)
        if unmatched_only:
            stmt = stmt.where(schema.receipts.c.expense_id.is_(None))
        return self._all(stmt.order_by(schema.receipts.c.date))

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        return self._one(select(schema.receipts).where(schema.receipts.c.id == receipt_id))

    def link_receipt(self, expense_id: str, receipt_id: str) -> None:
        with self._lock, self._engine.begin() as conn:
            conn.execute(schema.receipts.update().where(schema.receipts.c.id == receipt_id).values(expense_id=expense_id))
            conn.execute(
                schema.transactions.update().where(schema.transactions.c.id == expense_id).values(receipt_id=receipt_id)
            )

    # ------------------------------------------------------------------ emails
    def list_emails(self, since: date | None = None) -> list[dict[str, Any]]:
        rows = self._all(select(schema.emails).order_by(schema.emails.c.date.desc()))
        if since is None:
            return rows
        return [r for r in rows if date.fromisoformat(r["date"]) >= since]

    def latest_email_for_client(self, client_id: str) -> dict[str, Any] | None:
        return self._one(
            select(schema.emails)
            .where(schema.emails.c.client_id == client_id)
            .order_by(schema.emails.c.date.desc())
            .limit(1)
        )

    def list_emails_for_client(self, client_id: str, since: date | None = None) -> list[dict[str, Any]]:
        rows = self._all(
            select(schema.emails)
            .where(schema.emails.c.client_id == client_id)
            .order_by(schema.emails.c.date.desc())
        )
        if since is None:
            return rows
        return [r for r in rows if date.fromisoformat(r["date"]) >= since]

    # ------------------------------------------------------------------ todos
    def add_todo(
        self, title: str, due_date: str | None, note: str | None, cycle_id: str | None, dedupe_key: str | None = None
    ) -> dict[str, Any]:
        if dedupe_key:
            existing = self._one(
                select(schema.todos)
                .where(schema.todos.c.dedupe_key == dedupe_key, schema.todos.c.status == "open")
            )
            if existing:
                return {**existing, "duplicate": True}
        todo_id = new_id("todo")
        self._exec(
            schema.todos.insert().values(
                id=todo_id,
                title=title,
                due_date=due_date,
                note=note,
                status="open",
                created_at=utcnow(),
                cycle_id=cycle_id,
                dedupe_key=dedupe_key,
            )
        )
        return self._one(select(schema.todos).where(schema.todos.c.id == todo_id)) or {}

    def list_todos(self, open_only: bool = True) -> list[dict[str, Any]]:
        if open_only:
            return self._all(
                select(schema.todos)
                .where(schema.todos.c.status == "open")
                .order_by(schema.todos.c.due_date, schema.todos.c.created_at)
            )
        return self._all(select(schema.todos).order_by(schema.todos.c.created_at.desc()))

    def complete_todo(self, todo_id: str) -> None:
        self._exec(schema.todos.update().where(schema.todos.c.id == todo_id).values(status="done"))

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
            schema.decisions.insert().values(
                id=decision_id,
                kind=kind,
                agent=agent,
                tool_name=tool_name,
                tool_input=tool_input,
                summary=summary,
                severity=severity,
                client_id=client_id,
                invoice_id=invoice_id,
                tier=tier,
                dedupe_key=dedupe_key,
                status="pending",
                created_at=utcnow(),
                cycle_id=cycle_id,
            )
        )
        return self.get_decision(decision_id) or {}

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        return self._one(select(schema.decisions).where(schema.decisions.c.id == decision_id))

    def find_pending_decision(self, dedupe_key: str) -> dict[str, Any] | None:
        return self._one(
            select(schema.decisions)
            .where(schema.decisions.c.dedupe_key == dedupe_key, schema.decisions.c.status == "pending")
        )

    def list_decisions(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        stmt = select(schema.decisions)
        if status:
            stmt = stmt.where(schema.decisions.c.status == status)
        return self._all(stmt.order_by(schema.decisions.c.created_at.desc()).limit(limit))

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
            schema.decisions.update()
            .where(schema.decisions.c.id == decision_id)
            .values(status=status, resolved_at=utcnow(), response=response, edits=edits, result=result)
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
        with self._lock, self._engine.begin() as conn:
            result = conn.execute(
                schema.actions.insert().values(
                    agent=agent,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    summary=summary,
                    status=status,
                    kind=kind,
                    created_at=utcnow(),
                    cycle_id=cycle_id,
                    decision_id=decision_id,
                )
            )
            new_pk = result.inserted_primary_key[0]
            row = conn.execute(select(schema.actions).where(schema.actions.c.id == new_pk)).mappings().first()
        return dict(row) if row is not None else {}

    def list_actions(
        self, limit: int = 100, cycle_id: str | None = None, kinds: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(schema.actions)
        if cycle_id:
            stmt = stmt.where(schema.actions.c.cycle_id == cycle_id)
        if kinds:
            stmt = stmt.where(schema.actions.c.kind.in_(kinds))
        return self._all(stmt.order_by(schema.actions.c.id.desc()).limit(limit))

    # ------------------------------------------------------------------ progress (live narration)
    def add_progress(self, cycle_id: str | None, agent: str, kind: str, text: str) -> None:
        """What an agent said or is about to call, written as it happens so the UI can show it live."""
        self._exec(
            schema.progress.insert().values(
                cycle_id=cycle_id, agent=agent, kind=kind, text=text[:600], created_at=utcnow()
            )
        )

    def list_progress(self, cycle_id: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
        """Most recent narration lines, oldest first."""
        stmt = select(schema.progress)
        if cycle_id:
            stmt = stmt.where(schema.progress.c.cycle_id == cycle_id)
        rows = self._all(stmt.order_by(schema.progress.c.id.desc()).limit(limit))
        return list(reversed(rows))

    # ------------------------------------------------------------------ reports / cycles / meta
    def save_report(self, cycle_id: str, report: dict[str, Any]) -> None:
        self._exec(schema.reports.insert().values(cycle_id=cycle_id, created_at=utcnow(), report=report))

    def last_report(self) -> dict[str, Any] | None:
        row = self._one(select(schema.reports).order_by(schema.reports.c.id.desc()).limit(1))
        if not row:
            return None
        report = row["report"] or {}
        report["_cycle_id"] = row["cycle_id"]
        report["_created_at"] = row["created_at"]
        return report

    def start_cycle(self) -> str:
        cycle_id = new_id("cycle")
        self._exec(schema.cycles.insert().values(id=cycle_id, started_at=utcnow(), status="running"))
        return cycle_id

    def finish_cycle(self, cycle_id: str, status: str, detail: str | None = None) -> None:
        self._exec(
            schema.cycles.update()
            .where(schema.cycles.c.id == cycle_id)
            .values(finished_at=utcnow(), status=status, detail=detail)
        )
        self.set_meta("last_sweep_at", utcnow())
        self.set_meta("last_sweep_status", status)

    def last_cycle(self) -> dict[str, Any] | None:
        return self._one(select(schema.cycles).order_by(schema.cycles.c.started_at.desc()).limit(1))

    def set_meta(self, key: str, value: str) -> None:
        # Portable upsert (was INSERT OR REPLACE): update in place, else insert.
        with self._lock, self._engine.begin() as conn:
            result = conn.execute(schema.meta.update().where(schema.meta.c.key == key).values(value=value))
            if result.rowcount == 0:
                conn.execute(schema.meta.insert().values(key=key, value=value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self._scalar(select(schema.meta.c.value).where(schema.meta.c.key == key))
        return value if value is not None else default

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
            "actions": int(self._scalar(select(func.count()).select_from(schema.actions)) or 0),
        }
        return c
