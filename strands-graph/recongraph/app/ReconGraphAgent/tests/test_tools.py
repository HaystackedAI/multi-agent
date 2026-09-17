"""Each domain tool against the seeded demo data (called directly, bypassing the model)."""

from chaser import tools
from chaser.store import Store


def test_reconciler_listing_tools(store: Store) -> None:
    inv = tools.list_open_invoices()
    assert inv["count"] == 9 and inv["today"] == "2026-09-12"
    by_id = {i["invoice_id"]: i for i in inv["invoices"]}
    assert by_id["INV-1042"]["days_overdue"] == 23 and by_id["INV-1042"]["client"] == "Acme Robotics"

    deps = tools.list_unmatched_deposits()
    cands = {d["deposit_id"]: d["candidates"] for d in deps["deposits"]}
    assert cands["TX-2001"][0]["kind"] == "exact" and cands["TX-2001"][0]["invoice_ids"] == ["INV-1044"]
    assert cands["TX-2002"][0]["kind"] == "fee_adjusted" and cands["TX-2002"][0]["fee"] == 30.0
    assert cands["TX-2003"][0]["kind"] == "partial" and cands["TX-2003"][0]["invoice_ids"] == ["INV-1041"]
    assert all(c["confidence"] < 0.5 for c in cands["TX-2004"])

    emails = tools.list_recent_client_emails(days=14)
    orbital = next(e for e in emails["emails"] if e["email_id"] == "EM-01")
    assert orbital["about_payment"] is True and orbital["client"] == "Orbital Labs"
    noise = next(e for e in emails["emails"] if e["email_id"] == "EM-04")
    assert noise["client_id"] is None


def test_reconciler_write_tools(store: Store) -> None:
    r = tools.match_deposit_to_invoice("TX-2002", "INV-1043", note="less $30 wire fee")
    assert r["ok"] and r["invoice"]["status"] == "paid" and r["fee_absorbed"] == 30.0 and r["applied"] == 2120.0
    r = tools.record_partial_payment("TX-2003", "INV-1041", 600.0, note="half now")
    assert r["invoice"]["status"] == "partial" and r["invoice"]["outstanding"] == 600.0
    r = tools.flag_deposit_for_review("TX-2004", "ZELLE J. PARK matches no invoice")
    assert r["ok"] and store.get_decision(r["decision_id"])["kind"] == "review"
    again = tools.flag_deposit_for_review("TX-2004", "again")
    assert again["duplicate"] is True and again["decision_id"] == r["decision_id"]
    assert tools.note_client_context("C-ORBIT", "Sep 5: processing INV-1034 this week")["ok"]
    assert store.list_client_notes("C-ORBIT")[0]["source"] == "reconciler"
    assert "error" in tools.match_deposit_to_invoice("TX-NOPE", "INV-1043")


def test_collector_tools_recommend_tiers(store: Store) -> None:
    tools.note_client_context("C-ORBIT", "Sep 5: processing this week")
    overdue = tools.list_overdue_invoices()
    rec = {i["invoice_id"]: i for i in overdue["invoices"]}
    assert rec["INV-1045"]["recommended_tier"] == 1  # Bluefin, 9 days
    assert rec["INV-1042"]["recommended_tier"] == 2  # Acme, 23 days, one prior reminder
    assert rec["INV-1038"]["recommended_tier"] == 3  # Pixel Forge, 41 days, late fee clause
    assert "late fee" in rec["INV-1038"]["recommendation_reason"]
    assert rec["INV-1034"]["recommended_tier"] == 0  # Orbital wrote "processing this week"
    assert "client wrote" in rec["INV-1034"]["recommendation_reason"]
    assert rec["INV-1040"]["recommended_tier"] == 0  # Northwind asked for a resend two days ago
    assert rec["INV-1041"]["recommended_tier"] == 1  # Harbor, 11 days, gentle

    profile = tools.get_client_profile("C-ACME")
    assert profile["avg_days_to_pay"] == 39.0 and profile["reminders_sent_total"] == 2
    assert profile["latest_email"]["subject"] == "Phase 3 kickoff"

    terms = tools.get_contract_terms("INV-1038")
    assert terms["late_fee_clause"] is True and terms["accrued_late_fee"] == 65.6 and terms["total_with_fee"] == 3265.6

    d = tools.draft_followup("INV-1042", 2, "Hi Dana, INV-1042 ...")
    assert d["subject"].startswith("Second notice") and d["suggested_to"] == "ap@acmerobotics.example"
    assert store.latest_draft("INV-1042")["tier"] == 2


def test_gated_tools_execute_when_called_directly(store: Store) -> None:
    tools.draft_followup("INV-1045", 1, "Hi Marcus ...")
    r = tools.send_client_email("INV-1045", "marcus@bluefinmedia.example", "Quick reminder: INV-1045", "Hi Marcus")
    assert r["ok"] and r["tier"] == 1
    assert store.list_outbox()[0]["to_addr"] == "marcus@bluefinmedia.example"
    assert store.list_reminders("INV-1045")[-1]["tier"] == 1
    assert tools.offer_payment_plan("INV-1034", "two payments of $475")["ok"]
    assert tools.propose_write_off("INV-1034", "no response")["status"] == "written_off"
    assert store.get_invoice("INV-1034")["status"] == "written_off"


def test_bookkeeper_tools(store: Store) -> None:
    listing = tools.list_uncategorized_expenses()
    by_id = {e["expense_id"]: e for e in listing["expenses"]}
    assert listing["count"] == 11
    assert (
        by_id["TX-2105"]["suggested_category"] == "equipment_hardware" and by_id["TX-2105"]["candidate_receipts"] == []
    )
    assert by_id["TX-2103"]["candidate_receipts"][0]["receipt_id"] == "RCPT-503"
    assert tools.categorize_expense("TX-2105", "equipment_hardware")["ok"]
    assert "valid_categories" in tools.categorize_expense("TX-2105", "nonsense")
    assert tools.match_receipt("TX-2103", "RCPT-503")["ok"]
    assert "error" in tools.match_receipt("TX-2104", "RCPT-503")  # receipt already attached elsewhere

    missing = tools.list_expenses_missing_receipts(older_than_days=7)
    ids = {e["expense_id"] for e in missing["expenses"]}
    assert ids == {"TX-2105", "TX-2108"}  # laptop + USPS; the 5-day-old coffee is excluded
    t = tools.create_todo("Find receipt: Apple Store $1,299.00 (2026-08-29)", note="TX-2105")
    assert t["ok"] and t["due_date"] == "2026-09-19" and t["duplicate"] is False
    assert tools.create_todo("Find receipt: Apple Store $1,299.00 (2026-08-29)")["duplicate"] is True


def test_reporter_tools(store: Store) -> None:
    from chaser.context import set_cycle_id

    cid = store.start_cycle()
    set_cycle_id(cid)
    try:
        tools.match_deposit_to_invoice("TX-2001", "INV-1044")
        store.add_action(
            agent="reconciler",
            tool_name="match_deposit_to_invoice",
            tool_input={},
            summary="m",
            status="success",
            cycle_id=cid,
        )
        tools.flag_deposit_for_review("TX-2004", "unknown")
        summary = tools.get_week_summary()
        assert summary["cash_collected_this_week"] == 920.0
        assert summary["reconciliation"]["deposits_matched"] == 1
        assert summary["aging"]["d61_plus"]["total"] == 950.0
        assert len(summary["pending_decisions"]) == 1
        pend = tools.list_pending_decisions()
        assert pend["count"] == 1 and pend["decisions"][0]["kind"] == "review"
        acts = tools.list_actions_this_week()
        assert acts["cycle_id"] == cid and "reconciler" in acts["by_agent"]
    finally:
        set_cycle_id(None)
