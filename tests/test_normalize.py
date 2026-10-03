"""TASK-007: amount parsing to integer paisa, quantity parsing, NPR formatting."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bahikhata.normalize import (
    format_npr,
    normalize_amounts,
    parse_amount_to_paisa,
    parse_quantity,
)
from bahikhata.schemas import ReceiptExtraction

SPIKE_OUTPUTS = sorted(
    (Path(__file__).resolve().parent.parent / "scratch" / "outputs").glob("*.json")
)  # gitignored; present only on San's machine


@pytest.mark.parametrize("text, paisa", [
    ("1,250.00", 125000),
    ("Rs. 1,250.00", 125000),
    ("Rs. 1250/-", 125000),
    ("Rs.1250", 125000),
    ("Rs 1250", 125000),
    ("NRs 1,250", 125000),
    ("NPR 1250", 125000),
    ("रू 1,250", 125000),
    ("रु. 1,250.00", 125000),
    ("1,25,000/-", 12500000),           # Indian lakh grouping
    ("12,50,000", 125000000),
    ("1,250,000", 125000000),
    ("१२५०", 125000),                    # Devanagari digits
    ("१,२५०.५०", 125050),
    ("1250.5", 125050),
    ("  1250.00  ", 125000),
    ("Rs.  1,250 .00", 125000),          # stray spaces
    ("1250 only", 125000),
    ("Rs. 1250/- Only", 125000),
    ("0", 0),
    ("0.05", 5),
    ("1234567.89", 123456789),          # exact; a float path would drift
    ("0.29", 29),                       # float(0.29) * 100 == 28.999999999999996
])
def test_parse_amount_accepts(text, paisa):
    result = parse_amount_to_paisa(text)
    assert result == paisa
    assert type(result) is int


@pytest.mark.parametrize("text", [
    None,
    "",
    "   ",
    "abc",
    "Rs.",
    "12.50.00",          # multiple decimal points
    "1250.505",          # more than 2 decimal places
    "12O0",              # letter O instead of zero
    "1250abc",
    "-1250",
    "(1250)",
    "1250-",
    "12,50",             # comma grouping that is not thousands/lakh: ambiguous
    "1,2,5",
    ".50",
    "Rs 1,250 / 1,300",
])
def test_parse_amount_rejects(text):
    assert parse_amount_to_paisa(text) is None


@pytest.mark.parametrize("text, expected", [
    ("2", Decimal("2")),
    ("1.5", Decimal("1.5")),
    ("1.5 kg", Decimal("1.5")),
    ("3 pcs", Decimal("3")),
    ("३", Decimal("3")),
    (None, None),
    ("", None),
    ("two", None),
    ("-1", None),
])
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


@pytest.mark.parametrize("paisa, text", [
    (125050, "Rs 1,250.50"),
    (0, "Rs 0.00"),
    (5, "Rs 0.05"),
    (12500000, "Rs 125,000.00"),
    (-500, "-Rs 5.00"),
])
def test_format_npr(paisa, text):
    assert format_npr(paisa) == text


def test_normalize_amounts_builds_draft_and_n1_flags():
    extraction = ReceiptExtraction(
        merchant_name="Shree Traders",
        date_raw="2083/06/14",
        date_calendar_hint="BS",
        subtotal_raw="Rs. 1,000.00",
        vat_amount_raw="130",
        total_raw="12.50.00",
        category="Inventory",
        line_items=[
            {"description": "Rice", "quantity_raw": "2", "unit_price_raw": "500", "amount_raw": "1,000"},
            {"description": "Oil", "amount_raw": "abc"},
        ],
    )
    before = extraction.model_dump()
    draft, flags = normalize_amounts(extraction)

    assert extraction.model_dump() == before  # input not modified
    assert draft.subtotal_paisa == 100000
    assert draft.vat_paisa == 13000
    assert draft.total_paisa is None
    assert draft.discount_paisa is None  # absent: no flag
    assert draft.merchant_name == "Shree Traders"
    assert draft.date_raw == "2083/06/14"
    assert draft.date_ad is None and draft.bs_month is None  # dates are TASK-008
    assert draft.line_items[0].quantity == Decimal("2")
    assert draft.line_items[0].amount_paisa == 100000
    assert draft.line_items[1].amount_paisa is None

    assert [(f.rule_id, f.severity, f.field) for f in flags] == [
        ("N1", "WARNING", "total_paisa"),
        ("N1", "WARNING", "line_items[1].amount_paisa"),
    ]
    assert "12.50.00" in flags[0].message


def test_normalize_amounts_on_empty_extraction():
    draft, flags = normalize_amounts(ReceiptExtraction())
    assert flags == []
    assert draft.total_paisa is None and draft.line_items == []


@pytest.mark.skipif(not SPIKE_OUTPUTS, reason="no local TASK-002 spike outputs (gitignored)")
@pytest.mark.parametrize("path", SPIKE_OUTPUTS, ids=lambda p: p.stem)
def test_real_spike_total_raw_parses(path):
    total_raw = json.loads(path.read_text()).get("total_raw")
    if total_raw is None:
        pytest.skip("total_raw is null in this spike output")
    paisa = parse_amount_to_paisa(total_raw)
    assert isinstance(paisa, int) and paisa > 0
