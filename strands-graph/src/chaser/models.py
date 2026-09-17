"""Pydantic schemas: the structured WeeklyCloseReport and payload shapes."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgingBuckets(BaseModel):
    current: float = Field(0.0, description="Outstanding, not yet due")
    d1_30: float = Field(0.0, description="Outstanding, 1-30 days overdue")
    d31_60: float = Field(0.0, description="Outstanding, 31-60 days overdue")
    d61_plus: float = Field(0.0, description="Outstanding, 61+ days overdue")


class ReconciliationSummary(BaseModel):
    deposits_matched: int = 0
    partial_payments_recorded: int = 0
    deposits_flagged_for_review: int = 0
    notes: list[str] = Field(default_factory=list, description="One line per deposit handled")


class DraftAwaitingApproval(BaseModel):
    client: str
    invoice_id: str
    tier: int = Field(description="1 gentle, 2 firm, 3 final, 4 escalate")
    amount: float
    action: str = Field(description="e.g. send reminder, resend invoice, offer payment plan, write-off")


class BooksHealth(BaseModel):
    expenses_categorized: int = 0
    receipts_matched: int = 0
    expenses_missing_receipts: int = 0
    todos_created: int = 0


class WeeklyCloseReport(BaseModel):
    """Structured output produced by the reporter node at the end of each weekly close."""

    period_start: str = Field(description="ISO date, Monday of the week")
    period_end: str = Field(description="ISO date, the day the close ran")
    cash_collected_this_week: float
    outstanding: AgingBuckets
    reconciliation: ReconciliationSummary
    drafts_awaiting_approval: list[DraftAwaitingApproval] = Field(default_factory=list)
    books: BooksHealth
    top_things_to_know: list[str] = Field(default_factory=list, max_length=3)
    narrative: str = Field(description="Two or three plain sentences for the owner")
