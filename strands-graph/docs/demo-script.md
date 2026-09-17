# Chaser demo video script (4:00)

Setup before recording: `make seed`, `SWEEP_INTERVAL_SECONDS=0 make serve`, browser on
`http://localhost:8000`, terminal visible in a second window. `DEMO_TODAY=2026-09-12`.

## 0:00-0:40 The problem

Screen: bank app on the left, an invoicing tool on the right, a half-written "just following up"
email in the middle.

"If you freelance, Friday afternoon looks like this. A wire came in for $2,120, but the invoice
was $2,150. Is that a wire fee or did they short you? Acme is 23 days late and you already
reminded them once. Orbital is 67 days late but emailed on Tuesday saying payment is processing.
And there is a $1,299 laptop on the card with no receipt. None of this is hard. All of it takes
judgment, and all of it eats the afternoon."

## 0:40-1:00 Who it's for and why an agent

"Chaser is for freelancers, consultants and small studios who invoice clients directly and do
their own books. It runs the weekly close for you: reconciles, decides who needs a nudge and how
firm, tidies the books, and writes the report. The one rule: nothing reaches a client until you
say so."

## 1:00-3:20 Live demo

**1:00** Screen: Chaser UI, empty approvals queue, aging panel shows the open invoices.

"Here are the books before the close. Five invoices overdue, four unmatched deposits."

**1:10** Click **Run weekly close** in the header. Switch to the terminal running the server;
the activity feed on the right starts filling in.

"Four Strands agents run as a graph. The reconciler goes first."

**1:25** Point at the activity feed entries labelled *reconciler*.

"It matched Greenleaf's $920 check exactly, matched Northwind's $2,120 wire to the $2,150 invoice
and booked the $30 as a wire fee, recorded Harbor & Vine's $600 as a partial, and flagged the $500
Zelle from J. Park because nothing matches. That last one becomes a review card, not a guess."

**1:45** Point at *bookkeeper* entries.

"In parallel the bookkeeper categorized the new expenses against the chart of accounts, attached
receipts, and created one to-do: find the laptop receipt. Routine work, so no approval needed."

**2:00** Point at *collector* entries, then the approvals queue as cards appear.

"The collector is the one that talks to clients, so everything it wants to send lands here as a
proposal. Bluefin, 9 days late, gentle reminder. Acme, 23 days and one reminder already sent, a
firmer note with the payment link. Pixel Forge, 41 days, final notice quoting the 1.5% late fee
from their contract. Northwind asked us to resend to a new AP address, so that is a resend, not a
chase. And Orbital: 67 days late, but they wrote on Tuesday that payment is processing, so the
collector holds off and just notes it."

**2:30** Open the Acme card. Show the tier badge, invoice facts, the editable email preview.

"I can read exactly what would go out. I'll soften one line." Edit the body. Click **Approve
with edits**. The card moves to resolved; the activity feed shows *owner* executing
`send_client_email`.

**2:45** Open the Pixel Forge card. Click **Skip this week**.

"Skip is remembered. Next Friday the collector sees that I declined this and will not propose the
same email again."

**2:55** Open the Zelle review card. Choose an invoice from the **Match to invoice** dropdown, or
click **Mark as other income**.

**3:05** Scroll to the Weekly close report card: cash collected, aging buckets, drafts awaiting
approval, books health, top three things to know. Then type in the ask box: "who still owes me
money?" and show the answer.

## 3:20-3:50 Architecture

Screen: `docs/architecture.png`.

"Under the hood this is a Strands Graph: reconciler, then bookkeeper and collector, then reporter.
The collector only runs if something is still overdue after reconciliation. The approval gate is a
Strands InterventionHandler: when a gated tool is called it stores the exact tool input as a
pending decision and denies the call, so the pipeline never blocks. When you approve, the same
tool is executed directly on the owning agent with your edits. An audit hook records every tool
call per agent. It runs on Amazon Bedrock AgentCore Runtime with Claude on Bedrock; the web app
calls it through invoke_agent_runtime."

## 3:50-4:00 Close

"Chaser: the weekly close runs itself, and you only get asked before anything reaches a client.
Code, tests and deploy config are in the repo."
