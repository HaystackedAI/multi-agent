# Decisions and assumptions

Short log of calls made while building Chaser without a reviewer in the loop.

1. **Gate only the collector.** The spec's gated tools (`send_client_email`, `offer_payment_plan`,
   `propose_write_off`) all belong to the collector, so `ApprovalGate` is attached only there.
   The reconciler's `flag_deposit_for_review` is an allowed tool that itself writes a
   low-severity `review` decision; it never needs the gate.
2. **Dedupe key is `(tool_name, invoice_id)`** (`approval.dedupe_key`). The gate checks both its
   in-memory map for the current run and the store's pending decisions, so a retry in the same
   run or a re-run next week while a proposal is still pending does not create a second card.
3. **Approved proposals are executed via `agent.tool.<name>(...)`** on an agent rebuilt from the
   `TOOL_OWNERS` registry without the gate and with `record_direct_tool_call=False`. This is the
   simplest bypass; no context flag is needed because the gate is not on that agent.
4. **Fees are booked, not chased.** `match_deposit_to_invoice` treats a shortfall of at most
   $35 or 3.5% (`matching.FeePattern`) as a processor or wire fee: the invoice is credited the
   full amount and closes, the deposit allocation records the fee. Larger shortfalls are partials
   and must go through `record_partial_payment`.
5. **Hold rule for the collector.** Tier selection (`tiers.select_tier`) returns "hold" when the
   client emailed about paying or resending within the last 7 days. The reference email is the
   most recent one that names the invoice or uses a payment phrase (`tiers.PAYMENT_KEYWORDS`),
   so Acme's unrelated "Phase 3 kickoff" note does not suppress the Acme reminder while Orbital's
   "processing this week" does suppress theirs. The Northwind "resend" case is a hold for the
   reminder plus a gated resend, handled by the prompt.
6. **"Missing receipt" excludes expenses that have an unattached receipt on file** with match
   confidence >= 0.8 (`tools.missing_receipts`). Otherwise the list would name every expense
   before the bookkeeper has run `match_receipt`, which is noise, not a to-do.
7. **Structured report as a second call.** The reporter node produces text inside the graph;
   `service.run_sweep` then calls the same agent with `structured_output_model=WeeklyCloseReport`.
   If that fails, `fallback_report()` builds a report from the store, so `sweep` always returns a
   report dict.
8. **Graph has no session manager by default.** Cycles are independent scheduled jobs and the
   store is the memory. The `ask` agent does use `FileSessionManager` (or `S3SessionManager` when
   `SESSION_BUCKET` is set) so follow-up questions have history.
9. **Demo clock.** `DEMO_TODAY` (default 2026-09-12) drives all ages and deadlines, including
   "owner declined N days ago", which is computed from the `decided_on` date stored with the
   decision rather than the wall-clock resolution time.
10. **Scripted-model contract.** Tests inject models per node through `agents.ModelFactory`
    (`run_sweep(model_factory=...)`); the default resolves to `make_model()` at call time so tests
    never construct a Bedrock client.
11. **Line length 120 for ruff.** Chosen for readability of the tool docstrings and test fixtures.
12. **Ephemeral DB on AgentCore.** `agentcore.json` sets `CHASER_DB_PATH=/tmp/chaser.db`; the
    entrypoint reseeds an empty DB so `sweep` works immediately after deploy. A durable backend
    (DynamoDB or RDS) is the obvious next step for real use and the store API is shaped for it.
13. **Email sending is a connector.** `connectors.OutboxEmailSender` writes to an `outbox` table so
    the demo is safe; swapping in SES or Gmail means implementing `EmailSender.send`.
14. **First commit author.** The initial commit was made with a placeholder author before the
    globally configured author was confirmed; later commits use the global identity. History was
    not rewritten.
