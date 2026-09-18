# Data model — Chaser (review findings)

> Reviewer's notes on **the data**: where it comes from, the SQLite schema, and how it
> moves through one weekly close. Companion to [code-map.md](code-map.md) (the graph)
> and [architecture.md](architecture.md) (the flow narrative).
>
> Everything is synthetic demo data for a fictional studio, dated relative to the demo
> clock `DEMO_TODAY = 2026-09-12` (`config.py::today`). "Overdue", "aging", and tier
> ages are all computed against that date, not the wall clock.

## 0. The data pipeline in one line

```
data/*.json  ──JsonDataConnector──▶  store.seed()  ──16 SQLite tables──▶  tools read/write  ──▶  ui_state()
```

`service.seed()` reads the six bundled files via `connectors.JsonDataConnector` and
bulk-loads them with `store.seed()` (which `reset()`s first). On AgentCore this runs
automatically on first use only (`main.py::_ensure_seeded`, which seeds when the DB has no
clients). The only `reset()` is inside `store.seed()`; there is no idle/timeout auto-wipe —
the data is cleared only by an explicit `seed` action.

## 1. Seed inputs — `recongraph/app/ReconGraphAgent/data/`

| File | Count | Loads into table | Shape (key fields) |
|---|---|---|---|
| `clients.json` | 7 | `clients` | `id, name, contact_name, email, ap_email, terms_days, late_fee_clause, late_fee_pct_month, notes` |
| `invoices.json` | 14 | `invoices` (+`reminders`) | `id, client_id, description, amount, amount_paid, issued, due, status, paid_on, payment_link, reminders_sent[]` |
| `bank_transactions.json` | 16 | `transactions` | `id, date, amount, kind(deposit/expense), memo, merchant` |
| `receipts.json` | 8 | `receipts` | `id, date, merchant, amount, file` |
| `inbox.json` | 12 | `emails` | `id, date, from_name, from_email, client_id, subject, body` |
| `chart_of_accounts.yaml` | — | *not stored* | `expense_categories[]`, `non_pnl[]`, each with `id, label, hints[]` |

Notes:
- The chart of accounts is **not** loaded into a table — it's read live by the
  bookkeeper's tools via `connectors.load_chart_of_accounts()`. `hints` are
  merchant/memo keywords that drive `matching.suggest_category()`.
- `invoices.json[].reminders_sent` seeds the `reminders` table (prior reminder history),
  which feeds tier escalation.

## 2. SQLite schema — 16 tables (`store.py::SCHEMA`, store.py:24-92)

Single WAL SQLite file (`CHASER_DB_PATH`, `:memory:` in tests), one connection guarded
by an `RLock`. The API is deliberately small and key-based so a DynamoDB backend could
drop in.

### Core domain
| Table | PK | Purpose | Notable columns |
|---|---|---|---|
| `clients` | `id` | Who you invoice | `terms_days`, `late_fee_clause`, `late_fee_pct_month`, `notes` (relationship tone) |
| `client_notes` | auto | Facts learned about a client | `note`, `source`, `cycle_id` (e.g. "Owner declined…") |
| `invoices` | `id` | Receivables | `amount`, `amount_paid`, `due`, `status` (open/partial/paid/written_off), `sent_to` |
| `transactions` | `id` | Bank deposits + expenses | `kind`, `status`, `category`, `receipt_id`, `allocations` (JSON), `note` |
| `receipts` | `id` | Expense receipts | `expense_id` (link back to a transaction) |
| `emails` | `id` | Client inbox | `client_id`, `subject`, `body` |

### Work products of a sweep
| Table | PK | Written by | Purpose |
|---|---|---|---|
| `reminders` | auto | collector (on approved send) | Sent-reminder history; drives tier escalation |
| `drafts` | `id` | collector `draft_followup` | Drafted (unsent) emails |
| `outbox` | `id` | `OutboxEmailSender` on approval | "Sent" mail (demo: recorded, never delivered) |
| `todos` | `id` | bookkeeper `create_todo` | Owner to-dos (deduped by title), e.g. "Find receipt: …" |
| `decisions` | `id` | `ApprovalGate` / `flag_deposit_for_review` | **Pending approvals & reviews** — the inbox |
| `actions` | auto | `AuditHook` + `decide` | Audit log of every tool call (kind: read/write/gated/approved/decision) |
| `progress` | auto | `ProgressHook` | Live per-node narration during a sweep |
| `reports` | auto | `run_sweep` | Saved `WeeklyCloseReport` JSON per cycle |
| `cycles` | `id` | `start_cycle`/`finish_cycle` | One row per sweep (status, timing) |
| `meta` | `key` | misc | `last_sweep_at`, `last_sweep_status`, `seeded_at` |

### The `decisions` table is the heart of the product
Every human-in-the-loop item is a row here. Two `kind`s:
- **`approval`** — a gated collector action (send email / payment plan / write-off).
  `tool_input` holds the exact call to replay on approval; `dedupe_key` =
  `tool_name:invoice_id`.
- **`review`** — an unexplained deposit the reconciler couldn't match
  (`flag_deposit_for_review`). Resolved by matching to an invoice, marking other income,
  or dismissing.

Lifecycle columns: `status` (pending → executed/denied/failed), `response`, `edits`,
`result`, `resolved_at`. `service.decide` writes all of these.

## 3. Derived views (not stored — computed on read)

These are the "smart" reads tools expose to the agents:

- **Aging buckets** (`store.aging_buckets(today)`): outstanding grouped into
  `current / d1_30 / d31_60 / d61_plus`, with per-invoice chips. Feeds the report and UI.
- **Cash collected** (`store.cash_collected(since, until)`): sum of allocated deposit
  amounts in a date range.
- **Match candidates** (`matching.match_deposit`): for each unmatched deposit, a ranked
  list of ways to explain it — see §4.
- **Recommended tier** (`tiers.select_tier`): per overdue invoice, a follow-up tier +
  reason, surfaced in `list_overdue_invoices()`.
- **Receipt/category suggestions** (`matching.suggest_receipts`, `suggest_category`).

## 4. The two pieces of pure judgment logic (no I/O)

### `matching.py` — explaining a deposit
`match_deposit(amount, open_invoices, memo, client_names)` returns ranked
`MatchCandidate`s (`kind ∈ exact | fee_adjusted | combined | partial`):
- **exact** — outstanding == amount (conf ~0.9, +0.1 if the memo names the client).
- **fee_adjusted** — shortfall matches a known `FeePattern`: card `2.9%+$0.30` /
  `3.5%+$0.49`, ACH `0.8%`, wire `$15/$25/$30/$35` (conf ~0.75).
- **combined** — 2–3 invoices from the same client summing to the deposit (conf ~0.7).
- **partial** — deposit smaller than one invoice's balance (conf ~0.3, boosted if the
  memo names the client or it's exactly half). Suppressed when there's no memo hint and
  >3 open invoices (too ambiguous to guess).

The reconciler **never guesses**: no plausible candidate → `flag_deposit_for_review`.

### `tiers.py` — choosing a follow-up
`select_tier(...)` returns a `TierDecision` (0 hold … 4 escalate):
- **Age bands** (`tier_for_age`): 1–14d → 1 gentle, 15–30 → 2 firm, 31–60 → 3 final, 61+ → 4 escalate.
- **Holds (tier 0)** — do *not* chase this week when: owner declined ≤10d ago, the
  client emailed about payment ≤7d ago, or a reminder went out ≤6d ago.
- **Escalation** — ≥2 prior reminders bumps a tier (capped below 3→…); ≥1 prior on a
  tier-1 bumps to 2.
- **Relationship softening** — `gentle_client` caps at tier 2 until 60+ days.
- **Late fee** — `late_fee_amount()` accrues `pct_per_month` prorated by 30-day months;
  cited only at tier ≥3 when the contract has a clause.

## 5. Data lifecycle across one sweep (worked, demo data)

`DEMO_TODAY = 2026-09-12`. What each node reads and writes:

1. **reconciler** reads `transactions` (unmatched deposits) + `emails` (last 14d).
   - Greenleaf $920 → **exact** match to INV-1044 → `record_payment`, invoice `paid`.
   - Northwind $2,120 wire vs $2,150 INV-1043 → **fee_adjusted** ($30 wire fee) → paid.
   - Harbor & Vine $600 on $1,200 INV-1041 → **partial** → invoice `partial`.
   - $500 Zelle from "J. PARK" → nothing plausible → `flag_deposit_for_review` → a
     `review` decision.
   - Client emails → `note_client_context` rows in `client_notes` (e.g. Orbital "processing this week").
2. **conditional edge** re-checks `count_overdue_open_invoices(today)` — still >0, so:
3. **bookkeeper** reads uncategorized `transactions` + `receipts` → `categorize_expense`,
   `match_receipt`; creates one `todo` for a $1,299 laptop with no receipt (>7d old).
4. **collector** reads `list_overdue_invoices` (each with a recommended tier). For each
   not on hold → `draft_followup` then the **gated** `send_client_email` →
   `ApprovalGate` writes an `approval` decision instead of sending. Orbital (67d) is a
   **hold** because its AP wrote about payment within 7 days.
5. **reporter** reads `get_week_summary` + pending decisions + this cycle's actions →
   writes narrative → `run_sweep` converts it to a `WeeklyCloseReport` row in `reports`.

Result the owner sees: **4 approval cards + 1 review card** (the Zelle). Everything else
already happened and is visible in `actions` / `outbox` / `todos`.

## 6. How tools reach the data (a gotcha worth knowing)

Tools do **not** receive a store argument. They call `context.get_store()` and
`context.current_cycle_id()` (module-level, per process). So the store is effectively a
per-process singleton set up by `service`/`main`, and every write is auto-tagged with
the active `cycle_id`. When rebuilding, replicate this context indirection or the tool
signatures won't line up.
