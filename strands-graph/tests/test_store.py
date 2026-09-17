from datetime import date

from chaser.store import Store

TODAY = date(2026, 9, 12)


def test_seed_counts(store: Store) -> None:
    c = store.counts()
    assert c["clients"] == 7
    assert c["invoices_open"] == 9
    assert c["unmatched_deposits"] == 4
    assert c["uncategorized_expenses"] == 11  # the savings transfer is not an expense
    assert store.count_overdue_open_invoices(TODAY) == 6


def test_record_payment_full_and_partial(store: Store) -> None:
    inv = store.record_payment("INV-1044", 920.0, "TX-2001", TODAY, note="check")
    assert inv["status"] == "paid" and inv["paid_on"] == "2026-09-12"
    assert store.get_transaction("TX-2001")["status"] == "matched"

    inv = store.record_payment("INV-1041", 600.0, "TX-2003", TODAY)
    assert inv["status"] == "partial" and inv["amount_paid"] == 600.0
    assert store.count_overdue_open_invoices(TODAY) == 6  # partial still counts as overdue


def test_record_payment_with_fee_closes_invoice_but_allocates_cash_only(store: Store) -> None:
    inv = store.record_payment("INV-1043", 2120.0, "TX-2002", TODAY, note="wire", fee=30.0)
    assert inv["status"] == "paid" and inv["amount_paid"] == 2150.0
    tx = store.get_transaction("TX-2002")
    assert tx["status"] == "matched"
    assert tx["allocations"][0] == {"invoice_id": "INV-1043", "amount": 2120.0, "fee": 30.0, "note": "wire"}
    assert store.cash_collected(date(2026, 9, 7), TODAY) == 2120.0


def test_decisions_crud_and_dedupe(store: Store) -> None:
    d = store.create_decision(
        kind="approval",
        agent="collector",
        tool_name="send_client_email",
        tool_input={"invoice_id": "INV-1042"},
        summary="Send reminder",
        dedupe_key="send_client_email:INV-1042",
        cycle_id="c1",
    )
    assert d["status"] == "pending" and d["tool_input"] == {"invoice_id": "INV-1042"}
    assert store.find_pending_decision("send_client_email:INV-1042")["id"] == d["id"]
    resolved = store.resolve_decision(d["id"], "executed", "yes", {"body": "x"}, {"result": "ok"})
    assert resolved["status"] == "executed" and resolved["edits"] == {"body": "x"}
    assert store.find_pending_decision("send_client_email:INV-1042") is None
    assert store.list_decisions("executed")[0]["id"] == d["id"]


def test_actions_todos_and_aging(store: Store) -> None:
    store.add_action(
        agent="reconciler",
        tool_name="list_open_invoices",
        tool_input={},
        summary="read",
        status="success",
        cycle_id="c1",
    )
    store.add_action(
        agent="bookkeeper",
        tool_name="create_todo",
        tool_input={"title": "x"},
        summary="todo",
        status="success",
        cycle_id="c1",
    )
    assert [a["kind"] for a in store.list_actions(cycle_id="c1")] == ["write", "read"]
    assert len(store.list_actions(cycle_id="c1", kinds=("write",))) == 1

    t1 = store.add_todo("Find receipt: Apple", "2026-09-19", None, "c1", dedupe_key="find receipt: apple")
    t2 = store.add_todo("Find receipt: Apple", "2026-09-19", None, "c1", dedupe_key="find receipt: apple")
    assert t1["id"] == t2["id"] and t2["duplicate"] is True
    assert len(store.list_todos()) == 1

    aging = store.aging_buckets(TODAY)
    assert aging["current"]["count"] == 3 and aging["current"]["total"] == 7170.0
    assert aging["d1_30"]["count"] == 4 and aging["d31_60"]["count"] == 1 and aging["d61_plus"]["count"] == 1
    assert aging["d61_plus"]["invoices"][0]["id"] == "INV-1034"


def test_reports_cycles_meta(store: Store) -> None:
    cid = store.start_cycle()
    store.save_report(cid, {"narrative": "ok"})
    store.finish_cycle(cid, "completed")
    assert store.last_report()["narrative"] == "ok"
    assert store.last_cycle()["status"] == "completed"
    assert store.get_meta("last_sweep_at") is not None
