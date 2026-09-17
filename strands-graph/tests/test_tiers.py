from chaser.tiers import email_mentions_payment, late_fee_amount, select_tier


def test_tiers_by_age() -> None:
    assert select_tier(9).tier == 1
    assert select_tier(23).tier == 2
    assert select_tier(41, late_fee_clause=True).tier == 3
    assert select_tier(41, late_fee_clause=True).mentions_late_fee is True
    assert select_tier(41, late_fee_clause=False).mentions_late_fee is False
    assert select_tier(67).tier == 4
    assert select_tier(0).tier == 0


def test_history_bumps_tier() -> None:
    # one reminder already sent on a young debt: go firm
    assert select_tier(12, prior_reminders=1, days_since_last_reminder=20).tier == 2
    # two reminders already: bump one tier (cap at 3 before 61 days)
    assert select_tier(23, prior_reminders=2, days_since_last_reminder=20).tier == 3


def test_holds() -> None:
    d = select_tier(
        67, prior_reminders=2, days_since_last_reminder=31, days_since_client_email=7, client_email_about_payment=True
    )
    assert d.tier == 0 and "client wrote" in d.reason
    d = select_tier(23, prior_reminders=1, days_since_last_reminder=3)
    assert d.tier == 0 and "reminder already sent" in d.reason
    d = select_tier(9, days_since_owner_declined=2)
    assert d.tier == 0 and "owner declined" in d.reason
    # a non-payment email (project chatter) does not hold
    assert select_tier(9, days_since_client_email=1, client_email_about_payment=False).tier == 1


def test_gentle_client_cap_and_late_fee() -> None:
    assert select_tier(45, gentle_client=True, late_fee_clause=True).tier == 2
    assert select_tier(70, gentle_client=True).tier == 4
    assert late_fee_amount(3200.0, 1.5, 41) == 65.6
    assert late_fee_amount(3200.0, None, 41) == 0.0


def test_email_payment_detection() -> None:
    assert email_mentions_payment("Re: INV-1034", "finance is processing this week")
    assert email_mentions_payment("INV-1040", "please resend to ap@")
    assert not email_mentions_payment("October batch timing", "could we get drafts by the 3rd")
