# Devpost submission: Chaser

**Project name:** Chaser

**Tagline (<=60 chars):** Weekly close for freelancers; asks before it emails a client

**Track:** Professional Agents

**Try it out:** https://d344dcrnpg.us-east-1.awsapprunner.com (web UI on AWS App Runner; the agents run on Amazon Bedrock AgentCore Runtime `arn:aws:bedrock-agentcore:us-east-1:796330847946:runtime/Chaser_ChaserAgent-20whYPEX0A`)

**Code:** https://github.com/ritwikareddykancharla/chaser

## Inspiration

Every freelancer we know has the same Friday ritual: open the bank app, open the invoicing tool,
figure out which deposit was which invoice (why is it $30 short?), then stare at a blank email
trying to sound friendly but firm to a client who is 23 days late. The work is repetitive, but the
judgment is real: tone depends on who the client is, how late they are, and whether they already
said "it's coming". We wanted an agent that does the routine part every week and brings the
judgment calls to the owner in one batch.

## What it does

Chaser runs a weekly close. It matches new bank deposits to open invoices (exact, fee-adjusted,
partial), reads recent client emails so it does not chase someone who wrote yesterday, picks a
follow-up tier for each overdue invoice (gentle, firm with payment link, final notice with the
contract's late fee, or recommend a call), drafts the email, categorizes expenses, attaches
receipts, creates "find receipt" to-dos, and writes a structured weekly report. Anything that
reaches a client (sending an email, offering a payment plan, proposing a write-off) is queued as a
proposal card. The owner approves, edits, or skips each one in a small web app, and can ask
questions like "who still owes me money?".

## How we built it

- **Strands Agents SDK.** A `Graph` (`GraphBuilder`) with four specialist `Agent` nodes:
  reconciler, collector, bookkeeper, reporter. Deterministic edges plus one conditional edge
  (collector runs only if invoices are still overdue after reconciliation). Each node has its own
  `@tool` subset and system prompt.
- **Custom `InterventionHandler` (`ApprovalGate`).** In `before_tool_call` it turns gated tool
  calls into pending decisions in SQLite and returns `Deny` with an instruction not to retry, with
  de-duplication by invoice. The pipeline never blocks on a human.
- **Direct tool execution on approval.** `service.decide()` applies the owner's edits and calls
  `agent.tool.send_client_email(...)` on the owning agent, rebuilt without the gate.
- **`HookProvider` (`AuditHook`)** on `AfterToolCallEvent` records every tool call per agent.
- **Structured output.** The reporter produces a Pydantic `WeeklyCloseReport` via
  `structured_output_model`.
- **Session management** (`FileSessionManager` / `S3SessionManager`) for the conversational `ask`
  agent; `SlidingWindowConversationManager` on every node.
- **Amazon Bedrock AgentCore Runtime** hosts the entrypoint (`sweep`, `decide`, `ask`, `status`)
  with Claude Sonnet on Bedrock; a FastAPI web app talks to it through `invoke_agent_runtime`
  or runs the agent in-process for local demos.
- Deterministic scripted `Model` implementation for offline tests (43 tests, under 15 seconds).

## Challenges

- Making a background pipeline safe without making it stop. Interrupt-and-resume patterns are
  awkward for a scheduled job; an intervention that denies and queues turned out to be much
  simpler, and the deny reason doubles as an instruction to the model.
- Keeping the model from re-proposing the same action: dedupe on `(tool, invoice)` both within a
  run and against still-pending decisions.
- Matching semantics: a $30 shortfall is a wire fee, a $600 shortfall is a partial. Encoding
  those thresholds as pure functions with tests was the only way to keep it honest.
- The realistic exceptions in the dataset (a client that already said "processing this week", a
  client whose AP address changed) required the tier logic to look at the right email, not just
  the latest one.

## Accomplishments

- A complete weekly close that finishes unattended and leaves the owner with 4-5 decisions
  instead of two hours of work.
- Every tool call is attributed to an agent in an audit feed that visibly shows the multi-agent
  pipeline.
- Approve, edit, and skip all flow back into next week's behaviour (a declined reminder is
  remembered).

## What we learned

Interventions are a very good fit for propose-then-execute: the handler sees the exact tool input
the model wanted to use, so the proposal card is precise and the approved action is exactly what
the model proposed (plus the owner's edits). Also: system prompts are product logic; most of the
behaviour that judges will see lives in `prompts.py`.

## What's next

Real connectors (Plaid or bank CSV import, Gmail or IMAP, Stripe, QuickBooks or Wave), a durable
store on DynamoDB, scheduled sweeps from EventBridge, SES for sending, and learning tone
preferences from the owner's edits.

## Built with

Python 3.12, Strands Agents SDK, Amazon Bedrock (Claude Sonnet), Amazon Bedrock AgentCore
Runtime, AWS App Runner (web UI), AWS CodeBuild and CloudFormation, FastAPI, SQLite, Pydantic,
uv, pytest, ruff.
