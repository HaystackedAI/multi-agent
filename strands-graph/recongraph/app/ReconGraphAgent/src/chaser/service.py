"""Core orchestration: run_sweep, decide, ask, status, seed.

``main.py`` (AgentCore), ``app/server.py`` (web) and ``chaser.cli`` are thin wrappers over
these functions. Sweeps are serialized with a process-wide lock.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any

from strands.models.model import Model

from . import config, prompts
from .agents import ModelFactory, build_graph, default_model_factory, make_ask_agent, make_execution_agent
from .connectors import JsonDataConnector
from .context import get_store, set_cycle_id
from .models import WeeklyCloseReport
from .sessions import ASK_SESSION_ID, make_session_manager
from .store import Store
from .tools import missing_receipts, week_summary

logger = logging.getLogger(__name__)

SWEEP_LOCK = threading.Lock()
APPROVE_WORDS = {"y", "yes", "approve", "approved", "ok", "send", "true"}
OTHER_INCOME_WORDS = {"other", "other income", "other_income", "income"}


# ----------------------------------------------------------------------------- seeding
def seed(store: Store | None = None, data_dir: str | None = None) -> dict[str, int]:
    """(Re)load the demo dataset from ``data/*.json`` into the store."""
    store = store or get_store()
    src = JsonDataConnector(data_dir) if data_dir else JsonDataConnector()
    store.seed(
        clients=src.fetch_clients(),
        invoices=src.fetch_invoices(),
        transactions=src.fetch_transactions(),
        receipts=src.fetch_receipts(),
        emails=src.fetch_messages(),
    )
    return store.counts()


# ----------------------------------------------------------------------------- helpers
def _node_text(result: Any, node: str) -> str:
    node_result = getattr(result, "results", {}).get(node)
    if node_result is None:
        return ""
    inner = getattr(node_result, "result", node_result)
    try:
        return str(inner)
    except Exception:  # pragma: no cover
        return ""


def fallback_report(narrative: str, today: date) -> WeeklyCloseReport:
    """Deterministic report built from the store when structured output is unavailable."""
    s = week_summary()
    store = get_store()
    aging = s["aging"]
    drafts = []
    for d in s["pending_decisions"]:
        if d["kind"] != "approval":
            continue
        inv = store.get_invoice(d["invoice_id"]) if d.get("invoice_id") else None
        client = store.get_client(d["client_id"]) if d.get("client_id") else None
        drafts.append(
            {
                "client": client["name"] if client else (d.get("client_id") or "unknown"),
                "invoice_id": d.get("invoice_id") or "",
                "tier": d.get("tier") or 0,
                "amount": round(inv["amount"] - inv["amount_paid"], 2) if inv else 0.0,
                "action": d["tool_name"].replace("_", " "),
            }
        )
    top: list[str] = []
    for key in ("d61_plus", "d31_60"):  # oldest buckets first
        for inv in aging[key]["invoices"]:
            top.append(
                f"{inv['client']} {inv['id']} is {inv['days_overdue']} days overdue (${inv['outstanding']:,.2f})"
            )
    flagged = s["reconciliation"]["deposits_flagged_for_review"]
    if flagged:
        top.append(f"{flagged} deposit(s) need your review")
    return WeeklyCloseReport(
        period_start=s["period_start"],
        period_end=s["period_end"],
        cash_collected_this_week=s["cash_collected_this_week"],
        outstanding={k: aging[k]["total"] for k in ("current", "d1_30", "d31_60", "d61_plus")},
        reconciliation={
            "deposits_matched": s["reconciliation"]["deposits_matched"],
            "partial_payments_recorded": s["reconciliation"]["partial_payments_recorded"],
            "deposits_flagged_for_review": flagged,
            "notes": [],
        },
        drafts_awaiting_approval=drafts,
        books={
            "expenses_categorized": s["books"]["expenses_categorized"],
            "receipts_matched": s["books"]["receipts_matched"],
            "expenses_missing_receipts": s["books"]["expenses_missing_receipts"],
            "todos_created": s["books"]["todos_created"],
        },
        top_things_to_know=top[:3],
        narrative=narrative.strip()[:600] or "Weekly close completed.",
    )


def _is_approval(response: Any) -> bool:
    if response is True:
        return True
    return isinstance(response, str) and response.strip().lower() in APPROVE_WORDS


# ----------------------------------------------------------------------------- run_sweep
def run_sweep(
    model_factory: ModelFactory | None = None,
    *,
    store: Store | None = None,
) -> dict[str, Any]:
    """Run one weekly close: reconciler -> (bookkeeper, collector?) -> reporter -> structured report.

    Returns ``{"ok", "cycle_id", "report", "pending_decisions", "actions_taken", "execution_order", "node_summaries"}``.
    Never raises for agent failures; ``ok`` is False and ``error`` is set instead.
    """
    model_factory = model_factory or default_model_factory
    store = store or get_store()
    today = config.today()
    if not SWEEP_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "a sweep is already running"}
    cycle_id = store.start_cycle()
    set_cycle_id(cycle_id)
    try:
        graph, agents, gate = build_graph(store, model_factory)
        task = prompts.SWEEP_TASK.format(today=today.isoformat())
        logger.info("sweep %s: starting graph", cycle_id)
        result = graph(task)
        execution_order = [n.node_id for n in result.execution_order]
        summaries = {node: _node_text(result, node) for node in execution_order}
        logger.info("sweep %s: graph %s, order=%s", cycle_id, result.status, execution_order)

        # Structured report: ask the reporter node (which still holds its context) for the schema.
        narrative = summaries.get("reporter", "")
        report_model: WeeklyCloseReport | None = None
        try:
            reporter = agents["reporter"]
            conv = reporter(prompts.REPORT_CONVERSION_PROMPT, structured_output_model=WeeklyCloseReport)
            if isinstance(conv.structured_output, WeeklyCloseReport):
                report_model = conv.structured_output
        except Exception:
            logger.exception("sweep %s: structured report failed; using fallback", cycle_id)
        if report_model is None:
            report_model = fallback_report(narrative, today)
        report = report_model.model_dump()
        report["node_summaries"] = summaries
        report["execution_order"] = execution_order
        report["graph_status"] = str(result.status.value if hasattr(result.status, "value") else result.status)
        store.save_report(cycle_id, report)

        pending = store.list_pending_decisions()
        actions = store.list_actions(limit=200, cycle_id=cycle_id, kinds=("write", "gated"))
        status = "completed" if not result.failed_nodes else "completed_with_failures"
        store.finish_cycle(cycle_id, status, detail=",".join(execution_order))
        return {
            "ok": True,
            "cycle_id": cycle_id,
            "report": report,
            "pending_decisions": pending,
            "actions_taken": actions,
            "execution_order": execution_order,
            "node_summaries": summaries,
            "decisions_created": gate.created,
        }
    except Exception as exc:
        logger.exception("sweep %s failed", cycle_id)
        store.finish_cycle(cycle_id, "failed", detail=str(exc))
        return {"ok": False, "cycle_id": cycle_id, "error": str(exc)}
    finally:
        set_cycle_id(None)
        SWEEP_LOCK.release()


# ----------------------------------------------------------------------------- decide
def decide(
    decision_id: str,
    response: Any,
    edits: dict[str, Any] | None = None,
    *,
    store: Store | None = None,
    model: Model | None = None,
) -> dict[str, Any]:
    """Resolve a pending decision.

    approval kind: on approval, apply ``edits`` to the stored tool input and execute the tool
    directly through the owning agent (no gate); on denial, record a client note so next
    week's collector sees "owner declined".
    review kind (unknown deposit): ``edits={"invoice_id": ...}`` matches the deposit to that
    invoice; otherwise approval marks it other income; denial dismisses it for now.
    """
    store = store or get_store()
    edits = edits or {}
    decision = store.get_decision(decision_id)
    if decision is None:
        return {"ok": False, "error": f"unknown decision {decision_id}"}
    if decision["status"] != "pending":
        return {"ok": False, "error": f"decision {decision_id} already {decision['status']}", "decision": decision}

    today = config.today()
    approved = _is_approval(response)
    response_text = response if isinstance(response, str) else ("yes" if approved else "no")

    if decision["kind"] == "review":
        return _decide_review(store, decision, approved, response_text, edits, today)

    tool_name = decision["tool_name"]
    if not approved:
        note = f"Owner declined on {today.isoformat()}: {decision['summary']}"
        if isinstance(response, str) and response.strip().lower() not in {"no", "n", "skip", "deny", "false"}:
            note += f" ({response.strip()})"
        if decision.get("client_id"):
            store.add_client_note(decision["client_id"], note, source="owner")
        resolved = store.resolve_decision(
            decision_id, "denied", response_text, edits or None, {"note": note, "decided_on": today.isoformat()}
        )
        store.add_action(
            agent="owner",
            tool_name=tool_name,
            tool_input=decision["tool_input"],
            summary=f"Declined: {decision['summary']}",
            status="denied",
            cycle_id=None,
            kind="decision",
            decision_id=decision_id,
        )
        return {"ok": True, "decision": resolved, "result": {"executed": False}}

    tool_input = dict(decision["tool_input"])
    tool_input.update({k: v for k, v in edits.items() if k in tool_input or k in ("to", "subject", "body", "terms")})
    try:
        agent = make_execution_agent(tool_name, model)
        tool_result = getattr(agent.tool, tool_name)(**tool_input)
    except Exception as exc:
        logger.exception("decide %s: executing %s failed", decision_id, tool_name)
        resolved = store.resolve_decision(decision_id, "failed", response_text, edits or None, {"error": str(exc)})
        return {"ok": False, "error": str(exc), "decision": resolved}

    ok = tool_result.get("status") == "success"
    text = next((b.get("text") for b in tool_result.get("content", []) if "text" in b), "")
    status = "executed" if ok else "failed"
    resolved = store.resolve_decision(decision_id, status, response_text, edits or None, {"result": text})
    store.add_action(
        agent="owner",
        tool_name=tool_name,
        tool_input=tool_input,
        summary=f"Approved and executed: {decision['summary']}",
        status=status,
        cycle_id=None,
        kind="approved",
        decision_id=decision_id,
    )
    if tool_name == "send_client_email" and decision.get("invoice_id"):
        store.update_invoice(decision["invoice_id"], sent_to=tool_input.get("to"))
    return {"ok": ok, "decision": resolved, "result": {"executed": ok, "tool_result": text}}


def _decide_review(
    store: Store, decision: dict[str, Any], approved: bool, response_text: str, edits: dict[str, Any], today: date
) -> dict[str, Any]:
    deposit_id = decision["tool_input"].get("deposit_id")
    dep = store.get_transaction(deposit_id) if deposit_id else None
    if dep is None:
        resolved = store.resolve_decision(
            decision["id"], "failed", response_text, edits or None, {"error": "no deposit"}
        )
        return {"ok": False, "error": f"deposit {deposit_id} not found", "decision": resolved}

    invoice_id = edits.get("invoice_id")
    if invoice_id:
        inv = store.get_invoice(invoice_id)
        if inv is None:
            return {"ok": False, "error": f"unknown invoice {invoice_id}", "decision": decision}
        outstanding = round(inv["amount"] - inv["amount_paid"], 2)
        amount = min(outstanding, dep["amount"])
        store.record_payment(invoice_id, amount, deposit_id, today, note="matched by owner")
        summary = f"Matched {deposit_id} to {invoice_id} (${amount:,.2f})"
        status, outcome = "executed", "matched"
    elif approved or response_text.strip().lower() in OTHER_INCOME_WORDS:
        store.update_transaction(deposit_id, status="other_income", category="other_income", note="owner: other income")
        summary = f"Marked {deposit_id} as other income"
        status, outcome = "executed", "other_income"
    else:
        store.update_transaction(deposit_id, status="unmatched", note="owner skipped review this week")
        summary = f"Left {deposit_id} unmatched for now"
        status, outcome = "denied", "skipped"

    resolved = store.resolve_decision(decision["id"], status, response_text, edits or None, {"outcome": outcome})
    store.add_action(
        agent="owner",
        tool_name="review_deposit",
        tool_input={"deposit_id": deposit_id, **edits},
        summary=summary,
        status=status,
        cycle_id=None,
        kind="decision",
        decision_id=decision["id"],
    )
    return {"ok": True, "decision": resolved, "result": {"outcome": outcome}}


# ----------------------------------------------------------------------------- ask / status
def ask(prompt: str, *, model: Model | None = None, store: Store | None = None, use_session: bool = True) -> str:
    """Answer an owner question with the read-only agent; history persists via the session manager."""
    if store is not None:
        from .context import set_store

        set_store(store)
    session = make_session_manager(ASK_SESSION_ID) if use_session else None
    agent = make_ask_agent(model, session)
    result = agent(prompt)
    return str(result).strip()


def status(store: Store | None = None) -> dict[str, Any]:
    store = store or get_store()
    today = config.today()
    return {
        "today": today.isoformat(),
        "counts": store.counts(),
        "last_sweep_at": store.get_meta("last_sweep_at"),
        "last_sweep_status": store.get_meta("last_sweep_status"),
        "last_cycle": store.last_cycle(),
        "last_report": store.last_report(),
        "sweep_running": SWEEP_LOCK.locked(),
    }


def ui_state(store: Store | None = None) -> dict[str, Any]:
    """Everything the single-page UI needs in one call."""
    store = store or get_store()
    today = config.today()
    clients = {c["id"]: c for c in store.list_clients()}

    def enrich(d: dict[str, Any]) -> dict[str, Any]:
        inv = store.get_invoice(d["invoice_id"]) if d.get("invoice_id") else None
        client = clients.get(d.get("client_id") or "") if d.get("client_id") else None
        out = dict(d)
        out["client"] = client["name"] if client else None
        if inv:
            out["invoice"] = {
                "id": inv["id"],
                "amount": inv["amount"],
                "outstanding": round(inv["amount"] - inv["amount_paid"], 2),
                "due": inv["due"],
                "days_overdue": max(config.days_between(inv["due"], today), 0),
                "description": inv["description"],
                "payment_link": inv["payment_link"],
            }
        if d["kind"] == "review":
            out["open_invoices"] = [
                {
                    "id": i["id"],
                    "client": clients.get(i["client_id"], {}).get("name"),
                    "outstanding": round(i["amount"] - i["amount_paid"], 2),
                }
                for i in store.list_open_invoices()
            ]
        return out

    decisions = store.list_decisions(limit=200)
    return {
        "today": today.isoformat(),
        "sweep_running": SWEEP_LOCK.locked(),
        "last_sweep_at": store.get_meta("last_sweep_at"),
        "last_sweep_status": store.get_meta("last_sweep_status"),
        "pending": [enrich(d) for d in decisions if d["status"] == "pending"],
        "resolved": [enrich(d) for d in decisions if d["status"] != "pending"][:30],
        "aging": store.aging_buckets(today),
        "cash_collected_this_week": store.cash_collected(config.week_start(today), today),
        "report": store.last_report(),
        "todos": store.list_todos(),
        "unmatched_deposits": store.list_unmatched_deposits(),
        "uncategorized_expenses": store.list_uncategorized_expenses(),
        "missing_receipts": missing_receipts(7),
        "activity": store.list_actions(limit=120),
        "progress": store.list_progress(limit=40),
        "outbox": store.list_outbox()[:20],
        "counts": store.counts(),
    }
