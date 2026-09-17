"""Hooks shared by every node of the Graph.

- ``AuditHook`` records every completed tool call into the ``actions`` table (the activity feed).
- ``ProgressHook`` records live narration into ``progress`` as the run happens: "thinking" before
  each model call, what the agent said, which tool it is about to call, and each result. The web
  UI polls this while the weekly close runs so a two-minute Graph never looks hung.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)

from .context import current_cycle_id, get_store

logger = logging.getLogger(__name__)


def _result_text(result: dict[str, Any]) -> str:
    for block in result.get("content", []) or []:
        if "text" in block:
            return str(block["text"])
        if "json" in block:
            return str(block["json"])
    return ""


def summarize_action(tool_name: str, tool_input: dict[str, Any], result_text: str, status: str) -> str:
    """One human-readable line for the activity feed."""
    i = tool_input
    if status == "gated":
        return f"Proposed {tool_name.replace('_', ' ')} for {i.get('invoice_id', '')} (awaiting owner approval)"
    match tool_name:
        case "match_deposit_to_invoice":
            return f"Matched deposit {i.get('deposit_id')} to {i.get('invoice_id')}" + (
                f" ({i['note']})" if i.get("note") else ""
            )
        case "record_partial_payment":
            return f"Recorded ${float(i.get('amount', 0)):,.2f} partial payment on {i.get('invoice_id')}"
        case "flag_deposit_for_review":
            return f"Flagged deposit {i.get('deposit_id')} for owner review"
        case "note_client_context":
            return f"Noted for {i.get('client_id')}: {i.get('note', '')}"
        case "draft_followup":
            return f"Drafted tier-{i.get('tier')} follow-up for {i.get('invoice_id')}"
        case "send_client_email":
            return f"Sent email to {i.get('to')} about {i.get('invoice_id')}: {i.get('subject', '')}"
        case "offer_payment_plan":
            return f"Offered payment plan on {i.get('invoice_id')}: {i.get('terms', '')}"
        case "propose_write_off":
            return f"Wrote off {i.get('invoice_id')}: {i.get('reason', '')}"
        case "categorize_expense":
            return f"Categorized {i.get('expense_id')} as {i.get('category')}"
        case "match_receipt":
            return f"Attached receipt {i.get('receipt_id')} to {i.get('expense_id')}"
        case "create_todo":
            return f"Created to-do: {i.get('title')}"
        case _:
            if tool_name.startswith(("list_", "get_")):
                return f"Read {tool_name.replace('_', ' ')}"
            return f"{tool_name} {result_text[:120]}"


class AuditHook(HookProvider):
    """Append an audit row after each tool call; the row carries the calling agent's name."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "?")
        if tool_name[:1].isupper():
            return  # internal structured-output tool (e.g. WeeklyCloseReport)
        tool_input = dict(event.tool_use.get("input") or {})
        result = event.result or {}
        status = str(result.get("status", "unknown"))
        text = _result_text(result)
        kind = None
        cancel = event.cancel_message or ""
        if cancel.startswith("DENIED") or text.startswith("DENIED"):
            status, kind = "gated", "gated"
        if '"error"' in text[:40] and status == "success":
            status = "error"
        agent_name = getattr(event.agent, "name", None) or "agent"
        try:
            get_store().add_action(
                agent=agent_name,
                tool_name=tool_name,
                tool_input=tool_input,
                summary=summarize_action(tool_name, tool_input, text, status),
                status=status,
                cycle_id=current_cycle_id(),
                kind=kind,
            )
        except Exception:  # never let auditing break the agent loop
            logger.exception("audit hook failed for %s", tool_name)


def _short_input(tool_input: dict[str, Any], limit: int = 160) -> str:
    text = json.dumps(tool_input, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class ProgressHook(HookProvider):
    """Live narration per node; each line carries the agent's name so the UI can show who is working."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(MessageAddedEvent, self._message_added)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    @staticmethod
    def _write(agent: Any, kind: str, text: str) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        try:
            get_store().add_progress(current_cycle_id(), getattr(agent, "name", None) or "agent", kind, text)
        except Exception:  # never let narration break the agent loop
            logger.exception("progress hook failed")

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        self._write(event.agent, "thinking", "Thinking...")

    def _message_added(self, event: MessageAddedEvent) -> None:
        message = event.message or {}
        if message.get("role") != "assistant":
            return
        for block in message.get("content") or []:
            if "text" in block:
                self._write(event.agent, "said", block["text"])
            elif "toolUse" in block:
                tool_use = block["toolUse"] or {}
                name = str(tool_use.get("name") or "tool")
                if name[:1].isupper():
                    self._write(event.agent, "calling", "Writing the report")
                    continue
                self._write(event.agent, "calling", f"{name} {_short_input(dict(tool_use.get('input') or {}))}")

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = str((event.tool_use or {}).get("name") or "tool")
        if name[:1].isupper():
            return
        if event.cancel_message:
            self._write(event.agent, "done", f"{name}: proposed for your approval")
            return
        status = str((event.result or {}).get("status", "done"))
        self._write(event.agent, "done", f"{name}: {status}")
