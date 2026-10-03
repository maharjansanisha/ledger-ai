"""TASK-006: schema behaviour (lenient extraction in, strict confirmed record out)."""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bahikhata.schemas import (
    Category,
    ConfirmedLineItem,
    ConfirmedReceipt,
    GroundTruthReceipt,
    QueryPlan,
    ReceiptDraft,
    ReceiptExtraction,
    ValidationFlag,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_0001 = PROJECT_ROOT / "migrations" / "versions" / "0001_create_ledger_tables.py"
SPIKE_OUTPUTS = sorted((PROJECT_ROOT / "scratch" / "outputs").glob("*.json"))  # gitignored

MINIMAL_CONFIRMED = {
    "merchant_name": "Shree Traders",
    "date_ad": "2026-09-30",
    "date_bs": "2083-06-14",
    "bs_year": 2083,
    "bs_month": 6,
    "total_paisa": 125000,
    "category": "Inventory",
    "status": "clean",
}


def _table_columns(table: str) -> set[str]:
    """Column names of `CREATE TABLE <table> (...)` in migration 0001."""
    sql = MIGRATION_0001.read_text()
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\s*\)\n", sql, re.S).group(1)
    return {
        m.group(1)
        for line in body.splitlines()
        if (m := re.match(r"\s*([a-z_]+)\s+(BIGINT|TEXT|DATE|SMALLINT|BOOLEAN|TIMESTAMPTZ|REAL)\b", line))
    }


def _check_values(column: str) -> set[str]:
    """String literals in the `CHECK (<column> IN (...))` constraint of migration 0001."""
    sql = MIGRATION_0001.read_text()
    in_list = re.search(rf"CHECK \({column} IN \((.*?)\)\)", sql, re.S).group(1)
    return set(re.findall(r"'([^']+)'", in_list))


# --- ReceiptExtraction: lenient ----------------------------------------------

def test_extraction_parses_empty_object():
    extraction = ReceiptExtraction.model_validate({})
    assert extraction.total_raw is None
    assert extraction.line_items == []


def test_extraction_keeps_amount_text_unparsed():
    extraction = ReceiptExtraction.model_validate({
        "total_raw": "Rs. 1,250.00",
        "subtotal_raw": "1,25,000/-",
        "date_raw": "२०८३/०६/१४",
        "category": "not a real category",  # V9's job, not the parser's
        "line_items": [{"description": "Rice", "amount_raw": "Rs 500"}],
    })
    assert extraction.total_raw == "Rs. 1,250.00"
    assert extraction.subtotal_raw == "1,25,000/-"
    assert extraction.line_items[0].amount_raw == "Rs 500"
    assert extraction.line_items[0].quantity_raw is None


@pytest.mark.skipif(not SPIKE_OUTPUTS, reason="no local TASK-002 spike outputs (gitignored)")
@pytest.mark.parametrize("path", SPIKE_OUTPUTS, ids=lambda p: p.stem)
def test_extraction_parses_real_spike_output(path):
    ReceiptExtraction.model_validate_json(path.read_text())


# --- ReceiptDraft: typed but still representable when wrong -----------------

def test_draft_allows_nulls_and_negative_amounts_for_the_validator_to_flag():
    draft = ReceiptDraft(total_paisa=-500, line_items=[{"amount_paisa": -1}])
    assert draft.total_paisa == -500
    assert draft.merchant_name is None


def test_draft_rejects_float_money():
    with pytest.raises(ValidationError):
        ReceiptDraft(total_paisa=12.5)


# --- ConfirmedReceipt: strict ------------------------------------------------

def test_confirmed_minimal_is_valid():
    receipt = ConfirmedReceipt.model_validate(MINIMAL_CONFIRMED)
    assert receipt.date_ad == date(2026, 9, 30)
    assert receipt.category is Category.INVENTORY
    assert receipt.user_override is False
    assert receipt.line_items == []


def test_confirmed_full_is_valid():
    receipt = ConfirmedReceipt.model_validate({
        **MINIMAL_CONFIRMED,
        "merchant_pan": "123456789",
        "invoice_number": "INV-42",
        "subtotal_paisa": 110619,
        "discount_paisa": 0,
        "service_charge_paisa": 0,
        "vat_paisa": 14381,
        "status": "invalid",
        "user_override": True,
        "line_items": [
            {"description": "Rice 25kg", "quantity": "1.5", "unit_price_paisa": 73746, "amount_paisa": 110619},
        ],
    })
    assert receipt.line_items[0].quantity == Decimal("1.5")
    assert receipt.vat_paisa == 14381


def test_confirmed_strips_merchant_name():
    assert ConfirmedReceipt.model_validate({**MINIMAL_CONFIRMED, "merchant_name": "  Shree Traders "}).merchant_name == "Shree Traders"


@pytest.mark.parametrize("field", ["total_paisa", "merchant_name", "date_ad", "category", "status"])
def test_confirmed_rejects_missing_required_field(field):
    data = dict(MINIMAL_CONFIRMED)
    del data[field]
    with pytest.raises(ValidationError):
        ConfirmedReceipt.model_validate(data)


@pytest.mark.parametrize("bad", [
    {"total_paisa": 0},
    {"total_paisa": -100},
    {"total_paisa": 12.5},
    {"total_paisa": 1250.0},              # floats are never money, even whole ones
    {"total_paisa": "125000"},
    {"subtotal_paisa": -1},
    {"discount_paisa": -1},
    {"service_charge_paisa": -1},
    {"vat_paisa": -1},
    {"vat_paisa": 12.5},
    {"line_items": [{"amount_paisa": -1}]},
    {"line_items": [{"unit_price_paisa": -1}]},
    {"line_items": [{"amount_paisa": 99.5}]},
    {"category": "Groceries"},
    {"category": "inventory"},
    {"status": "approved"},
    {"bs_month": 0},
    {"bs_month": 13},
    {"merchant_name": ""},
    {"merchant_name": "   "},
    {"date_bs": "14/06/2083"},
    {"image_path": "data/images/x.jpg"},  # supplied to the save function, not the model
], ids=str)
def test_confirmed_rejects_bad_value(bad):
    with pytest.raises(ValidationError):
        ConfirmedReceipt.model_validate({**MINIMAL_CONFIRMED, **bad})


# --- Consistency with the Alembic migration (TASK-009 maps 1:1) --------------

def test_confirmed_receipt_fields_match_receipts_columns():
    db_only = {"id", "created_at", "image_path"}  # generated by the DB / passed at save time
    expected = (_table_columns("receipts") - db_only) | {"line_items"}
    assert set(ConfirmedReceipt.model_fields) == expected


def test_confirmed_line_item_fields_match_line_items_columns():
    db_only = {"id", "receipt_id", "line_no"}  # line_no = list position at save time
    assert set(ConfirmedLineItem.model_fields) == _table_columns("line_items") - db_only


def test_category_and_status_match_db_checks():
    assert {c.value for c in Category} == _check_values("category")
    assert set(ConfirmedReceipt.model_fields["status"].annotation.__args__) == _check_values("status")


# --- Other models ------------------------------------------------------------

def test_validation_flag_rule_ids_and_severity():
    ValidationFlag(rule_id="V10", severity="WARNING", message="possible duplicate")
    ValidationFlag(rule_id="EXTRACTION_FAILED", severity="BLOCKING", message="x")
    with pytest.raises(ValidationError):
        ValidationFlag(rule_id="V11", severity="WARNING", message="x")
    with pytest.raises(ValidationError):
        ValidationFlag(rule_id="V1", severity="FATAL", message="x")


def test_ground_truth_label_catches_typos():
    label = {"id": "r001", "split": "dev", "slice": "thermal", "total": "1250.50"}
    GroundTruthReceipt.model_validate(label)
    with pytest.raises(ValidationError):
        GroundTruthReceipt.model_validate({**label, "total": "1,250.50"})
    with pytest.raises(ValidationError):
        GroundTruthReceipt.model_validate({**label, "totl": "1250.50"})


def test_query_plan_status():
    assert QueryPlan.model_validate({"status": "out_of_scope"}).sql is None
    with pytest.raises(ValidationError):
        QueryPlan.model_validate({"status": "maybe"})
