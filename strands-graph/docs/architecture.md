# Chaser architecture

![Architecture](architecture.png)

Diagram: [architecture.png](architecture.png). Editable source: [architecture.excalidraw](architecture.excalidraw) (open at excalidraw.com), also [architecture.svg](architecture.svg).

## One cycle, end to end

`service.run_sweep()` is the whole weekly close. It is called by three thin wrappers:
`main.py` (AgentCore `{"action": "sweep"}`), `app/server.py` (`POST /api/sweep` and the background
scheduler) and `python -m chaser.cli sweep`.

1. `store.start_cycle()` opens a cycle row and `context.set_cycle_id()` publishes it so tools and
   hooks can tag everything they write.
2. `agents.build_graph()` builds four fresh `Agent`s and wires them with `GraphBuilder`:

   | node | tools | gated |
   |---|---|---|
   | `reconciler` | list_open_invoices, list_unmatched_deposits, list_recent_client_emails, match_deposit_to_invoice, record_partial_payment, flag_deposit_for_review, note_client_context | none (flagging creates a low-severity `review` decision directly) |
   | `bookkeeper` | list_uncategorized_expenses, categorize_expense, match_receipt, list_expenses_missing_receipts, create_todo | none |
   | `collector` | list_overdue_invoices, get_client_profile, get_contract_terms, draft_followup, send_client_email, offer_payment_plan, propose_write_off | send_client_email, offer_payment_plan, propose_write_off |
   | `reporter` | get_week_summary, list_pending_decisions, list_actions_this_week | none |

   Edges: `reconciler -> bookkeeper` (always), `reconciler -> collector` (condition closure
   `store.count_overdue_open_invoices(today) > 0`, evaluated after reconciliation so freshly paid
   invoices no longer count), `collector -> reporter`, `bookkeeper -> reporter`. Entry point
   `reconciler`; `set_max_node_executions(8)`, `set_execution_timeout(600)`, `set_node_timeout(240)`.
3. The graph runs. Every tool call on every node passes through `hooks.AuditHook`
   (`AfterToolCallEvent`) which writes an `actions` row with the agent name. Gated calls on the
   collector pass through `approval.ApprovalGate` first (see below).
4. `service.run_sweep` then calls the reporter agent again with
   `structured_output_model=WeeklyCloseReport` to turn its narrative into the report schema
   (`models.py`). If that fails, `fallback_report()` builds the report from the store so the
   cycle still produces a usable report.
5. The report, pending decisions and this cycle's actions are saved and returned.

## Propose-then-execute (the human-in-the-loop mechanism)

`approval.ApprovalGate` is an `InterventionHandler` (`name = "approval-gate"`) attached only to the
collector. In `before_tool_call`:

- non-gated tool: `Proceed()`.
- gated tool: build a dedupe key `(tool_name, invoice_id)` (`approval.dedupe_key`). If a pending
  decision with that key already exists (in this instance or in the store), return `Deny` with a
  "already queued as <id>" reason. Otherwise `summarize_proposal()` produces a human-readable
  summary such as "Send tier-2 reminder to Acme Robotics for INV-1042 ($2,400.00, 23 days
  overdue)", `store.create_decision()` persists `{kind: approval, agent, tool_name, tool_input,
  summary, client_id, invoice_id, tier, cycle_id, status: pending}`, and the handler returns
  `Deny(reason="Queued for owner approval as decision <id>. Do not retry ...")`.

The model sees the deny reason as the tool result, moves on, and mentions the pending approval in
its summary. The sweep never blocks on a human.

`service.decide(decision_id, response, edits)` closes the loop:

- approval kind, approved: merge `edits` (e.g. an edited `body`) into `tool_input`, call
  `agents.make_execution_agent(tool_name)` (the owning node rebuilt from `TOOL_OWNERS`, without
  the gate, `record_direct_tool_call=False`) and run `agent.tool.<tool_name>(**tool_input)`.
  The tool executes for real (demo: `connectors.OutboxEmailSender` writes to the `outbox` table),
  the AuditHook records it, the decision becomes `executed`.
- approval kind, denied: decision becomes `denied`; a client note "Owner declined on <date>: ..."
  is stored, and `get_client_profile()` surfaces `owner_declined_days_ago` so next week's collector
  does not propose the same thing.
- review kind (unknown deposit): `edits={"invoice_id": ...}` matches it to an invoice; a plain
  approval marks it other income; denial leaves it unmatched.

## Why the graph is rebuilt every cycle

The weekly close is a scheduled job. All durable state (invoices, decisions, audit, reports,
client notes) lives in the store, so each cycle starts from a clean agent context and reads the
current truth through tools. `build_graph()` accepts an optional `session_manager` for
long-running deployments, but it is not used by default. The `ask` agent is the opposite: it is
conversational, so `sessions.make_session_manager()` gives it a `FileSessionManager` (or
`S3SessionManager` when `SESSION_BUCKET` is set).

## Storage

`store.Store` (SQLite, WAL, one connection guarded by a lock) holds clients, client notes,
invoices, reminders, transactions, receipts, emails, todos, decisions, actions, cycles, reports
and outbox. Its API is intentionally small and key-based so a DynamoDB implementation could
replace it. `CHASER_DB_PATH` selects the file; `:memory:` is used by tests. On AgentCore the
runtime filesystem is ephemeral, so `agentcore.json` points the DB at `/tmp` and the entrypoint
reseeds when the DB is empty; a production deployment would point the store at a durable service.

## Deployment shape

- `main.py`: `BedrockAgentCoreApp` entrypoint; dispatches `sweep | decide | ask | status | seed`.
- `app/server.py`: FastAPI UI with `Backend` protocol (`backend.py`): `LocalBackend` runs the
  service in-process, `AgentCoreBackend` calls `invoke_agent_runtime` with a stable session id.
- `recongraph/agentcore/agentcore.json`: CodeZip runtime `ReconGraphAgent`, deployed via `agentcore deploy`.
