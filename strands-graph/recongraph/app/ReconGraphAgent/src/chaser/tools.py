"""Strands tools for the four Chaser agents.

Every tool reads or writes through the active :class:`Store` (see ``context.py``).
Return values are JSON-serializable dicts or strings; the model reads the docstrings,
so they say precisely when to use each tool and what comes back.

Tool ownership (see ``agents.py``):
    reconciler : list_open_invoices, list_unmatched_deposits, list_recent_client_emails,
                 match_deposit_to_invoice, record_partial_payment, flag_deposit_for_review,
                 note_client_context
    collector  : list_overdue_invoices, get_client_profile, get_contract_terms, draft_followup,
                 send_client_email (gated), offer_payment_plan (gated), propose_write_off (gated)
    bookkeeper : list_uncategorized_expenses, categorize_expense, list_expenses_missing_receipts,
                 create_todo, match_receipt
    reporter   : get_week_summary, list_pending_decisions, list_actions_this_week
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from strands import tool

from . import config
from .connectors import OutboxEmailSender, load_chart_of_accounts
from .context import current_cycle_id, get_store
from .matching import match_deposit, suggest_category, suggest_receipts
from .tiers import TIER_LABELS, email_mentions_payment, late_fee_amount, select_tier

GATED_TOOLS: frozenset[str] = frozenset({"send_client_email", "offer_payment_plan", "propose_write_off"})


# ----------------------------------------------------------------------------- helpers
def _client_name(client_id: str) -> str:
    c = get_store().get_client(client_id)
    return c["name"] if c else client_id


def _invoice_view(inv: dict[str, Any]) -> dict[str, Any]:
    today = config.today()
    outstanding = round(inv["amount"] - inv["amount_paid"], 2)
    age = config.days_between(inv["due"], today)
    return {
        "invoice_id": inv["id"],
        "client_id": inv["client_id"],
        "client": _client_name(inv["client_id"]),
        "description": inv["description"],
        "amount": inv["amount"],
        "amount_paid": inv["amount_paid"],
        "outstanding": outstanding,
        "issued": inv["issued"],
        "due": inv["due"],
        "days_overdue": max(age, 0),
        "status": inv["status"],
        "payment_link": inv["payment_link"],
    }


def _owner_declined_days_ago(client_id: str) -> int | None:
    """Days since the owner last declined a proposal for this client (from decisions)."""
    today = config.today()
    best: int | None = None
    for d in get_store().list_decisions("denied"):
        if d.get("client_id") != client_id:
            continue
        result = d.get("result") if isinstance(d.get("result"), dict) else {}
        # prefer the demo-clock date recorded at decision time; fall back to the wall-clock timestamp
        resolved = result.get("decided_on") or (d.get("resolved_at") or d["created_at"])[:10]
        days = max(config.days_between(resolved, today), 0)
        best = days if best is None else min(best, days)
    return best


def _client_profile(client_id: str) -> dict[str, Any]:
    store = get_store()
    client = store.get_client(client_id)
    if client is None:
        return {"error": f"unknown client {client_id}"}
    today = config.today()
    paid = [i for i in store.list_invoices("paid") if i["client_id"] == client_id and i.get("paid_on")]
    days_to_pay = [config.days_between(i["issued"], i["paid_on"]) for i in paid]
    avg_days = round(sum(days_to_pay) / len(days_to_pay), 1) if days_to_pay else None
    open_invoices = [_invoice_view(i) for i in store.list_open_invoices() if i["client_id"] == client_id]
    latest_email = store.latest_email_for_client(client_id)
    notes = [
        {"note": n["note"], "source": n["source"], "at": n["created_at"][:10]}
        for n in store.list_client_notes(client_id)
    ]
    declined = _owner_declined_days_ago(client_id)
    if declined is not None:
        notes.insert(
            0, {"note": f"Owner declined a proposed reminder {declined} days ago", "source": "owner", "at": ""}
        )
    return {
        "client_id": client_id,
        "name": client["name"],
        "contact_name": client["contact_name"],
        "email": client["email"],
        "ap_email": client["ap_email"],
        "terms_days": client["terms_days"],
        "late_fee_clause": bool(client["late_fee_clause"]),
        "late_fee_pct_month": client["late_fee_pct_month"],
        "relationship_notes": client["notes"],
        "avg_days_to_pay": avg_days,
        "paid_invoices": len(paid),
        "reminders_sent_total": store.count_reminders_for_client(client_id),
        "open_invoices": open_invoices,
        "latest_email": (
            {
                "date": latest_email["date"],
                "days_ago": config.days_between(latest_email["date"], today),
                "subject": latest_email["subject"],
                "excerpt": (latest_email["body"] or "")[:240],
            }
            if latest_email
            else None
        ),
        "notes": notes,
        "owner_declined_days_ago": declined,
    }


# ----------------------------------------------------------------------------- reconciler tools
@tool
def list_open_invoices() -> dict[str, Any]:
    """List every invoice with an outstanding balance (status open or partial).

    Use this first to know what can be matched against deposits. Returns invoices with
    client, amount, amount_paid, outstanding, due date, days_overdue and payment link.
    """
    rows = [_invoice_view(i) for i in get_store().list_open_invoices()]
    return {"today": config.today().isoformat(), "count": len(rows), "invoices": rows}


@tool
def list_unmatched_deposits() -> dict[str, Any]:
    """List bank deposits not yet applied to an invoice, each with ranked match candidates.

    Candidate kinds: exact (amount equals an invoice balance), fee_adjusted (invoice minus a
    known card/wire fee; the fee is given), combined (covers several invoices of one client),
    partial (less than the balance; confidence is higher when the memo names the client).
    Apply high-confidence candidates with match_deposit_to_invoice or record_partial_payment.
    If no candidate is plausible, call flag_deposit_for_review instead of guessing.
    """
    store = get_store()
    open_invoices = store.list_open_invoices()
    names = {c["id"]: c["name"] for c in store.list_clients()}
    out = []
    for dep in store.list_unmatched_deposits():
        remaining = round(dep["amount"] - sum(a["amount"] for a in dep["allocations"]), 2)
        candidates = match_deposit(remaining, open_invoices, memo=dep["memo"], client_names=names)
        out.append(
            {
                "deposit_id": dep["id"],
                "date": dep["date"],
                "amount": dep["amount"],
                "unallocated": remaining,
                "memo": dep["memo"],
                "candidates": [c.to_dict() for c in candidates[:4]],
            }
        )
    return {"count": len(out), "deposits": out}


@tool
def list_recent_client_emails(days: int = 14) -> dict[str, Any]:
    """Return client emails from the last N days, newest first, with a payment-related flag.

    Use it to learn what changes the plan: "we paid on Tuesday", "please resend the invoice",
    "can we push to the 30th", out-of-office replies. Emails with client_id null are not from
    a known client (inquiries, newsletters) and can be ignored for collections.

    Args:
        days: how far back to look (default 14).
    """
    since = config.today() - timedelta(days=days)
    rows = []
    for e in get_store().list_emails(since):
        rows.append(
            {
                "email_id": e["id"],
                "date": e["date"],
                "days_ago": config.days_between(e["date"], config.today()),
                "from": f"{e['from_name']} <{e['from_email']}>",
                "client_id": e["client_id"],
                "client": _client_name(e["client_id"]) if e["client_id"] else None,
                "subject": e["subject"],
                "body": e["body"],
                "about_payment": email_mentions_payment(e["subject"], e["body"]),
            }
        )
    return {"since": since.isoformat(), "count": len(rows), "emails": rows}


@tool
def match_deposit_to_invoice(deposit_id: str, invoice_id: str, note: str = "") -> dict[str, Any]:
    """Apply a deposit to an invoice in full, marking the invoice paid.

    Use for exact and fee_adjusted candidates (mention the fee in the note, e.g. "less $30
    wire fee"). For a combined deposit call this once per invoice. Returns the updated invoice.

    Args:
        deposit_id: bank transaction id, e.g. TX-2001.
        invoice_id: invoice id, e.g. INV-1044.
        note: short explanation stored on the match.
    """
    store = get_store()
    dep = store.get_transaction(deposit_id)
    inv = store.get_invoice(invoice_id)
    if dep is None or inv is None:
        return {"error": f"unknown deposit {deposit_id} or invoice {invoice_id}"}
    outstanding = round(inv["amount"] - inv["amount_paid"], 2)
    unallocated = round(dep["amount"] - sum(a["amount"] for a in dep["allocations"]), 2)
    applied = min(outstanding, unallocated)
    fee = round(outstanding - applied, 2)  # shortfall is a processor/bank fee, not a receivable
    updated = store.record_payment(invoice_id, applied, deposit_id, config.today(), note or None, fee=fee)
    return {
        "ok": True,
        "invoice": _invoice_view(updated),
        "applied": applied,
        "fee_absorbed": fee,
        "deposit_status": (store.get_transaction(deposit_id) or {}).get("status"),
    }


@tool
def record_partial_payment(deposit_id: str, invoice_id: str, amount: float, note: str = "") -> dict[str, Any]:
    """Apply part of a deposit to an invoice, leaving the invoice partially paid.

    Use when a client sends less than the balance (e.g. "sending half now"). Returns the
    invoice with its new outstanding amount.

    Args:
        deposit_id: bank transaction id.
        invoice_id: invoice id.
        amount: the amount to apply (positive).
        note: short explanation, e.g. "client said rest at month end".
    """
    store = get_store()
    if store.get_transaction(deposit_id) is None or store.get_invoice(invoice_id) is None:
        return {"error": f"unknown deposit {deposit_id} or invoice {invoice_id}"}
    updated = store.record_payment(invoice_id, float(amount), deposit_id, config.today(), note or None)
    return {"ok": True, "invoice": _invoice_view(updated)}


@tool
def flag_deposit_for_review(deposit_id: str, reason: str) -> dict[str, Any]:
    """Queue an unexplained deposit for the owner (low-severity 'review' decision) instead of guessing.

    The owner will match it to an invoice or mark it as other income in the approvals UI.
    Returns the decision id. Do not call twice for the same deposit.

    Args:
        deposit_id: bank transaction id.
        reason: why it could not be matched, including the memo text.
    """
    store = get_store()
    dep = store.get_transaction(deposit_id)
    if dep is None:
        return {"error": f"unknown deposit {deposit_id}"}
    key = f"flag_deposit_for_review:{deposit_id}"
    existing = store.find_pending_decision(key)
    if existing:
        return {"ok": True, "decision_id": existing["id"], "duplicate": True}
    decision = store.create_decision(
        kind="review",
        agent="reconciler",
        tool_name="flag_deposit_for_review",
        tool_input={
            "deposit_id": deposit_id,
            "reason": reason,
            "amount": dep["amount"],
            "memo": dep["memo"],
            "date": dep["date"],
        },
        summary=f"Unknown deposit ${dep['amount']:,.2f} on {dep['date']} ({dep['memo']})",
        severity="low",
        dedupe_key=key,
        cycle_id=current_cycle_id(),
    )
    store.update_transaction(deposit_id, status="flagged", note=reason)
    return {"ok": True, "decision_id": decision["id"]}


@tool
def note_client_context(client_id: str, note: str) -> dict[str, Any]:
    """Record a short fact about a client that later agents and future weeks should know.

    Examples: "Wrote Sep 5: processing INV-1034 this week", "AP address changed to ap@...".
    The collector sees these notes in get_client_profile. Keep notes under 200 characters.

    Args:
        client_id: client id, e.g. C-ORBIT.
        note: the fact to remember.
    """
    store = get_store()
    if store.get_client(client_id) is None:
        return {"error": f"unknown client {client_id}"}
    store.add_client_note(client_id, note[:200], source="reconciler", cycle_id=current_cycle_id())
    return {"ok": True}


# ----------------------------------------------------------------------------- collector tools
@tool
def list_overdue_invoices() -> dict[str, Any]:
    """List invoices past due with a recommended follow-up tier and the reason for it.

    Tiers: 0 hold (do not chase this week; the reason says why), 1 gentle (1-14 days),
    2 firm with payment link (15-30), 3 final notice citing the late fee when the contract
    has one (31-60), 4 escalate (61+; recommend a call, payment plan or write-off).
    The recommendation already accounts for prior reminders, recent client emails and
    owner declines. Follow it unless get_client_profile shows a reason not to.
    """
    store = get_store()
    today = config.today()
    rows = []
    for inv in store.list_overdue_invoices(today):
        client = store.get_client(inv["client_id"]) or {}
        reminders = store.list_reminders(inv["id"])
        last_reminder = reminders[-1]["sent_at"] if reminders else None
        # most recent client email that is about paying/resending this invoice (last 14 days), else the latest one
        recent = store.list_emails_for_client(inv["client_id"], today - timedelta(days=14))
        payment_emails = [e for e in recent if email_mentions_payment(e["subject"], e["body"], inv["id"])]
        email = payment_emails[0] if payment_emails else (recent[0] if recent else None)
        view = _invoice_view(inv)
        decision = select_tier(
            view["days_overdue"],
            prior_reminders=len(reminders),
            days_since_last_reminder=config.days_between(last_reminder, today) if last_reminder else None,
            days_since_client_email=config.days_between(email["date"], today) if email else None,
            client_email_about_payment=bool(payment_emails),
            days_since_owner_declined=_owner_declined_days_ago(inv["client_id"]),
            gentle_client="gentle" in (client.get("notes") or "").lower(),
            late_fee_clause=bool(client.get("late_fee_clause")),
        )
        rows.append(
            {
                **view,
                "prior_reminders": [{"tier": r["tier"], "sent_at": r["sent_at"]} for r in reminders],
                "latest_client_email": (
                    {"date": email["date"], "subject": email["subject"], "excerpt": (email["body"] or "")[:200]}
                    if email
                    else None
                ),
                "recommended_tier": decision.tier,
                "recommended_tier_label": decision.label,
                "recommendation_reason": decision.reason,
                "contact_email": client.get("email"),
                "ap_email": client.get("ap_email"),
            }
        )
    rows.sort(key=lambda r: -r["days_overdue"])
    return {"today": today.isoformat(), "count": len(rows), "invoices": rows}


@tool
def get_client_profile(client_id: str) -> dict[str, Any]:
    """Return a client's payment history and relationship context before drafting anything.

    Includes average days to pay, reminders sent, relationship notes, the latest email from
    them, notes left by the reconciler this week, and whether the owner declined a reminder
    for this client recently (if so, do not propose the same reminder again).

    Args:
        client_id: client id, e.g. C-ACME.
    """
    return _client_profile(client_id)


@tool
def get_contract_terms(invoice_id: str) -> dict[str, Any]:
    """Return payment terms and late-fee clause for an invoice, with the fee accrued so far.

    Use before a tier 3 final notice so the amount quoted is right. Returns terms_days,
    late_fee_clause, late_fee_pct_month, days_overdue, accrued_late_fee and total_with_fee.

    Args:
        invoice_id: invoice id, e.g. INV-1038.
    """
    store = get_store()
    inv = store.get_invoice(invoice_id)
    if inv is None:
        return {"error": f"unknown invoice {invoice_id}"}
    client = store.get_client(inv["client_id"]) or {}
    view = _invoice_view(inv)
    fee = late_fee_amount(view["outstanding"], client.get("late_fee_pct_month"), view["days_overdue"])
    return {
        "invoice_id": invoice_id,
        "client": client.get("name"),
        "terms": f"net {client.get('terms_days')}",
        "terms_days": client.get("terms_days"),
        "late_fee_clause": bool(client.get("late_fee_clause")),
        "late_fee_pct_month": client.get("late_fee_pct_month"),
        "days_overdue": view["days_overdue"],
        "outstanding": view["outstanding"],
        "accrued_late_fee": fee,
        "total_with_fee": round(view["outstanding"] + fee, 2),
        "payment_link": inv["payment_link"],
    }


@tool
def draft_followup(invoice_id: str, tier: int, body: str, subject: str = "") -> dict[str, Any]:
    """Store a drafted follow-up email for an invoice (does not send anything).

    Always draft before calling send_client_email. The body must include the contact's
    first name, the invoice number, the amount outstanding, the due date and the payment
    link. Returns the draft id and the recommended recipient address.

    Args:
        invoice_id: invoice id.
        tier: 1 gentle, 2 firm, 3 final notice, 4 escalation.
        body: full plain-text email body.
        subject: email subject; a sensible default is generated if empty.
    """
    store = get_store()
    inv = store.get_invoice(invoice_id)
    if inv is None:
        return {"error": f"unknown invoice {invoice_id}"}
    client = store.get_client(inv["client_id"]) or {}
    label = TIER_LABELS.get(int(tier), "reminder")
    subject = subject or {
        1: f"Quick reminder: {invoice_id}",
        2: f"Second notice: {invoice_id} is past due",
        3: f"Final notice: {invoice_id}",
        4: f"Regarding {invoice_id}",
    }.get(int(tier), f"Regarding {invoice_id}")
    draft = store.save_draft(invoice_id, int(tier), subject, body, current_cycle_id())
    return {
        "ok": True,
        "draft_id": draft["id"],
        "tier": int(tier),
        "tier_label": label,
        "subject": subject,
        "suggested_to": client.get("ap_email") or client.get("email"),
        "contact_email": client.get("email"),
        "ap_email": client.get("ap_email"),
    }


@tool
def send_client_email(invoice_id: str, to: str, subject: str, body: str) -> dict[str, Any]:
    """Send an email to a client about an invoice. GATED: the owner approves before it goes out.

    Use for reminders and for resending an invoice to a new address. When you call this the
    message is queued for approval and you receive a decision id; do not call it again for
    the same invoice in this run. Returns the sent message id when executed after approval.

    Args:
        invoice_id: invoice the email is about.
        to: recipient address.
        subject: subject line.
        body: plain-text body.
    """
    store = get_store()
    inv = store.get_invoice(invoice_id)
    draft = store.latest_draft(invoice_id)
    tier = draft["tier"] if draft else 0
    msg = OutboxEmailSender(store).send(to, subject, body, invoice_id=invoice_id, decision_id=None)
    if inv is not None and tier:
        store.add_reminder(invoice_id, tier, config.today().isoformat(), subject, current_cycle_id())
    return {"ok": True, "message_id": msg.get("id"), "to": to, "subject": subject, "tier": tier}


@tool
def offer_payment_plan(invoice_id: str, terms: str) -> dict[str, Any]:
    """Offer a client a payment plan or extension for an invoice. GATED: owner approves first.

    Use for 61+ day balances where the client has engaged, or when they ask for more time.
    Returns a decision id when queued; when executed, records the plan on the client.

    Args:
        invoice_id: invoice id.
        terms: the proposed terms, e.g. "two payments of $475 on Sep 30 and Oct 31".
    """
    store = get_store()
    inv = store.get_invoice(invoice_id)
    if inv is None:
        return {"error": f"unknown invoice {invoice_id}"}
    store.add_client_note(
        inv["client_id"], f"Payment plan offered for {invoice_id}: {terms}", "collector", current_cycle_id()
    )
    return {"ok": True, "invoice_id": invoice_id, "terms": terms}


@tool
def propose_write_off(invoice_id: str, reason: str) -> dict[str, Any]:
    """Propose writing off an invoice as uncollectible. GATED: owner approves first.

    Use only for 61+ day balances with no engagement after multiple reminders. Returns a
    decision id when queued; when executed, marks the invoice written_off.

    Args:
        invoice_id: invoice id.
        reason: why collection is unlikely.
    """
    store = get_store()
    inv = store.get_invoice(invoice_id)
    if inv is None:
        return {"error": f"unknown invoice {invoice_id}"}
    store.update_invoice(invoice_id, status="written_off")
    store.add_client_note(inv["client_id"], f"{invoice_id} written off: {reason}", "collector", current_cycle_id())
    return {"ok": True, "invoice_id": invoice_id, "status": "written_off"}


# ----------------------------------------------------------------------------- bookkeeper tools
@tool
def list_uncategorized_expenses() -> dict[str, Any]:
    """List expenses without a category, each with a suggested category and candidate receipts.

    Also returns the chart of accounts. For each expense: call categorize_expense with the
    suggested (or a better) category id, and match_receipt when a candidate receipt has
    confidence >= 0.8. Transfers to savings are not expenses and are already excluded.
    """
    store = get_store()
    chart = load_chart_of_accounts()
    receipts = store.list_receipts(unmatched_only=True)
    rows = []
    for e in store.list_uncategorized_expenses():
        rows.append(
            {
                "expense_id": e["id"],
                "date": e["date"],
                "amount": abs(e["amount"]),
                "merchant": e["merchant"],
                "memo": e["memo"],
                "age_days": config.days_between(e["date"], config.today()),
                "has_receipt": bool(e["receipt_id"]),
                "suggested_category": suggest_category(e, chart),
                "candidate_receipts": suggest_receipts(e, receipts),
            }
        )
    categories = [
        {"id": c["id"], "label": c["label"]} for c in chart.get("expense_categories", []) + chart.get("non_pnl", [])
    ]
    return {"count": len(rows), "expenses": rows, "categories": categories}


@tool
def categorize_expense(expense_id: str, category: str) -> dict[str, Any]:
    """Assign a chart-of-accounts category to an expense. Routine; no approval needed.

    Args:
        expense_id: bank transaction id, e.g. TX-2105.
        category: category id from the chart of accounts, e.g. equipment_hardware.
    """
    store = get_store()
    tx = store.get_transaction(expense_id)
    if tx is None:
        return {"error": f"unknown expense {expense_id}"}
    chart = load_chart_of_accounts()
    valid = {c["id"] for c in chart.get("expense_categories", []) + chart.get("non_pnl", [])}
    if category not in valid:
        return {"error": f"unknown category {category}", "valid_categories": sorted(valid)}
    store.update_transaction(expense_id, category=category, status="categorized")
    return {"ok": True, "expense_id": expense_id, "category": category}


def missing_receipts(older_than_days: int = 7) -> list[dict[str, Any]]:
    """Expenses older than N days with no receipt attached and no confidently matching receipt on file."""
    store = get_store()
    receipts = store.list_receipts(unmatched_only=True)
    out = []
    for e in store.list_expenses_missing_receipts(config.today(), older_than_days):
        cands = suggest_receipts(e, receipts)
        if cands and cands[0]["confidence"] >= 0.8:
            continue  # a receipt exists; it just needs match_receipt
        out.append(e)
    return out


@tool
def list_expenses_missing_receipts(older_than_days: int = 7) -> dict[str, Any]:
    """List expenses with no receipt on file that are older than N days.

    Expenses that have an unattached receipt available are not listed (attach those with
    match_receipt instead). For each one returned, create_todo("Find receipt: <merchant>
    $<amount> (<date>)") unless a matching open to-do already exists (create_todo
    de-duplicates by title). Recent expenses (younger than the threshold) are excluded on purpose.

    Args:
        older_than_days: age threshold in days (default 7).
    """
    rows = [
        {
            "expense_id": e["id"],
            "date": e["date"],
            "amount": abs(e["amount"]),
            "merchant": e["merchant"],
            "memo": e["memo"],
            "age_days": e["age_days"],
            "category": e["category"],
        }
        for e in missing_receipts(older_than_days)
    ]
    return {"count": len(rows), "expenses": rows}


@tool
def create_todo(title: str, due_date: str = "", note: str = "") -> dict[str, Any]:
    """Create a to-do for the owner (e.g. "Find receipt: Apple Store $1,299.00 (2026-08-29)").

    Routine; no approval needed. De-duplicated by title: calling twice returns the existing
    to-do with duplicate=true.

    Args:
        title: short imperative title.
        due_date: ISO date; defaults to 7 days from today.
        note: optional detail (expense id, why it matters).
    """
    due = due_date or (config.today() + timedelta(days=7)).isoformat()
    todo = get_store().add_todo(title, due, note or None, current_cycle_id(), dedupe_key=title.strip().lower())
    return {
        "ok": True,
        "todo_id": todo["id"],
        "title": todo["title"],
        "due_date": todo["due_date"],
        "duplicate": bool(todo.get("duplicate")),
    }


@tool
def match_receipt(expense_id: str, receipt_id: str) -> dict[str, Any]:
    """Attach a receipt to an expense. Routine; use for candidates with confidence >= 0.8.

    Args:
        expense_id: bank transaction id.
        receipt_id: receipt id, e.g. RCPT-503.
    """
    store = get_store()
    tx, rc = store.get_transaction(expense_id), store.get_receipt(receipt_id)
    if tx is None or rc is None:
        return {"error": f"unknown expense {expense_id} or receipt {receipt_id}"}
    if rc.get("expense_id") and rc["expense_id"] != expense_id:
        return {"error": f"receipt {receipt_id} already attached to {rc['expense_id']}"}
    store.link_receipt(expense_id, receipt_id)
    return {"ok": True, "expense_id": expense_id, "receipt_id": receipt_id}


# ----------------------------------------------------------------------------- reporter tools
def week_summary() -> dict[str, Any]:
    """Deterministic numbers for the report (shared by the reporter tool and the fallback report)."""
    store = get_store()
    today = config.today()
    start = config.week_start(today)
    cycle = current_cycle_id()
    actions = store.list_actions(limit=500, cycle_id=cycle, kinds=("write",)) if cycle else []
    by_tool: dict[str, int] = {}
    for a in actions:
        by_tool[a["tool_name"]] = by_tool.get(a["tool_name"], 0) + 1
    pending = store.list_pending_decisions()
    return {
        "period_start": start.isoformat(),
        "period_end": today.isoformat(),
        "cash_collected_this_week": store.cash_collected(start, today),
        "aging": store.aging_buckets(today),
        "reconciliation": {
            "deposits_matched": by_tool.get("match_deposit_to_invoice", 0),
            "partial_payments_recorded": by_tool.get("record_partial_payment", 0),
            "deposits_flagged_for_review": by_tool.get("flag_deposit_for_review", 0),
        },
        "books": {
            "expenses_categorized": by_tool.get("categorize_expense", 0),
            "receipts_matched": by_tool.get("match_receipt", 0),
            "expenses_missing_receipts": len(missing_receipts(7)),
            "todos_created": by_tool.get("create_todo", 0),
            "uncategorized_expenses": len(store.list_uncategorized_expenses()),
        },
        "pending_decisions": [
            {
                "id": d["id"],
                "kind": d["kind"],
                "summary": d["summary"],
                "client_id": d.get("client_id"),
                "invoice_id": d.get("invoice_id"),
                "tier": d.get("tier"),
                "tool_name": d["tool_name"],
            }
            for d in pending
        ],
        "open_todos": [{"title": t["title"], "due_date": t["due_date"]} for t in store.list_todos()],
    }


@tool
def get_week_summary() -> dict[str, Any]:
    """Return this week's numbers: cash collected, receivables aging buckets with invoice chips,
    reconciliation counts, books health (uncategorized, missing receipts, to-dos) and the
    pending decisions. Use it as the factual basis for the weekly close report.
    """
    return week_summary()


@tool
def list_pending_decisions() -> dict[str, Any]:
    """List proposals awaiting the owner: gated emails, payment plans, write-offs and deposits to review.

    Each entry has id, kind (approval or review), summary, client, invoice, tier and the tool
    that will run on approval.
    """
    rows = [
        {
            "id": d["id"],
            "kind": d["kind"],
            "agent": d["agent"],
            "summary": d["summary"],
            "severity": d["severity"],
            "client": _client_name(d["client_id"]) if d.get("client_id") else None,
            "invoice_id": d.get("invoice_id"),
            "tier": d.get("tier"),
            "tool_name": d["tool_name"],
            "created_at": d["created_at"],
        }
        for d in get_store().list_pending_decisions()
    ]
    return {"count": len(rows), "decisions": rows}


@tool
def list_actions_this_week() -> dict[str, Any]:
    """List the write actions the agents took this cycle (matches, categorizations, to-dos, notes),
    grouped by agent. Use it for the "what I did while you were away" part of the report.
    """
    store = get_store()
    cycle = current_cycle_id()
    actions = store.list_actions(limit=500, cycle_id=cycle, kinds=("write", "approved")) if cycle else []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for a in actions:
        grouped.setdefault(a["agent"], []).append(
            {"tool": a["tool_name"], "summary": a["summary"], "status": a["status"], "at": a["created_at"]}
        )
    return {"cycle_id": cycle, "count": len(actions), "by_agent": grouped}


@tool
def list_todos() -> dict[str, Any]:
    """List the owner's open to-dos (missing receipts, follow-ups) with due dates."""
    return {"todos": get_store().list_todos()}


RECONCILER_TOOLS = [
    list_open_invoices,
    list_unmatched_deposits,
    list_recent_client_emails,
    match_deposit_to_invoice,
    record_partial_payment,
    flag_deposit_for_review,
    note_client_context,
]
COLLECTOR_TOOLS = [
    list_overdue_invoices,
    get_client_profile,
    get_contract_terms,
    draft_followup,
    send_client_email,
    offer_payment_plan,
    propose_write_off,
]
BOOKKEEPER_TOOLS = [
    list_uncategorized_expenses,
    categorize_expense,
    list_expenses_missing_receipts,
    create_todo,
    match_receipt,
]
REPORTER_TOOLS = [get_week_summary, list_pending_decisions, list_actions_this_week]
ASK_TOOLS = [
    list_open_invoices,
    list_overdue_invoices,
    get_client_profile,
    get_week_summary,
    list_pending_decisions,
    list_actions_this_week,
    list_todos,
    list_recent_client_emails,
]
