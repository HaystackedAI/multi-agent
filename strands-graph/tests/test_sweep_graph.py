"""End-to-end weekly close through the Strands Graph with a scripted model per node."""

from __future__ import annotations

from scripted_model import ScriptedModel, call, text

from chaser import service
from chaser.models import WeeklyCloseReport
from chaser.store import Store

ACME_BODY = "Hi Dana,\n\nINV-1042 ($2,400.00) was due Aug 20. Pay here: https://pay.chaser.example/INV-1042\n\nThanks"
PIXEL_BODY = "Hi Tomas,\n\nFinal notice for INV-1038 ($3,200.00, due Aug 2). Late fee accrued: $65.60. Link: ..."
NORTH_BODY = "Hi Elaine,\n\nResending INV-1040 ($1,675.00, due Aug 27) to your new AP address as requested. Link: ..."
BLUE_BODY = "Hi Marcus,\n\nIn case it slipped through: INV-1045 ($1,850.00) was due Sep 3. Link: ..."

REPORT = dict(
    period_start="2026-09-07",
    period_end="2026-09-12",
    cash_collected_this_week=3640.0,
    outstanding={"current": 4100.0, "d1_30": 6525.0, "d31_60": 3200.0, "d61_plus": 950.0},
    reconciliation={
        "deposits_matched": 2,
        "partial_payments_recorded": 1,
        "deposits_flagged_for_review": 1,
        "notes": ["TX-2002 matched to INV-1043 less $30 wire fee"],
    },
    drafts_awaiting_approval=[
        {"client": "Acme Robotics", "invoice_id": "INV-1042", "tier": 2, "amount": 2400.0, "action": "send reminder"},
    ],
    books={"expenses_categorized": 2, "receipts_matched": 1, "expenses_missing_receipts": 2, "todos_created": 1},
    top_things_to_know=[
        "Orbital Labs INV-1034 is 67 days overdue",
        "Pixel Forge is at 41 days",
        "$500 Zelle needs review",
    ],
    narrative="Collected $3,640 this week. Four proposals are waiting for you.",
)


def demo_scripts() -> dict[str, ScriptedModel]:
    return {
        "reconciler": ScriptedModel(
            [
                call("list_open_invoices"),
                call("list_unmatched_deposits"),
                call("list_recent_client_emails", days=14),
                [  # several tool uses in one assistant turn
                    call("match_deposit_to_invoice", deposit_id="TX-2001", invoice_id="INV-1044", note="check 2231"),
                    call(
                        "match_deposit_to_invoice",
                        deposit_id="TX-2002",
                        invoice_id="INV-1043",
                        note="less $30 wire fee",
                    ),
                    call(
                        "record_partial_payment",
                        deposit_id="TX-2003",
                        invoice_id="INV-1041",
                        amount=600.0,
                        note="half now",
                    ),
                    call(
                        "flag_deposit_for_review", deposit_id="TX-2004", reason="ZELLE J. PARK matches no open invoice"
                    ),
                ],
                call("note_client_context", client_id="C-ORBIT", note="Sep 5 email: processing INV-1034 this week"),
                call(
                    "note_client_context",
                    client_id="C-NORTH",
                    note="Sep 10 email: resend INV-1040 to ap@northwinddental.example",
                ),
                text(
                    "Matched TX-2001 to INV-1044 and TX-2002 to INV-1043 (less $30 wire fee); recorded $600 partial on "
                    "INV-1041; flagged TX-2004 for review. Orbital says processing this week; "
                    "Northwind wants INV-1040 resent."
                ),
            ]
        ),
        "collector": ScriptedModel(
            [
                call("list_overdue_invoices"),
                call("get_client_profile", client_id="C-ACME"),
                call("get_contract_terms", invoice_id="INV-1038"),
                call("draft_followup", invoice_id="INV-1045", tier=1, body=BLUE_BODY),
                call(
                    "send_client_email",
                    invoice_id="INV-1045",
                    to="marcus@bluefinmedia.example",
                    subject="Quick reminder: INV-1045",
                    body=BLUE_BODY,
                ),
                call(
                    "draft_followup", invoice_id="INV-1042", tier=2, body=ACME_BODY, subject="Second notice: INV-1042"
                ),
                call(
                    "send_client_email",
                    invoice_id="INV-1042",
                    to="ap@acmerobotics.example",
                    subject="Second notice: INV-1042",
                    body=ACME_BODY,
                ),
                call(
                    "draft_followup", invoice_id="INV-1038", tier=3, body=PIXEL_BODY, subject="Final notice: INV-1038"
                ),
                call(
                    "send_client_email",
                    invoice_id="INV-1038",
                    to="tomas@pixelforge.example",
                    subject="Final notice: INV-1038",
                    body=PIXEL_BODY,
                ),
                call(
                    "send_client_email",
                    invoice_id="INV-1040",
                    to="ap@northwinddental.example",
                    subject="Resending INV-1040",
                    body=NORTH_BODY,
                ),
                text(
                    "Proposed: Bluefin tier-1, Acme tier-2, Pixel Forge tier-3 with $65.60 late fee, Northwind resend. "
                    "Holding Orbital (client wrote Sep 5) and Harbor (paid half, gentle)."
                ),
            ]
        ),
        "bookkeeper": ScriptedModel(
            [
                call("list_uncategorized_expenses"),
                [
                    call("categorize_expense", expense_id="TX-2105", category="equipment_hardware"),
                    call("categorize_expense", expense_id="TX-2103", category="rent_coworking"),
                    call("match_receipt", expense_id="TX-2103", receipt_id="RCPT-503"),
                ],
                call("list_expenses_missing_receipts", older_than_days=7),
                call("create_todo", title="Find receipt: Apple Store $1,299.00 (2026-08-29)", note="TX-2105"),
                text("Categorized 2 expenses, matched 1 receipt, created 1 to-do for the laptop receipt."),
            ]
        ),
        "reporter": ScriptedModel(
            [
                call("get_week_summary"),
                call("list_pending_decisions"),
                call("list_actions_this_week"),
                text("Weekly close: collected $3,640; four items need approval."),
                call("WeeklyCloseReport", **REPORT),  # structured output conversion step
            ]
        ),
    }


def test_weekly_close_graph_end_to_end(store: Store) -> None:
    scripts = demo_scripts()
    result = service.run_sweep(lambda node: scripts[node], store=store)

    assert result["ok"] is True, result.get("error")
    order = result["execution_order"]
    assert order[0] == "reconciler" and order[-1] == "reporter"
    assert set(order) == {"reconciler", "bookkeeper", "collector", "reporter"}

    # reconciler outcomes
    assert store.get_invoice("INV-1044")["status"] == "paid"
    assert store.get_invoice("INV-1043")["status"] == "paid"
    assert store.get_invoice("INV-1041")["status"] == "partial"
    assert store.get_transaction("TX-2004")["status"] == "flagged"

    # decisions: 1 review + 4 approvals, each gated call denied in-run (nothing sent)
    pending = result["pending_decisions"]
    kinds = sorted(d["kind"] for d in pending)
    assert kinds == ["approval", "approval", "approval", "approval", "review"]
    by_invoice = {d["invoice_id"]: d for d in pending if d["kind"] == "approval"}
    assert set(by_invoice) == {"INV-1045", "INV-1042", "INV-1038", "INV-1040"}
    assert by_invoice["INV-1042"]["tier"] == 2 and by_invoice["INV-1038"]["tier"] == 3
    assert by_invoice["INV-1040"]["summary"].startswith("Resend INV-1040 to ap@northwinddental.example")
    assert store.list_outbox() == []

    # audit log carries the agent column for every node
    agents = {a["agent"] for a in store.list_actions(limit=500, cycle_id=result["cycle_id"])}
    assert agents == {"reconciler", "collector", "bookkeeper", "reporter"}
    write_actions = [a for a in result["actions_taken"] if a["kind"] == "write"]
    assert len(write_actions) >= 9  # 2 matches + partial + flag + 2 notes + 2 categorize + receipt + todo

    # structured report came from the reporter node via structured output
    report = result["report"]
    WeeklyCloseReport(**{k: v for k, v in report.items() if not k.startswith(("node_", "execution_", "graph_"))})
    assert report["cash_collected_this_week"] == 3640.0
    assert report["narrative"].startswith("Collected $3,640")
    assert report["execution_order"] == order
    assert "Orbital" in report["node_summaries"]["collector"]
    assert store.last_report()["cash_collected_this_week"] == 3640.0
    assert store.last_cycle()["status"] == "completed"

    # every scripted turn was consumed (each node ran exactly its script)
    assert all(m.turns == [] for m in scripts.values())

    # status() reflects the sweep
    st = service.status(store)
    assert st["counts"]["decisions_pending"] == 5 and st["last_sweep_status"] == "completed"


def test_conditional_edge_skips_collector_when_nothing_overdue(store: Store) -> None:
    # seed variant: nothing is overdue -> reconciler -> bookkeeper -> reporter only
    for inv in store.list_overdue_invoices(service.config.today()):
        store.update_invoice(inv["id"], status="paid", amount_paid=inv["amount"], paid_on="2026-09-01")
    assert store.count_overdue_open_invoices(service.config.today()) == 0

    scripts = {
        "reconciler": ScriptedModel([call("list_unmatched_deposits"), text("Nothing new.")]),
        "collector": ScriptedModel([text("SHOULD NOT RUN")]),
        "bookkeeper": ScriptedModel([call("list_expenses_missing_receipts", older_than_days=7), text("Books fine.")]),
        "reporter": ScriptedModel([call("get_week_summary"), text("Quiet week.")]),  # no structured turn -> fallback
    }
    result = service.run_sweep(lambda node: scripts[node], store=store)
    assert result["ok"] is True
    assert result["execution_order"] == ["reconciler", "bookkeeper", "reporter"]
    assert "collector" not in result["node_summaries"]
    assert scripts["collector"].turns == [text("SHOULD NOT RUN")]
    # fallback report is deterministic from the store
    assert result["report"]["outstanding"]["d1_30"] == 0.0
    assert result["report"]["narrative"] == "Quiet week."


def test_fallback_report_when_structured_output_fails(store: Store) -> None:
    scripts = {
        "reconciler": ScriptedModel([text("ok")]),
        "collector": ScriptedModel([text("ok")]),
        "bookkeeper": ScriptedModel([text("ok")]),
        # conversion turn returns an invalid schema; the fallback kicks in
        "reporter": ScriptedModel([text("summary"), text("still not structured")]),
    }
    result = service.run_sweep(lambda node: scripts[node], store=store)
    assert result["ok"]
    assert result["report"]["outstanding"]["d61_plus"] == 950.0
    assert any("Orbital Labs INV-1034" in t for t in result["report"]["top_things_to_know"])


def test_run_sweep_survives_node_failure(store: Store) -> None:
    class Exploding(ScriptedModel):
        async def stream(self, *a, **k):  # type: ignore[override]
            raise RuntimeError("model down")
            yield  # pragma: no cover

    scripts = {n: Exploding() for n in ("reconciler", "collector", "bookkeeper", "reporter")}
    result = service.run_sweep(lambda node: scripts[node], store=store)
    assert result["ok"] is False and "model down" in result["error"]
    assert store.last_cycle()["status"] == "failed"
    assert not service.SWEEP_LOCK.locked()
