"""main.invoke payload dispatch and the FastAPI endpoints (httpx TestClient)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from scripted_model import ScriptedModel, text

import main
from chaser import service
from chaser.store import Store
from chaser.tools import flag_deposit_for_review


def test_invoke_dispatch_status_and_errors(store: Store) -> None:
    out = main.invoke({"action": "status"})
    assert out["ok"] is True and out["counts"]["clients"] == 7
    assert main.invoke({"action": "nope"}) == {"ok": False, "error": "unknown action: 'nope'"}
    assert main.invoke({})["ok"] is False
    assert main.invoke({"action": "decide"})["ok"] is False
    assert main.invoke({"action": "ask", "prompt": "  "})["ok"] is False
    assert main.invoke({"action": "decide", "decision_id": "dec_x", "response": "yes"})["ok"] is False


def test_invoke_decide_and_state(store: Store) -> None:
    r = flag_deposit_for_review("TX-2004", "unknown")
    out = main.invoke(
        {"action": "decide", "decision_id": r["decision_id"], "response": "yes", "edits": {"invoice_id": "INV-1041"}}
    )
    assert out["ok"] and out["decision"]["status"] == "executed"
    st = main.invoke({"action": "state"})
    assert st["ok"] and st["counts"]["decisions_resolved"] == 1


def test_invoke_sweep_and_ask(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = {n: ScriptedModel([text(f"{n} done")]) for n in ("reconciler", "collector", "bookkeeper", "reporter")}
    monkeypatch.setattr(service, "default_model_factory", lambda node: scripts[node])
    out = main.invoke({"action": "sweep"})
    assert out["ok"] and "report" in out and "pending_decisions" in out and "actions_taken" in out
    assert out["report"]["execution_order"][0] == "reconciler"

    monkeypatch.setattr(service, "make_ask_agent", lambda model, session: _FakeAgent("Acme owes $2,400."))
    assert main.invoke({"action": "ask", "prompt": "who owes me?"}) == {"ok": True, "answer": "Acme owes $2,400."}


class _FakeAgent:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def __call__(self, prompt: str):
        return self.answer


@pytest.fixture
def client(store: Store, monkeypatch: pytest.MonkeyPatch):
    from app import server

    scripts = {n: ScriptedModel([text(f"{n} ok")]) for n in ("reconciler", "collector", "bookkeeper", "reporter")}
    monkeypatch.setattr(service, "default_model_factory", lambda node: scripts[node])
    monkeypatch.setattr(service, "make_ask_agent", lambda model, session: _FakeAgent("You are owed $14,775."))
    server._backend = None
    with TestClient(server.app) as c:
        yield c


def test_api_state_and_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["ok"] is True
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["clients"] == 7 and body["aging"]["d61_plus"]["total"] == 950.0
    assert body["pending"] == [] and body["report"] is None
    assert client.get("/").status_code == 200 and "Chaser" in client.get("/").text


def test_api_sweep_then_decide(client: TestClient, store: Store) -> None:
    assert client.post("/api/sweep").json() == {"started": True}
    for _ in range(100):
        if store.last_report() is not None and not client.get("/api/state").json()["sweep_running"]:
            break
        time.sleep(0.1)
    state = client.get("/api/state").json()
    assert state["report"]["execution_order"][0] == "reconciler"
    assert state["last_sweep_status"] == "completed"
    # Live narration was written per node while the graph ran, tagged with the agent's name.
    agents = {p["agent"] for p in state["progress"]}
    assert "reconciler" in agents
    assert {"thinking", "said"} <= {p["kind"] for p in state["progress"]}

    r = flag_deposit_for_review("TX-2004", "unknown")
    out = client.post(f"/api/decisions/{r['decision_id']}", json={"response": "yes", "edits": {}}).json()
    assert out["ok"] and out["result"]["outcome"] == "other_income"
    assert client.post("/api/decisions/dec_missing", json={"response": "yes"}).status_code == 404


def test_api_ask(client: TestClient) -> None:
    assert client.post("/api/ask", json={"prompt": "who owes me?"}).json() == {
        "ok": True,
        "answer": "You are owed $14,775.",
    }
    assert client.post("/api/ask", json={"prompt": ""}).status_code == 422
