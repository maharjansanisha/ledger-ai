"""TASK-010: validator V1-V9, status, save rules, immutability."""

import copy
from datetime import date, timedelta
from decimal import Decimal

import nepali_datetime
import pytest

from bahikhata import config
from bahikhata.schemas import ReceiptDraft, ValidationFlag
from bahikhata.validate import may_save, summarize_flags, validate_draft

TODAY = date(2026, 10, 3)
TOL = config.AMOUNT_TOLERANCE_PAISA  # 100 paisa

# Clean printed VAT bill: 2 × Rs 500 = Rs 1,000; VAT 13% = Rs 130; total Rs 1,130.
CLEAN = {
    "merchant_name": "Shree Traders",
    "merchant_pan": "123456789",
    "date_ad": date(2026, 9, 30),
    "date_bs": "2083-06-14",
    "bs_year": 2083,
    "bs_month": 6,
    "subtotal_paisa": 100000,
    "vat_paisa": 13000,
    "total_paisa": 113000,
    "category": "Inventory",
    "line_items": [{"description": "Rice", "quantity": Decimal("2"), "unit_price_paisa": 50000, "amount_paisa": 100000}],
}


def draft(**overrides) -> ReceiptDraft:
    return ReceiptDraft.model_validate({**CLEAN, **overrides})


def run(d: ReceiptDraft):
    return validate_draft(d, TODAY)


def ids(d: ReceiptDraft) -> list[str]:
    return [f.rule_id for f in run(d)[0]]


def test_clean_receipt_has_no_flags():
    flags, status, can_save, needs_override = run(draft())
    assert flags == [] and status == "clean" and can_save and not needs_override


# --- V1 required (BLOCKING) --------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("merchant_name", None), ("merchant_name", "   "), ("date_ad", None), ("total_paisa", None),
])
def test_v1_missing_required_field_blocks(field, value):
    flags, status, can_save, _ = run(draft(**{field: value}))
    v1 = [f for f in flags if f.rule_id == "V1"]
    assert [(f.severity, f.field) for f in v1] == [("BLOCKING", field)]
    assert status == "invalid" and not can_save


def test_v1_all_missing_on_empty_draft():
    flags, status, can_save, _ = run(ReceiptDraft())
    assert [f.field for f in flags if f.rule_id == "V1"] == ["merchant_name", "date_ad", "total_paisa"]
    assert "V9" in [f.rule_id for f in flags]
    assert status == "invalid" and not can_save


# --- V2 positive amounts (BLOCKING) ------------------------------------------

@pytest.mark.parametrize("overrides, field", [
    ({"total_paisa": 0}, "total_paisa"),
    ({"total_paisa": -1}, "total_paisa"),
    ({"discount_paisa": -1}, "discount_paisa"),
    ({"vat_paisa": -1}, "vat_paisa"),
    ({"line_items": [{"amount_paisa": -1}]}, "line_items[0].amount_paisa"),
    ({"line_items": [{"unit_price_paisa": -5}]}, "line_items[0].unit_price_paisa"),
])
def test_v2_zero_or_negative_blocks(overrides, field):
    flags, status, can_save, _ = run(draft(**overrides))
    v2 = [f for f in flags if f.rule_id == "V2"]
    assert [(f.severity, f.field) for f in v2] == [("BLOCKING", field)]
    assert not can_save


def test_v2_message_has_the_number():
    flags = run(draft(total_paisa=-1250))[0]
    assert "-12.50" in next(f.message for f in flags if f.rule_id == "V2")


def test_v2_one_paisa_total_is_fine():
    assert "V2" not in ids(draft(total_paisa=1))


# --- V3 line arithmetic (WARNING) --------------------------------------------

@pytest.mark.parametrize("amount, flagged", [
    (100000 + TOL, False), (100000 - TOL, False), (100000 + TOL + 1, True), (100000 - TOL - 1, True),
])
def test_v3_tolerance_boundary(amount, flagged):
    d = draft(line_items=[{"quantity": "2", "unit_price_paisa": 50000, "amount_paisa": amount}], subtotal_paisa=amount,
              total_paisa=amount + 13000)
    flags = run(d)[0]
    v3 = [f for f in flags if f.rule_id == "V3"]
    assert bool(v3) is flagged
    if flagged:
        assert v3[0].severity == "WARNING" and "1,000.00" in v3[0].message


def test_v3_fractional_quantity_rounds_half_up():
    # 1.5 kg × Rs 0.33 = 49.5 paisa -> 50
    d = draft(line_items=[{"quantity": "1.5", "unit_price_paisa": 33, "amount_paisa": 50}])
    assert "V3" not in ids(d)


@pytest.mark.parametrize("item", [
    {"quantity": None, "unit_price_paisa": 50000, "amount_paisa": 1},
    {"quantity": "2", "unit_price_paisa": None, "amount_paisa": 1},
    {"quantity": "2", "unit_price_paisa": 50000, "amount_paisa": None},
])
def test_v3_skipped_when_a_value_is_missing(item):
    assert "V3" not in ids(draft(line_items=[item]))


# --- V4 lines vs subtotal (WARNING) ------------------------------------------

@pytest.mark.parametrize("subtotal, flagged", [
    (100000 + TOL, False), (100000 - TOL, False), (100000 + TOL + 1, True), (100000 - TOL - 1, True),
])
def test_v4_tolerance_boundary(subtotal, flagged):
    assert ("V4" in ids(draft(subtotal_paisa=subtotal))) is flagged


def test_v4_skipped_without_subtotal_items_or_with_a_missing_amount():
    assert "V4" not in ids(draft(subtotal_paisa=None))
    assert "V4" not in ids(draft(line_items=[]))
    assert "V4" not in ids(draft(subtotal_paisa=1, line_items=[{"amount_paisa": 5}, {"amount_paisa": None}]))


# --- V5 bill arithmetic (ERROR, overridable) ----------------------------------

@pytest.mark.parametrize("total, flagged", [
    (113000 + TOL, False), (113000 - TOL, False), (113000 + TOL + 1, True), (113000 - TOL - 1, True),
])
def test_v5_tolerance_boundary(total, flagged):
    flags, status, can_save, needs_override = run(draft(total_paisa=total))
    v5 = [f for f in flags if f.rule_id == "V5"]
    assert bool(v5) is flagged
    if flagged:
        assert v5[0].severity == "ERROR"
        assert status == "invalid" and can_save and needs_override


def test_v5_message_shows_every_number():
    flags = run(draft(total_paisa=122000))[0]
    message = next(f.message for f in flags if f.rule_id == "V5")
    for number in ("1,000.00", "130.00", "1,130.00", "1,220.00", "90.00"):
        assert number in message


def test_v5_with_discount():
    # 1,000 − 100 = 900; VAT 13% of 900 = 117; total 1,017
    d = draft(discount_paisa=10000, vat_paisa=11700, total_paisa=101700)
    assert ids(d) == []


def test_v5_skipped_without_subtotal():
    assert "V5" not in ids(draft(subtotal_paisa=None, total_paisa=999999))


def test_vat_inclusive_bill_passes_v5_and_skips_v6():
    d = draft(vat_paisa=None, subtotal_paisa=113000, total_paisa=113000,
              line_items=[{"quantity": "2", "unit_price_paisa": 56500, "amount_paisa": 113000}])
    assert ids(d) == []


def test_restaurant_with_service_charge_passes_v5_and_v6():
    # subtotal 1,000 + 10% SC 100 = 1,100 taxable; VAT 13% = 143; total 1,243
    d = draft(service_charge_paisa=10000, vat_paisa=14300, total_paisa=124300)
    assert ids(d) == []


# --- V6 VAT rate (WARNING) ---------------------------------------------------

@pytest.mark.parametrize("vat, flagged", [
    (13000 + 200, False), (13000 - 200, False), (13000 + 201, True), (13000 - 201, True),  # min NPR 2 applies
])
def test_v6_small_bill_uses_npr_2_tolerance(vat, flagged):
    assert ("V6" in ids(draft(vat_paisa=vat, total_paisa=100000 + vat))) is flagged


@pytest.mark.parametrize("vat, flagged", [
    (1300000 + 13000, False), (1300000 + 13001, True),  # 1% of 1,300,000 = 13,000 > 200
])
def test_v6_large_bill_uses_one_percent_tolerance(vat, flagged):
    d = draft(subtotal_paisa=10000000, vat_paisa=vat, total_paisa=10000000 + vat, line_items=[])
    assert ("V6" in ids(d)) is flagged


def test_v6_is_warning_with_numbers():
    flag = next(f for f in run(draft(vat_paisa=15000, total_paisa=115000))[0] if f.rule_id == "V6")
    assert flag.severity == "WARNING" and "150.00" in flag.message and "130.00" in flag.message


# --- V7 PAN (WARNING) ---------------------------------------------------------

@pytest.mark.parametrize("pan, flagged", [
    ("123456789", False), (" 123456789 ", False), (None, False),
    ("12345678", True), ("1234567890", True), ("12345678a", True), ("१२३४५६७८९", True),
])
def test_v7_pan_format(pan, flagged):
    flags = [f for f in run(draft(merchant_pan=pan))[0] if f.rule_id == "V7"]
    assert bool(flags) is flagged
    assert all(f.severity == "WARNING" for f in flags)


# --- V8 date plausibility (WARNING) ------------------------------------------

def test_v8_future_and_old_dates():
    earliest = nepali_datetime.date(2075, 1, 1).to_datetime_date()
    assert "V8" not in ids(draft(date_ad=TODAY))
    assert "V8" in ids(draft(date_ad=TODAY + timedelta(days=1)))
    assert "V8" not in ids(draft(date_ad=earliest))
    assert "V8" in ids(draft(date_ad=earliest - timedelta(days=1)))
    flags, status, can_save, _ = run(draft(date_ad=TODAY + timedelta(days=1)))
    assert status == "needs_review" and can_save


# --- V9 category (BLOCKING) ---------------------------------------------------

def test_v9_missing_category_blocks():
    flags, status, can_save, _ = run(draft(category=None))
    assert [(f.rule_id, f.severity) for f in flags] == [("V9", "BLOCKING")]
    assert not can_save and "Office Supplies" in flags[0].message


# --- Status and save rules ----------------------------------------------------

def flag(severity, rule_id="V3"):
    return ValidationFlag(rule_id=rule_id, severity=severity, message="m")


@pytest.mark.parametrize("severities, expected", [
    ([], ("clean", True, False)),
    (["WARNING"], ("needs_review", True, False)),
    (["WARNING", "ERROR"], ("invalid", True, True)),
    (["ERROR", "BLOCKING"], ("invalid", False, False)),
    (["BLOCKING"], ("invalid", False, False)),
])
def test_summarize_flags(severities, expected):
    assert summarize_flags([flag(s) for s in severities]) == expected


def test_n_flags_count_toward_status():
    n3 = ValidationFlag(rule_id="N3", severity="WARNING", message="m")
    assert summarize_flags([n3])[0] == "needs_review"


@pytest.mark.parametrize("severities, override, allowed", [
    ([], False, True),
    (["WARNING"], False, True),
    (["ERROR"], False, False),
    (["ERROR"], True, True),
    (["BLOCKING"], True, False),
    (["BLOCKING", "ERROR"], True, False),
])
def test_may_save(severities, override, allowed):
    assert may_save([flag(s) for s in severities], user_override=override) is allowed


# --- Immutability -------------------------------------------------------------

@pytest.mark.parametrize("d", [
    draft(), ReceiptDraft(), draft(total_paisa=-1, merchant_pan="x", vat_paisa=1, category=None,
                                    date_ad=TODAY + timedelta(days=9)),
], ids=["clean", "empty", "many-flags"])
def test_validator_never_mutates_its_input(d):
    before = copy.deepcopy(d.model_dump())
    validate_draft(d, TODAY)
    assert d.model_dump() == before
