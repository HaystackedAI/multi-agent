from chaser.matching import match_deposit, memo_mentions_client, suggest_category, suggest_receipts

INVOICES = [
    {"id": "INV-A", "client_id": "C1", "amount": 920.0, "amount_paid": 0.0},
    {"id": "INV-B", "client_id": "C2", "amount": 2150.0, "amount_paid": 0.0},
    {"id": "INV-C", "client_id": "C3", "amount": 1200.0, "amount_paid": 0.0},
    {"id": "INV-D", "client_id": "C2", "amount": 350.0, "amount_paid": 0.0},
    {"id": "INV-E", "client_id": "C4", "amount": 1000.0, "amount_paid": 0.0},
]
NAMES = {"C1": "Greenleaf Landscaping", "C2": "Northwind Dental", "C3": "Harbor & Vine Cafe", "C4": "Acme"}


def test_exact_match_ranks_first() -> None:
    c = match_deposit(920.0, INVOICES, memo="CHECK 2231 GREENLEAF", client_names=NAMES)
    assert c[0].kind == "exact" and c[0].invoice_ids == ["INV-A"] and c[0].confidence == 1.0


def test_fee_adjusted_wire() -> None:
    c = match_deposit(2120.0, INVOICES, memo="INCOMING WIRE NORTHWIND", client_names=NAMES)
    top = c[0]
    assert top.kind == "fee_adjusted" and top.invoice_ids == ["INV-B"] and top.fee == 30.0
    assert "wire fee $30" in top.note


def test_fee_adjusted_card_percent() -> None:
    gross = 1000.0
    net = round(gross - (gross * 0.029 + 0.30), 2)
    c = match_deposit(net, INVOICES, memo="STRIPE PAYOUT", client_names=NAMES)
    assert c[0].kind == "fee_adjusted" and c[0].invoice_ids == ["INV-E"]


def test_combined_same_client() -> None:
    c = match_deposit(2500.0, INVOICES, memo="NORTHWIND DENTAL", client_names=NAMES)
    assert c[0].kind == "combined" and sorted(c[0].invoice_ids) == ["INV-B", "INV-D"]
    assert sum(a["amount"] for a in c[0].allocations) == 2500.0


def test_partial_with_memo_hint() -> None:
    c = match_deposit(600.0, INVOICES, memo="ACH CREDIT HARBOR AND VINE", client_names=NAMES)
    assert c[0].kind == "partial" and c[0].invoice_ids == ["INV-C"]
    assert "exactly half" in c[0].note


def test_unknown_deposit_has_no_confident_candidate() -> None:
    c = match_deposit(500.0, INVOICES, memo="ZELLE J. PARK", client_names=NAMES)
    assert all(x.confidence < 0.5 for x in c)


def test_memo_tokens() -> None:
    assert memo_mentions_client("ACH CREDIT HARBOR AND VINE CAFE", "Harbor & Vine Cafe")
    assert not memo_mentions_client("ZELLE J. PARK", "Harbor & Vine Cafe")


def test_receipt_and_category_suggestions() -> None:
    expense = {"id": "TX-1", "date": "2026-09-01", "amount": -450.0, "merchant": "Foundry Coworking", "memo": "FOUNDRY"}
    receipts = [
        {"id": "R1", "date": "2026-09-01", "merchant": "Foundry Coworking", "amount": 450.0},
        {"id": "R2", "date": "2026-09-02", "merchant": "Other", "amount": 450.0},
        {"id": "R3", "date": "2026-09-20", "merchant": "Foundry Coworking", "amount": 450.0},
    ]
    s = suggest_receipts(expense, receipts)
    assert [r["receipt_id"] for r in s] == ["R1", "R2"] and s[0]["confidence"] >= 0.8
    chart = {"expense_categories": [{"id": "rent_coworking", "hints": ["coworking"]}], "non_pnl": []}
    assert suggest_category(expense, chart) == "rent_coworking"
    assert suggest_category({"merchant": "Mystery", "memo": ""}, chart) is None
