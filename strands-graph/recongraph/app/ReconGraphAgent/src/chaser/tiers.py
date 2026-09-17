"""Follow-up tier selection for overdue invoices.

Pure logic; the collector agent gets the recommendation in ``list_overdue_invoices()``
and can override it with judgment, but the default keeps tone consistent week to week.

Tiers:
    0 hold     : do not chase this week (client just wrote, reminder sent days ago, owner declined)
    1 gentle   : 1-14 days overdue, friendly nudge
    2 firm     : 15-30 days, direct, includes payment link and due date
    3 final    : 31-60 days, final notice; mentions the late fee if the contract has one
    4 escalate : 61+ days, recommend a phone call or a write-off decision
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

TIER_LABELS = {0: "hold", 1: "gentle", 2: "firm", 3: "final", 4: "escalate"}

HOLD_IF_CLIENT_WROTE_WITHIN_DAYS = 7
HOLD_IF_REMINDER_WITHIN_DAYS = 6
HOLD_IF_OWNER_DECLINED_WITHIN_DAYS = 10
PAYMENT_KEYWORDS = (
    "paid",
    "processing",
    "wire sent",
    "wire for",
    "check is",
    "check for",
    "in the mail",
    "resend",
    "re-send",
    "push to",
    "extension",
    "pay on",
    "pay by",
    "will pay",
    "payment is",
    "payment will",
    "sending half",
    "payment plan",
    "released today",
)


@dataclass
class TierDecision:
    tier: int
    label: str
    reason: str
    mentions_late_fee: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def tier_for_age(days_overdue: int) -> int:
    if days_overdue <= 0:
        return 0
    if days_overdue <= 14:
        return 1
    if days_overdue <= 30:
        return 2
    if days_overdue <= 60:
        return 3
    return 4


def select_tier(
    days_overdue: int,
    *,
    prior_reminders: int = 0,
    days_since_last_reminder: int | None = None,
    days_since_client_email: int | None = None,
    client_email_about_payment: bool = False,
    days_since_owner_declined: int | None = None,
    gentle_client: bool = False,
    late_fee_clause: bool = False,
) -> TierDecision:
    """Choose a follow-up tier from age, history and recent client contact.

    Args:
        days_overdue: days past the due date (<= 0 means not yet due).
        prior_reminders: reminders already sent for this invoice.
        days_since_last_reminder: age of the most recent reminder, if any.
        days_since_client_email: age of the client's most recent email, if any.
        client_email_about_payment: whether that email talks about paying / resending.
        days_since_owner_declined: age of an owner "skip this week" for this client, if any.
        gentle_client: relationship notes ask for a soft touch; caps at tier 2 until 60+ days.
        late_fee_clause: contract has a late fee, so the final notice may cite it.
    """
    if days_overdue <= 0:
        return TierDecision(0, "hold", "not yet due")

    if days_since_owner_declined is not None and days_since_owner_declined <= HOLD_IF_OWNER_DECLINED_WITHIN_DAYS:
        return TierDecision(0, "hold", f"owner declined a reminder {days_since_owner_declined} days ago")

    if (
        client_email_about_payment
        and days_since_client_email is not None
        and days_since_client_email <= HOLD_IF_CLIENT_WROTE_WITHIN_DAYS
    ):
        return TierDecision(
            0, "hold", f"client wrote about payment {days_since_client_email} days ago; respond to that instead"
        )

    if days_since_last_reminder is not None and days_since_last_reminder <= HOLD_IF_REMINDER_WITHIN_DAYS:
        return TierDecision(0, "hold", f"reminder already sent {days_since_last_reminder} days ago")

    tier = tier_for_age(days_overdue)
    reason = f"{days_overdue} days overdue"

    if prior_reminders >= 2 and tier < 3:
        tier += 1
        reason += f", {prior_reminders} reminders already sent"
    elif prior_reminders >= 1 and tier == 1:
        tier = 2
        reason += ", one reminder already sent"

    if gentle_client and tier > 2 and days_overdue <= 60:
        tier = 2
        reason += "; relationship notes ask for a gentle tone"

    mentions_fee = tier >= 3 and late_fee_clause
    if mentions_fee:
        reason += "; contract late fee applies"
    return TierDecision(tier, TIER_LABELS[tier], reason, mentions_fee)


def late_fee_amount(outstanding: float, pct_per_month: float | None, days_overdue: int) -> float:
    """Accrued late fee, prorated by month (30 days), rounded to cents."""
    if not pct_per_month or days_overdue <= 0:
        return 0.0
    months = days_overdue / 30.0
    return round(outstanding * (pct_per_month / 100.0) * months, 2)


def email_mentions_payment(subject: str | None, body: str | None, invoice_id: str | None = None) -> bool:
    """True when an email talks about paying, resending or rescheduling (or names ``invoice_id``)."""
    text = f"{subject or ''} {body or ''}".lower()
    if invoice_id and invoice_id.lower() in text:
        return True
    return any(k in text for k in PAYMENT_KEYWORDS)
