"""TASK-007 amounts (integer paisa, quantity, NPR formatting) and TASK-008 dates (AD/BS, N2/N3)."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import nepali_datetime
import pytest

from bahikhata.normalize import (
    format_npr,
    normalize_amounts,
    normalize_date,
    normalize_extraction,
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


# --- Dates (TASK-008) --------------------------------------------------------
# Expected AD/BS pairs below come from nepali-datetime itself (UNVERIFIED, see the
# TASK-003 Spike Log). These tests check parsing and calendar choice, not the
# library's calendar table; VERIFIED_PAIRS checks the table once San fills it in.

# (BS, AD) checked by San in Hamro Patro. TODO(San): fill in (same pairs as scratch/spike_bs_ad.py).
VERIFIED_PAIRS: list[tuple[str, str]] = []

BS_2083_06_14_AD = nepali_datetime.date(2083, 6, 14).to_datetime_date()  # library: 2026-09-30


def rule_ids(result) -> list[str]:
    return [f.rule_id for f in result.flags]


@pytest.mark.skipif(not VERIFIED_PAIRS, reason="Hamro Patro pairs not supplied yet (TODO San)")
@pytest.mark.parametrize("bs_text, ad_text", VERIFIED_PAIRS)
def test_verified_bs_ad_pairs(bs_text, ad_text):
    result = normalize_date(bs_text)
    assert result.date_ad == date.fromisoformat(ad_text)
    assert normalize_date(ad_text).date_bs == bs_text


@pytest.mark.parametrize("raw", [
    "2083-06-14",
    "2083/06/14",
    "2083.06.14",
    "2083-6-14",
    "2083-06-14 B.S.",
    "2083/06/14 BS",
    "2083-06-14 वि.सं.",
    "14/06/2083",
    "14-06-2083",
    "२०८३/०६/१४",
    "२०८३-०६-१४ वि.सं.",
    "Date: 2083/06/14",
    "मिति: २०८३/०६/१४",
    " 2083 / 06 / 14 ",
])
def test_bs_forms(raw):
    result = normalize_date(raw)
    assert (result.date_bs, result.bs_year, result.bs_month) == ("2083-06-14", 2083, 6)
    assert result.date_ad == BS_2083_06_14_AD
    assert result.flags == []


@pytest.mark.parametrize("raw", [
    "2026-09-30",
    "2026/09/30",
    "30/09/2026",
    "30-09-2026",
    "30.09.2026",
    "30/09/2026 14:22",
    "30/09/2026 2:22 PM",
    "2026-09-30 A.D.",
    "09/30/2026",          # day-first would mean month 30, so only MM/DD is valid
])
def test_ad_forms(raw):
    result = normalize_date(raw)
    assert result.date_ad == date(2026, 9, 30)
    assert result.date_bs == nepali_datetime.date.from_datetime_date(date(2026, 9, 30)).strftime("%Y-%m-%d")
    assert result.bs_year == 2083 and result.bs_month == int(result.date_bs[5:7])
    assert result.flags == []


def test_ambiguous_day_month_is_read_dd_mm_with_n3():
    result = normalize_date("03/04/2026")
    assert result.date_ad == date(2026, 4, 3)
    assert rule_ids(result) == ["N3"]
    assert result.flags[0].severity == "WARNING"


def test_same_day_and_month_is_not_ambiguous():
    assert normalize_date("05/05/2026").flags == []


@pytest.mark.parametrize("raw", [
    "2083-13-01",          # month 13
    "2083-00-10",
    "2083-08-30",          # library: Mangsir 2083 has 29 days
    "2083-06-32",
    "2026-02-30",
    "2026-13-01",
    "32/01/2026",
    "13/14/2026",          # no valid month either way
    "14 Sep",              # no year
    "14 Sep 2026",         # month names are not parsed
    "abc",
    "2083",
    "12.50.00.00",
    "2101-01-01",          # outside the library's BS range
])
def test_unreadable_dates_give_none_and_n2(raw):
    result = normalize_date(raw)
    assert result.date_ad is None and result.date_bs is None
    assert result.bs_year is None and result.bs_month is None
    assert rule_ids(result) == ["N2"]
    assert raw in result.flags[0].message


@pytest.mark.parametrize("raw, calendar", [
    ("2035-12-31", "AD"),
    ("2036-01-01", None),
    ("2069-12-30", None),
    ("2070-01-01", "BS"),
])
def test_year_range_boundaries(raw, calendar):
    result = normalize_date(raw)
    if calendar is None:
        assert result.date_ad is None and rule_ids(result) == ["N2"]
        assert "neither" in result.flags[0].message
    elif calendar == "AD":
        assert result.date_ad == date(2035, 12, 31) and result.flags == []
    else:
        assert result.date_bs == "2070-01-01" and result.flags == []


@pytest.mark.parametrize("raw, hint", [("2026-09-30", "BS"), ("30/09/2026", "BS"), ("2083-06-14", "AD")])
def test_hint_never_overrides_four_digit_year(raw, hint):
    result = normalize_date(raw, hint)
    assert result.date_ad in (date(2026, 9, 30), BS_2083_06_14_AD)
    expected_ad = BS_2083_06_14_AD if raw.startswith("2083") else date(2026, 9, 30)
    assert result.date_ad == expected_ad


def test_two_digit_year_uses_hint_only_then():
    assert normalize_date("14/06/83", "BS").date_bs == "2083-06-14"
    assert normalize_date("30/09/26", "AD").date_ad == date(2026, 9, 30)
    assert normalize_date("14/06/83 B.S.", "unknown").date_bs == "2083-06-14"  # printed marker
    for raw, hint in [("14/06/83", "AD"), ("14/06/83", "unknown"), ("14/06/83", None), ("30/09/26", "BS")]:
        result = normalize_date(raw, hint)
        assert result.date_ad is None and rule_ids(result) == ["N2"], (raw, hint)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_date_gives_no_flag(raw):
    result = normalize_date(raw)
    assert result.date_ad is None and result.flags == []


def test_normalize_extraction_fills_amounts_and_dates():
    extraction = ReceiptExtraction(date_raw="03/04/2026", date_calendar_hint="BS", total_raw="abc",
                                   subtotal_raw="1,000")
    draft, flags = normalize_extraction(extraction)
    assert draft.date_ad == date(2026, 4, 3)          # 4-digit AD year wins over the BS hint
    assert draft.date_bs is not None and draft.bs_year == 2082
    assert draft.date_raw == "03/04/2026"
    assert draft.subtotal_paisa == 100000 and draft.total_paisa is None
    assert [f.rule_id for f in flags] == ["N1", "N3"]
