"""Pure functions for matching bank deposits to invoices and expenses to receipts.

No I/O here: callers pass plain dicts and receive ranked candidates. The reconciler
agent sees these candidates inside ``list_unmatched_deposits()`` and decides what to do.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Any

CENTS = 0.011  # tolerance for float comparisons


@dataclass(frozen=True)
class FeePattern:
    """A known payment-processor or bank fee that can explain a short deposit."""

    name: str
    percent: float = 0.0
    fixed: float = 0.0

    def fee_for(self, gross: float) -> float:
        return round(gross * self.percent + self.fixed, 2)


FEE_PATTERNS: tuple[FeePattern, ...] = (
    FeePattern("card 2.9% + $0.30", 0.029, 0.30),
    FeePattern("card 3.5% + $0.49", 0.035, 0.49),
    FeePattern("ACH 0.8%", 0.008, 0.0),
    FeePattern("wire fee $15", 0.0, 15.0),
    FeePattern("wire fee $25", 0.0, 25.0),
    FeePattern("wire fee $30", 0.0, 30.0),
    FeePattern("wire fee $35", 0.0, 35.0),
)


@dataclass
class MatchCandidate:
    """One way a deposit could be explained by invoices."""

    kind: str  # exact | fee_adjusted | combined | partial
    invoice_ids: list[str]
    confidence: float
    fee: float = 0.0
    note: str = ""
    allocations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _outstanding(inv: dict[str, Any]) -> float:
    return round(float(inv["amount"]) - float(inv.get("amount_paid", 0.0)), 2)


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= 3 and t not in {"the", "and"}}


def memo_mentions_client(memo: str | None, client_name: str | None) -> bool:
    """True when the bank memo shares a meaningful word with the client name (e.g. HARBOR)."""
    return bool(_tokens(memo) & _tokens(client_name))


def match_deposit(
    amount: float,
    open_invoices: list[dict[str, Any]],
    memo: str | None = None,
    client_names: dict[str, str] | None = None,
) -> list[MatchCandidate]:
    """Rank the ways ``amount`` can be applied to ``open_invoices``.

    Args:
        amount: deposit amount (positive).
        open_invoices: invoice dicts with ``id``, ``client_id``, ``amount``, ``amount_paid``.
        memo: bank memo text, used to boost invoices whose client name appears in it.
        client_names: mapping client_id -> display name, for memo matching.

    Returns:
        Candidates sorted by confidence (highest first). Empty when nothing plausible.
    """
    client_names = client_names or {}
    amount = round(float(amount), 2)
    out: list[MatchCandidate] = []

    def boost(inv: dict[str, Any]) -> float:
        return 0.1 if memo_mentions_client(memo, client_names.get(inv["client_id"])) else 0.0

    for inv in open_invoices:
        due = _outstanding(inv)
        if due <= 0:
            continue
        if abs(due - amount) <= CENTS:
            out.append(
                MatchCandidate(
                    "exact",
                    [inv["id"]],
                    min(1.0, 0.9 + boost(inv)),
                    note=f"exact match for {inv['id']}",
                    allocations=[{"invoice_id": inv["id"], "amount": amount}],
                )
            )
            continue
        shortfall = round(due - amount, 2)
        if shortfall > 0:
            for pattern in FEE_PATTERNS:
                if abs(pattern.fee_for(due) - shortfall) <= CENTS:
                    out.append(
                        MatchCandidate(
                            "fee_adjusted",
                            [inv["id"]],
                            min(1.0, 0.75 + boost(inv)),
                            fee=shortfall,
                            note=f"{inv['id']} less {pattern.name} (${shortfall:.2f})",
                            allocations=[{"invoice_id": inv["id"], "amount": amount, "fee": shortfall}],
                        )
                    )
                    break

    # combined: two or three invoices from the same client summing to the deposit
    by_client: dict[str, list[dict[str, Any]]] = {}
    for inv in open_invoices:
        if _outstanding(inv) > 0:
            by_client.setdefault(inv["client_id"], []).append(inv)
    for _client_id, invs in by_client.items():
        for n in (2, 3):
            for combo in combinations(invs, n):
                total = round(sum(_outstanding(i) for i in combo), 2)
                if abs(total - amount) <= CENTS:
                    out.append(
                        MatchCandidate(
                            "combined",
                            [i["id"] for i in combo],
                            min(1.0, 0.7 + boost(combo[0])),
                            note="covers " + " + ".join(i["id"] for i in combo),
                            allocations=[{"invoice_id": i["id"], "amount": _outstanding(i)} for i in combo],
                        )
                    )

    # partial: deposit smaller than a single invoice's balance
    for inv in open_invoices:
        due = _outstanding(inv)
        if amount < due - CENTS:
            b = boost(inv)
            if b == 0.0 and len(open_invoices) > 3:
                continue  # without a memo hint, partials are too ambiguous to suggest
            half = abs(amount - due / 2) <= CENTS
            out.append(
                MatchCandidate(
                    "partial",
                    [inv["id"]],
                    round(0.3 + b * 3 + (0.15 if half else 0.0), 2),
                    note=f"partial payment of {inv['id']} (${due:.2f} outstanding)"
                    + (", exactly half" if half else ""),
                    allocations=[{"invoice_id": inv["id"], "amount": amount}],
                )
            )

    out.sort(key=lambda c: c.confidence, reverse=True)
    return out


def suggest_receipts(expense: dict[str, Any], receipts: list[dict[str, Any]], day_window: int = 3) -> list[dict]:
    """Receipts whose amount equals the expense and whose date is within ``day_window`` days.

    Merchant-name overlap raises confidence; results are sorted by confidence.
    """
    from datetime import date

    amount = abs(float(expense["amount"]))
    exp_date = date.fromisoformat(expense["date"])
    out = []
    for r in receipts:
        if r.get("expense_id"):
            continue
        if abs(float(r["amount"]) - amount) > CENTS:
            continue
        gap = abs((date.fromisoformat(r["date"]) - exp_date).days)
        if gap > day_window:
            continue
        name_hit = bool(_tokens(r.get("merchant")) & (_tokens(expense.get("merchant")) | _tokens(expense.get("memo"))))
        out.append(
            {
                "receipt_id": r["id"],
                "merchant": r.get("merchant"),
                "amount": r["amount"],
                "date": r["date"],
                "confidence": round(0.6 + (0.3 if name_hit else 0.0) + (0.1 if gap == 0 else 0.0), 2),
            }
        )
    out.sort(key=lambda c: c["confidence"], reverse=True)
    return out


def suggest_category(expense: dict[str, Any], chart: dict[str, Any]) -> str | None:
    """Pick the first chart-of-accounts category whose hints appear in the merchant/memo."""
    text = f"{expense.get('merchant') or ''} {expense.get('memo') or ''}".lower()
    for section in ("expense_categories", "non_pnl"):
        for cat in chart.get(section, []):
            for hint in cat.get("hints", []):
                if hint.lower() in text:
                    return cat["id"]
    return None
