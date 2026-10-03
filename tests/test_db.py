"""TASK-009: save transaction and read helpers on Neon Postgres.

DB tests run ONLY against TEST_DATABASE_URL (Neon "ledger_test", migrated with
`make migrate TARGET=test`). They skip if it is not set, and refuse to run if it
points at the main database or at a database whose name does not end in "_test".
Each test starts and ends with empty tables.
"""

import os
import re
import types
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from bahikhata import db
from bahikhata.schemas import ConfirmedReceipt

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()  # .env loaded by bahikhata.config

MINIMAL = {
    "merchant_name": "Shree Traders",
    "date_ad": "2026-09-30",
    "date_bs": "2083-06-14",
    "bs_year": 2083,
    "bs_month": 6,
    "total_paisa": 125000,
    "category": "Inventory",
    "status": "clean",
}
FULL = {
    **MINIMAL,
    "merchant_pan": "123456789",
    "invoice_number": "INV-42",
    "subtotal_paisa": 110619,
    "discount_paisa": 0,
    "service_charge_paisa": 0,
    "vat_paisa": 14381,
    "status": "invalid",
    "user_override": True,
    "line_items": [
        {"description": "Rice 25kg", "quantity": "1.5", "unit_price_paisa": 50000, "amount_paisa": 75000},
        {"description": "Oil", "quantity": "2", "unit_price_paisa": 17810, "amount_paisa": 35619},
        {"description": None, "quantity": None, "unit_price_paisa": None, "amount_paisa": None},
    ],
}
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def make_audit(**overrides) -> db.AuditRecord:
    fields = {
        "model_name": "gemini-3.5-flash-lite",
        "prompt_version": "extraction_v1",
        "image_sha256": "ab" * 32,
        "raw_response": '{"total_raw": "1,250.00"}',
        "ai_extraction_json": {"total_raw": "1,250.00"},
        "ai_draft_json": {"total_paisa": 125000},
        "flags_at_extraction_json": [{"rule_id": "V6", "severity": "WARNING", "field": "vat_paisa", "message": "m"}],
        "flags_at_save_json": [],
        "edited_fields_json": ["total_paisa"],
        "extracted_at": NOW,
        "confirmed_at": NOW,
    }
    return db.AuditRecord(**{**fields, **overrides})


# --- No database needed ------------------------------------------------------

def test_receipt_insert_matches_confirmed_receipt_fields():
    """The model and the INSERT agree: every ConfirmedReceipt field is a column, plus image_path."""
    placeholders = set(re.findall(r"%\((\w+)\)s", db._INSERT_RECEIPT))
    model_fields = set(ConfirmedReceipt.model_fields) - {"line_items"}
    assert placeholders == model_fields | {"image_path"}
    assert set(db._receipt_params(ConfirmedReceipt.model_validate(MINIMAL))) == model_fields


def test_line_item_and_audit_inserts_match_their_params():
    receipt = ConfirmedReceipt.model_validate(FULL)
    item_params = db._line_item_params(1, 1, receipt.line_items[0])
    assert set(re.findall(r"%\((\w+)\)s", db._INSERT_LINE_ITEM)) == set(item_params)
    assert type(item_params["quantity"]) is float  # REAL column; quantity is not money
    audit_params = db._audit_params(1, make_audit())
    assert set(re.findall(r"%\((\w+)\)s", db._INSERT_AUDIT)) == set(audit_params)


def test_receipt_params_send_category_as_plain_text():
    params = db._receipt_params(ConfirmedReceipt.model_validate(MINIMAL))
    assert type(params["category"]) is str and params["category"] == "Inventory"


# --- Database tests ----------------------------------------------------------

requires_test_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set: create Neon 'ledger_test', add it to .env, run `make migrate TARGET=test`",
)


@pytest.fixture
def conn():
    if TEST_DATABASE_URL == os.getenv("DATABASE_URL", "").strip():
        pytest.fail("Refusing: TEST_DATABASE_URL equals DATABASE_URL (the real ledger).")
    dbname = conninfo_to_dict(TEST_DATABASE_URL).get("dbname") or ""
    if not dbname.endswith("_test"):
        pytest.fail(f"Refusing: test database name must end in '_test' (got {dbname!r}).")

    with db.get_connection(TEST_DATABASE_URL) as connection:
        truncate = "TRUNCATE receipts, line_items, receipt_audit RESTART IDENTITY CASCADE"
        connection.execute(truncate)
        connection.commit()
        yield connection
        connection.rollback()
        connection.execute(truncate)
        connection.commit()


def count_rows(conn) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # fixed table names
        for table in ("receipts", "line_items", "receipt_audit")
    }


@requires_test_db
def test_save_and_read_back_full_receipt(conn):
    receipt = ConfirmedReceipt.model_validate(FULL)
    receipt_id = db.save_confirmed_receipt(conn, receipt, "data/images/abc.jpg", make_audit())

    saved = db.get_receipt(conn, receipt_id)
    for field in ConfirmedReceipt.model_fields.keys() - {"line_items", "category"}:
        assert saved[field] == getattr(receipt, field), field
    assert saved["category"] == "Inventory"
    assert saved["date_ad"] == date(2026, 9, 30)
    assert saved["image_path"] == "data/images/abc.jpg"
    assert saved["created_at"] is not None

    items = saved["line_items"]
    assert [i["line_no"] for i in items] == [1, 2, 3]
    assert [i["description"] for i in items] == ["Rice 25kg", "Oil", None]
    assert [i["amount_paisa"] for i in items] == [75000, 35619, None]
    assert Decimal(str(items[0]["quantity"])) == Decimal("1.5")

    audit = conn.execute(
        "SELECT model_name, ai_extraction_json, flags_at_extraction_json, edited_fields_json, confirmed_at"
        " FROM receipt_audit WHERE receipt_id = %s", (receipt_id,)
    ).fetchone()
    assert audit[0] == "gemini-3.5-flash-lite"
    assert audit[1] == {"total_raw": "1,250.00"}
    assert audit[2][0]["rule_id"] == "V6"
    assert audit[3] == ["total_paisa"]
    assert audit[4] == NOW


@requires_test_db
def test_minimal_confirmed_receipt_saves_without_extra_fields(conn):
    receipt_id = db.save_confirmed_receipt(
        conn, ConfirmedReceipt.model_validate(MINIMAL), "data/images/min.jpg", make_audit()
    )
    saved = db.get_receipt(conn, receipt_id)
    assert saved["line_items"] == []
    assert saved["subtotal_paisa"] is None and saved["user_override"] is False
    assert count_rows(conn) == {"receipts": 1, "line_items": 0, "receipt_audit": 1}


@requires_test_db
def test_failure_in_audit_insert_rolls_back_everything(conn):
    receipt = ConfirmedReceipt.model_validate(FULL)
    bad_audit = make_audit().model_copy(update={"confirmed_at": None})  # NOT NULL in receipt_audit
    with pytest.raises(psycopg.errors.NotNullViolation):
        db.save_confirmed_receipt(conn, receipt, "data/images/abc.jpg", bad_audit)
    assert count_rows(conn) == {"receipts": 0, "line_items": 0, "receipt_audit": 0}


@requires_test_db
@pytest.mark.parametrize("override", [
    {"total_paisa": 0},
    {"bs_month": 13},
    {"vat_paisa": -1},
    {"merchant_name": "  "},
    {"status": "approved"},
    {"category": types.SimpleNamespace(value="Groceries")},
], ids=str)
def test_db_check_constraints_raise(conn, override):
    # model_copy(update=...) skips Pydantic validation, so this proves the DB itself refuses bad rows.
    receipt = ConfirmedReceipt.model_validate(MINIMAL).model_copy(update=override)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.save_confirmed_receipt(conn, receipt, "data/images/x.jpg", make_audit())
    assert count_rows(conn)["receipts"] == 0


@requires_test_db
def test_get_receipt_missing_returns_none(conn):
    assert db.get_receipt(conn, 999999) is None


@requires_test_db
def test_list_receipts_newest_first_with_limit(conn):
    for day in ("2026-09-01", "2026-09-30", "2026-09-15"):
        db.save_confirmed_receipt(
            conn, ConfirmedReceipt.model_validate({**MINIMAL, "date_ad": day}), "data/images/x.jpg", make_audit()
        )
    rows = db.list_receipts(conn, limit=2)
    assert [r["date_ad"] for r in rows] == [date(2026, 9, 30), date(2026, 9, 15)]
    assert rows[0]["total_paisa"] == 125000
