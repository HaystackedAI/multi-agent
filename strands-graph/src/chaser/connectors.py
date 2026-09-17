"""Connector interfaces and their demo (JSON / SQLite) implementations.

Real integrations (Plaid for bank feeds, Gmail/IMAP for the inbox, an email API for
sending) would implement the same small protocols. Only the demo versions exist here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import yaml

from .config import DATA_DIR
from .store import Store


class BankConnector(Protocol):
    def fetch_transactions(self) -> list[dict[str, Any]]: ...


class MailboxConnector(Protocol):
    def fetch_messages(self) -> list[dict[str, Any]]: ...


class InvoicingConnector(Protocol):
    def fetch_clients(self) -> list[dict[str, Any]]: ...
    def fetch_invoices(self) -> list[dict[str, Any]]: ...


class ReceiptsConnector(Protocol):
    def fetch_receipts(self) -> list[dict[str, Any]]: ...


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str, *, invoice_id: str | None, decision_id: str | None) -> dict: ...


def _load_json(path: Path) -> list[dict[str, Any]]:
    with path.open() as fh:
        return json.load(fh)


class JsonDataConnector:
    """Reads the bundled demo dataset. Implements Bank, Mailbox, Invoicing and Receipts connectors."""

    def __init__(self, data_dir: Path | str = DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    def fetch_clients(self) -> list[dict[str, Any]]:
        return _load_json(self.data_dir / "clients.json")

    def fetch_invoices(self) -> list[dict[str, Any]]:
        return _load_json(self.data_dir / "invoices.json")

    def fetch_transactions(self) -> list[dict[str, Any]]:
        return _load_json(self.data_dir / "bank_transactions.json")

    def fetch_receipts(self) -> list[dict[str, Any]]:
        return _load_json(self.data_dir / "receipts.json")

    def fetch_messages(self) -> list[dict[str, Any]]:
        return _load_json(self.data_dir / "inbox.json")

    def chart_of_accounts(self) -> dict[str, Any]:
        with (self.data_dir / "chart_of_accounts.yaml").open() as fh:
            return yaml.safe_load(fh)


class OutboxEmailSender:
    """Demo sender: records the message in the store's outbox table instead of delivering it."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def send(self, to: str, subject: str, body: str, *, invoice_id: str | None, decision_id: str | None) -> dict:
        return self.store.add_outbox(invoice_id, to, subject, body, decision_id)


def load_chart_of_accounts(data_dir: Path | str = DATA_DIR) -> dict[str, Any]:
    return JsonDataConnector(data_dir).chart_of_accounts()
