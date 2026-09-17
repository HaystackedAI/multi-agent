"""ApprovalGate through a scripted collector agent, plus decide() approve/deny paths."""

from scripted_model import ScriptedModel, call, text

from chaser import service
from chaser.agents import make_node_agent
from chaser.approval import ApprovalGate
from chaser.context import set_cycle_id
from chaser.store import Store
from chaser.tools import get_client_profile

BODY = "Hi Dana,\n\nINV-1042 for $2,400.00 was due Aug 20. Pay here: https://pay.chaser.example/INV-1042\n\nThanks"
SEND = dict(invoice_id="INV-1042", to="ap@acmerobotics.example", subject="Second notice: INV-1042", body=BODY)


def run_collector(store: Store, turns: list) -> tuple[ApprovalGate, object]:
    gate = ApprovalGate()
    agent = make_node_agent("collector", ScriptedModel(turns), gate=gate)
    result = agent("Run collections.")
    result.agent_messages = agent.messages  # keep the transcript for assertions
    return gate, result


def tool_result_texts(result) -> list[str]:
    out = []
    for m in result.agent_messages:
        for block in m.get("content", []):
            if "toolResult" in block:
                out.extend(c.get("text", "") for c in block["toolResult"].get("content", []))
    return out


def test_gated_call_becomes_pending_decision_and_deny_reason(store: Store) -> None:
    cid = store.start_cycle()
    set_cycle_id(cid)
    try:
        gate, result = run_collector(
            store,
            [
                call("list_overdue_invoices"),
                call("draft_followup", invoice_id="INV-1042", tier=2, body=BODY, subject="Second notice: INV-1042"),
                call("send_client_email", **SEND),
                text("Proposed a tier-2 reminder for Acme; awaiting approval."),
            ],
        )
    finally:
        set_cycle_id(None)

    assert result.stop_reason == "end_turn"
    pending = store.list_pending_decisions()
    assert len(pending) == 1
    d = pending[0]
    assert d["kind"] == "approval" and d["agent"] == "collector" and d["tool_name"] == "send_client_email"
    assert d["tool_input"] == SEND and d["tier"] == 2 and d["client_id"] == "C-ACME" and d["cycle_id"] == cid
    assert d["summary"] == "Send tier-2 (firm) reminder to Acme Robotics for INV-1042 ($2,400.00, 23 days overdue)"
    assert gate.created == [d["id"]]

    # the model saw the deny reason as the tool result, and nothing was actually sent
    texts = tool_result_texts(result)
    assert any(f"DENIED: Queued for owner approval as decision {d['id']}" in t for t in texts)
    assert store.list_outbox() == []
    gated = [a for a in store.list_actions(cycle_id=cid) if a["kind"] == "gated"]
    assert len(gated) == 1 and gated[0]["agent"] == "collector"


def test_duplicate_gated_call_in_same_run_creates_one_decision(store: Store) -> None:
    gate, _ = run_collector(
        store,
        [
            call("send_client_email", **SEND),
            call("send_client_email", **SEND),
            call("send_client_email", **{**SEND, "subject": "retry"}),
            text("done"),
        ],
    )
    assert len(store.list_pending_decisions()) == 1
    assert len(gate.created) == 1


def test_pending_from_earlier_cycle_is_not_duplicated(store: Store) -> None:
    run_collector(store, [call("send_client_email", **SEND), text("done")])
    run_collector(store, [call("send_client_email", **SEND), text("done")])  # next cycle, fresh gate
    assert len(store.list_pending_decisions()) == 1


def test_non_gated_tools_proceed(store: Store) -> None:
    run_collector(store, [call("draft_followup", invoice_id="INV-1045", tier=1, body="Hi Marcus"), text("ok")])
    assert store.latest_draft("INV-1045") is not None
    assert store.list_pending_decisions() == []


def test_decide_yes_executes_tool_directly_with_edits(store: Store) -> None:
    run_collector(
        store,
        [
            call("draft_followup", invoice_id="INV-1042", tier=2, body=BODY, subject="Second notice: INV-1042"),
            call("send_client_email", **SEND),
            text("done"),
        ],
    )
    d = store.list_pending_decisions()[0]
    out = service.decide(
        d["id"], "yes", {"body": BODY + "\n\nP.S. edited by owner"}, store=store, model=ScriptedModel()
    )
    assert out["ok"] is True and out["decision"]["status"] == "executed"
    assert out["decision"]["edits"] == {"body": BODY + "\n\nP.S. edited by owner"}
    msgs = store.list_outbox()
    assert len(msgs) == 1 and msgs[0]["body"].endswith("edited by owner") and msgs[0]["to_addr"] == SEND["to"]
    assert store.list_reminders("INV-1042")[-1]["tier"] == 2
    approved = [a for a in store.list_actions() if a["kind"] == "approved"]
    assert approved and approved[0]["decision_id"] == d["id"]
    # second decide on the same decision is rejected
    again = service.decide(d["id"], "yes", store=store, model=ScriptedModel())
    assert again["ok"] is False and "already executed" in again["error"]


def test_decide_no_records_denial_visible_in_client_profile(store: Store) -> None:
    run_collector(store, [call("send_client_email", **SEND), text("done")])
    d = store.list_pending_decisions()[0]
    out = service.decide(d["id"], "no", store=store, model=ScriptedModel())
    assert out["ok"] and out["decision"]["status"] == "denied" and out["result"] == {"executed": False}
    assert store.list_outbox() == []
    profile = get_client_profile("C-ACME")
    assert profile["owner_declined_days_ago"] == 0
    assert any("Owner declined" in n["note"] for n in profile["notes"])
    # next week's recommendation for Acme is now a hold
    from chaser.tools import list_overdue_invoices

    acme = next(i for i in list_overdue_invoices()["invoices"] if i["invoice_id"] == "INV-1042")
    assert acme["recommended_tier"] == 0 and "owner declined" in acme["recommendation_reason"]


def test_decide_review_deposit_match_and_other_income(store: Store) -> None:
    from chaser.tools import flag_deposit_for_review

    r = flag_deposit_for_review("TX-2004", "no match")
    out = service.decide(r["decision_id"], "yes", {"invoice_id": "INV-1041"}, store=store)
    assert out["ok"] and out["result"]["outcome"] == "matched"
    assert store.get_invoice("INV-1041")["amount_paid"] == 500.0
    assert store.get_transaction("TX-2004")["status"] == "matched"

    store.update_transaction("TX-2001", status="flagged")
    r2 = flag_deposit_for_review("TX-2001", "pretend unknown")
    out2 = service.decide(r2["decision_id"], "other income", store=store)
    assert out2["result"]["outcome"] == "other_income"
    assert store.get_transaction("TX-2001")["category"] == "other_income"


def test_decide_unknown_id(store: Store) -> None:
    assert service.decide("dec_nope", "yes", store=store)["ok"] is False
