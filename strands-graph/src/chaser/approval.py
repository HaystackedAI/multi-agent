"""ApprovalGate: a Strands InterventionHandler implementing propose-then-execute.

When an agent calls a gated tool (send_client_email, offer_payment_plan, propose_write_off)
the gate does NOT pause the agent. It records the exact tool call as a pending ``decision``
and returns ``Deny`` with a reason that tells the model the action is queued for the owner.
The background sweep therefore always completes, and the owner later reviews a batch of
proposals in the inbox. Approval executes the very same tool call directly
(``agent.tool.<name>(**tool_input)``) through an agent built without the gate.

De-duplication: one decision per (tool_name, invoice_id). A retry inside the same cycle, or
a proposal that is still pending from an earlier cycle, is denied without creating a second
decision.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import BeforeToolCallEvent
from strands.interventions import Deny, InterventionHandler, Proceed

from . import config
from .context import current_cycle_id, get_store
from .store import Store
from .tiers import TIER_LABELS, tier_for_age
from .tools import GATED_TOOLS

logger = logging.getLogger(__name__)

DENY_TEMPLATE = (
    "Queued for owner approval as decision {decision_id}. Do not retry this action; continue with "
    "other work and mention the pending approval in your summary."
)
DUPLICATE_TEMPLATE = (
    "Already queued for owner approval as decision {decision_id}. Do not retry this action; continue with other work."
)


def dedupe_key(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Key used to collapse repeated proposals: one per tool per invoice (or per input)."""
    invoice_id = tool_input.get("invoice_id") or tool_input.get("deposit_id") or ""
    return f"{tool_name}:{invoice_id}" if invoice_id else f"{tool_name}:{sorted(tool_input.items())!r}"


def summarize_proposal(tool_name: str, tool_input: dict[str, Any], store: Store) -> dict[str, Any]:
    """Build the human-readable summary and metadata for a gated tool call."""
    invoice_id = tool_input.get("invoice_id")
    inv = store.get_invoice(invoice_id) if invoice_id else None
    client = store.get_client(inv["client_id"]) if inv else None
    client_name = client["name"] if client else (inv["client_id"] if inv else "client")
    outstanding = round(inv["amount"] - inv["amount_paid"], 2) if inv else 0.0
    days = max(config.days_between(inv["due"], config.today()), 0) if inv else 0
    money = f"${outstanding:,.2f}"
    tier: int | None = None

    if tool_name == "send_client_email":
        draft = store.latest_draft(invoice_id) if invoice_id else None
        tier = int(draft["tier"]) if draft else tier_for_age(days)
        text = f"{tool_input.get('subject', '')} {tool_input.get('body', '')}".lower()
        if "resend" in text or "re-send" in text:
            summary = f"Resend {invoice_id} to {tool_input.get('to')} for {client_name} ({money}, {days} days overdue)"
        else:
            label = TIER_LABELS.get(tier, "reminder")
            summary = (
                f"Send tier-{tier} ({label}) reminder to {client_name} for {invoice_id} ({money}, {days} days overdue)"
            )
    elif tool_name == "offer_payment_plan":
        summary = f"Offer payment plan to {client_name} for {invoice_id} ({money}): {tool_input.get('terms', '')}"
    elif tool_name == "propose_write_off":
        summary = (
            f"Write off {invoice_id} ({client_name}, {money}, {days} days overdue): {tool_input.get('reason', '')}"
        )
    else:
        summary = f"{tool_name} {tool_input}"

    return {
        "summary": summary,
        "client_id": inv["client_id"] if inv else None,
        "invoice_id": invoice_id,
        "tier": tier,
    }


class ApprovalGate(InterventionHandler):
    """Intercept gated tools, persist them as pending decisions, and deny the in-run call."""

    name = "approval-gate"

    def __init__(self, gated_tools: frozenset[str] | set[str] = GATED_TOOLS) -> None:
        self.gated_tools = frozenset(gated_tools)
        self.created: list[str] = []  # decision ids created by this gate instance (one instance per cycle)
        self._seen: dict[str, str] = {}

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any) -> Proceed | Deny:
        tool_use = event.tool_use
        tool_name = tool_use["name"]
        if tool_name not in self.gated_tools:
            return Proceed()

        tool_input = dict(tool_use.get("input") or {})
        store = get_store()
        key = dedupe_key(tool_name, tool_input)

        existing_id = self._seen.get(key)
        if existing_id is None:
            existing = store.find_pending_decision(key)
            existing_id = existing["id"] if existing else None
        if existing_id:
            logger.info("approval-gate: duplicate proposal %s -> %s", key, existing_id)
            return Deny(reason=DUPLICATE_TEMPLATE.format(decision_id=existing_id))

        meta = summarize_proposal(tool_name, tool_input, store)
        agent_name = getattr(event.agent, "name", None) or "agent"
        decision = store.create_decision(
            kind="approval",
            agent=agent_name,
            tool_name=tool_name,
            tool_input=tool_input,
            summary=meta["summary"],
            severity="normal",
            client_id=meta["client_id"],
            invoice_id=meta["invoice_id"],
            tier=meta["tier"],
            dedupe_key=key,
            cycle_id=current_cycle_id(),
        )
        self._seen[key] = decision["id"]
        self.created.append(decision["id"])
        logger.info("approval-gate: queued %s as %s", meta["summary"], decision["id"])
        return Deny(reason=DENY_TEMPLATE.format(decision_id=decision["id"]))
