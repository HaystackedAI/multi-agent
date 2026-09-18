# Code map — Chaser (review findings)

> Reviewer's notes from a full read of the repo. Focus: the Strands multi-agent
> **graph** and how a request flows through the code. Data is documented separately in
> [data-model.md](data-model.md). Product/flow narrative already lives in
> [architecture.md](architecture.md); this file is the code-level companion.

## 1. Two deployables, one contract

The repo ships **two independent apps** that talk over one tiny JSON contract.

```
┌─────────────────────────┐        {"action": "..."}        ┌──────────────────────────────┐
│  app/  (FastAPI web UI)  │  ───────────────────────────▶  │ recongraph/.../ReconGraphAgent │
│  the "face"              │                                 │  the "brain" (all logic)       │
│  thin client, no logic   │  ◀───────────────────────────  │  Bedrock AgentCore Runtime     │
└─────────────────────────┘          JSON result            └──────────────────────────────┘
```

| | Web app (`app/`) | Agent (`recongraph/app/ReconGraphAgent/`) |
|---|---|---|
| Role | Decision-inbox UI + JSON API | The 4-agent graph + tools + store |
| Runs the graph? | **No** — never | Yes |
| Deploy target | FastAPI Cloud (`uvicorn app.server:app`) | Bedrock AgentCore Runtime (CodeZip) |
| Entry | `app/server.py` | `main.py` (`BedrockAgentCoreApp`) |
| State | none (stateless) | SQLite store in `/tmp` (ephemeral, reseeded) |

The web app is a genuine thin client: `app/server.py` handles routing/UI and every
handler delegates to `app/agent.py::AgentClient`, which just POSTs an action payload
to the runtime (boto3 `invoke_agent_runtime`, or `AGENT_LOCAL_URL` for local testing).
There is **no business logic on the web side** — including no in-process graph.

### The action contract (`main.py::dispatch`)
```
{"action": "sweep"}                                    run one weekly close
{"action": "decide", "decision_id", "response", "edits"}  resolve a pending decision
{"action": "ask", "prompt"}                            read-only Q&A
{"action": "status"}                                   counts + last sweep
{"action": "seed"}                                     reload demo dataset
{"action": "state"}                                    everything the UI renders
```
`main.py::dispatch` never raises — it always returns `{"ok": bool, ...}`. It also
lazily reseeds the store on first use (`_ensure_seeded`) because AgentCore's disk
starts empty, and `_normalize` unwraps the `agentcore invoke` shape (a bare prompt
becomes `{"action": "ask"}`).

## 2. The brain: `src/chaser/` module map

18 modules. Grouped by role:

**Orchestration (the graph)**
- `agents.py` — agent factories, the `TOOL_OWNERS` registry, `build_graph()`. **Start here to learn the graph.**
- `service.py` — `run_sweep`, `decide`, `ask`, `status`, `seed`, `ui_state`. The real API; `main.py`/`server.py`/`cli.py` are thin wrappers.
- `prompts.py` — the four node system prompts + tone rules. This is product logic, not boilerplate.

**Human-in-the-loop**
- `approval.py` — `ApprovalGate` (Strands `InterventionHandler`): propose-then-execute.
- `hooks.py` — `AuditHook` (writes every tool call to `actions`) + `ProgressHook` (live narration to `progress`).

**Tools & pure logic**
- `tools.py` — all 23 `@tool` functions, grouped into per-agent subsets. See [data-model.md](data-model.md) for the full list.
- `matching.py` — pure deposit→invoice matching (exact / fee / combined / partial) and receipt/category suggestion. No I/O.
- `tiers.py` — pure follow-up tier selection (0 hold … 4 escalate) with hold rules.
- `models.py` — `WeeklyCloseReport` Pydantic schema (structured output).

**Data & infra**
- `store.py` — SQLite store, 16 tables. The persistence layer.
- `connectors.py` — connector protocols + `JsonDataConnector` (demo) + `OutboxEmailSender` (writes email to a table, never sends).
- `context.py` — per-process `get_store()` / current cycle id (how tools reach the store without it being passed in).
- `config.py` — env config + the **demo clock** (`today()` defaults to `2026-09-12`).
- `sessions.py` — `FileSessionManager` / `S3SessionManager` for the conversational `ask` agent.
- `model.py` — `make_model()`: `BedrockModel` or `AnthropicModel`.
- `cli.py`, `seed.py`, `__init__.py` — CLI wrappers and seeding helper.

## 3. The graph (learn-the-graph section)

Built in `agents.py::build_graph` with Strands `GraphBuilder`. **Topology:**

```
              ┌──────────────┐
              │  reconciler  │  (entry point)
              └──────┬───────┘
        always       │        condition: has_overdue(state)
        ┌────────────┴────────────┐   = store.count_overdue_open_invoices(today) > 0
        ▼                         ▼
  ┌────────────┐            ┌────────────┐
  │ bookkeeper │            │ collector  │  (has the ApprovalGate)
  └──────┬─────┘            └──────┬─────┘
         │                         │
         └───────────┬─────────────┘
                     ▼
               ┌──────────┐
               │ reporter │  (also produces structured output afterward)
               └──────────┘
```

Key design points I want you to notice:

1. **One conditional edge.** `reconciler → collector` fires only when
   `has_overdue()` is true. Because the condition is a **closure over the store** and
   is evaluated *after the reconciler finishes*, invoices paid this cycle no longer
   count — the collector is skipped entirely if reconciliation cleared the overdue set.
   This is the single most instructive line in the graph (`agents.py:121-123, 129`).

2. **Each node is a plain `Agent`** with (a) its own tool subset (`NODE_TOOLS`),
   (b) its own system prompt (`NODE_PROMPTS`), (c) the shared `AuditHook` + `ProgressHook`,
   and — **only for the collector** — (d) the `ApprovalGate` intervention. See
   `make_node_agent()` (`agents.py:68`).

3. **The graph carries no session/memory.** It's rebuilt fresh every cycle
   (`build_graph` is called inside `run_sweep`). All durable truth lives in the store;
   each node reads current state through tools. Contrast the `ask` agent
   (`make_ask_agent`), which *does* get a session manager because it's conversational.

4. **Guardrails:** `set_max_node_executions(8)`, `set_execution_timeout(600)`,
   `set_node_timeout(240)` (`agents.py:133-135`).

5. **`TOOL_OWNERS`** (`agents.py:64`) maps every `tool_name → node`. This is what lets
   `decide()` later replay an approved tool call *through the agent that owns it*
   (`make_execution_agent`), without the gate.

### What one sweep actually does (`service.run_sweep`, service.py:118)
1. Acquire `SWEEP_LOCK` (non-blocking; a second concurrent sweep is refused).
2. `store.start_cycle()` → `context.set_cycle_id()` so tools/hooks tag their writes.
3. `build_graph(store, model_factory)` → run `graph(SWEEP_TASK)`.
4. Ask the **reporter node again** with `structured_output_model=WeeklyCloseReport` to
   turn its narrative into the schema. If that fails → `fallback_report()` builds the
   report deterministically from the store (`service.py:59`). Robust by design.
5. Save report, collect pending decisions + this cycle's actions, `finish_cycle()`, return.

## 4. Propose-then-execute (the human-in-the-loop core)

This is the product's defining mechanism (also covered in architecture.md §Propose-then-execute).
Code-level summary:

- **Deny, don't pause.** `ApprovalGate.before_tool_call` (`approval.py:95`) returns
  `Proceed()` for routine tools and, for a **gated** tool (`send_client_email`,
  `offer_payment_plan`, `propose_write_off`), persists a pending `decision` row and
  returns `Deny(reason="Queued for owner approval as decision <id>…")`. The model
  treats the deny reason as the tool result and moves on — the sweep always completes.
- **Dedupe** by `(tool_name, invoice_id)` (`approval.py::dedupe_key`) so a retry or next
  week's run doesn't create a second card while one is pending.
- **Execute on approval.** `service.decide` (`service.py:186`) merges `edits` into the
  stored `tool_input`, rebuilds the owning agent *without* the gate
  (`make_execution_agent`), and calls `agent.tool.<name>(**tool_input)` directly.
- ⚠️ **Gating is enforced at the agent layer, not in the tool bodies.** The three gated
  tool functions themselves perform their write when called directly (e.g.
  `propose_write_off` sets `status=written_off`). They are safe only because the gate
  intercepts them during a sweep, and `decide` calls them intentionally after approval.
  If you reuse these tools elsewhere, that invariant must hold.

## 5. Request flow end-to-end (who calls whom)

```
Browser
  │  POST /api/sweep
  ▼
app/server.py  ── AgentClient.sweep() ──▶ boto3 invoke_agent_runtime
  (thin)                                        │  {"action":"sweep"}
                                                ▼
                                    main.py::dispatch ──▶ service.run_sweep()
                                                                │
                                          build_graph → graph(task) → reporter structured output
                                                                │
                                              store writes (invoices, decisions, actions, report)
                                                                │
                                                ◀── JSON result ─┘
  ◀── UI polls GET /api/state ── AgentClient.state() ── {"action":"state"} ── service.ui_state()
```

The keepalive matters here: `app/server.py::_keepalive` pings `status` every
`KEEPALIVE_SECONDS` (default 600) so AgentCore's microVM — and the in-`/tmp` demo state
— survives the 15-minute idle timeout. Without it, visitors hit a cold start + empty store.

## 6. ⚠️ Findings: doc-vs-code drift

The top-level `README.md` describes a layout that **no longer matches the code**. Worth
knowing when learning from the README:

1. **`src/chaser/backend.py` does not exist.** README §"How it uses Strands" and
   architecture.md §Deployment reference a `Backend` protocol with `LocalBackend` /
   `AgentCoreBackend`. The actual web client is `app/agent.py::AgentClient` (boto3 /
   local-URL). There is no `backend.py` anywhere in the tree.
2. **Paths moved.** README shows `main.py`, `src/chaser/`, `data/`, `scripts/`, `tests/`
   at the repo root. They actually live under `recongraph/app/ReconGraphAgent/`. The repo
   root only has `app/` (web), `recongraph/` (agent), `docs/`.
3. **`store.py` docstring undercounts tables** — it lists 15 and omits `progress`; the
   schema actually has **16** `CREATE TABLE` statements (incl. `progress`, live narration).
   `reset()` also skips `progress` and `drafts`.
4. **`tools.py` module docstring's ownership map omits `list_todos` and the `ASK_TOOLS`
   subset.** The authoritative lists are the module-level constants
   (`RECONCILER_TOOLS` … `ASK_TOOLS`), not the docstring.

None of these are bugs — they're stale docs. The **code** is the source of truth; these
findings docs describe the code as it actually is.
