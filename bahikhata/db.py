"""Persistence on Neon Postgres (ARCHITECTURE.md §3.9, §7, Amendments A1/A2).

Raw psycopg v3 and hand-written, parameterised SQL; no ORM. The tables are
created by the Alembic migrations in migrations/versions/, not here.

save_confirmed_receipt() is the only write path: receipts + line_items +
receipt_audit in one transaction. There is deliberately no general "execute any
SQL" function on the writable connection.

Connections are opened per operation (Neon is serverless; no global connection):

    with db.get_connection() as conn:
        receipt_id = db.save_confirmed_receipt(conn, receipt, image_path, audit)
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict

from bahikhata import config
from bahikhata.schemas import ConfirmedLineItem, ConfirmedReceipt


class AuditRecord(BaseModel):
    """One receipt_audit row: what the AI said, and what changed before saving (A§7)."""

    model_config = ConfigDict(extra="forbid")

    model_name: str
    prompt_version: str
    image_sha256: str
    raw_response: str | None = None
    ai_extraction_json: dict[str, Any] | None = None   # ReceiptExtraction.model_dump(mode="json")
    ai_draft_json: dict[str, Any] | None = None        # ReceiptDraft.model_dump(mode="json")
    flags_at_extraction_json: list[dict[str, Any]]     # [ValidationFlag.model_dump(), ...]
    flags_at_save_json: list[dict[str, Any]]
    edited_fields_json: list[str]                       # e.g. ["total_paisa", "line_items"]
    extracted_at: AwareDatetime | None = None
    confirmed_at: AwareDatetime


def get_connection(url: str | None = None) -> psycopg.Connection:
    """Open a new connection (to DATABASE_URL unless a URL is given). Use it in a `with` block."""
    return psycopg.connect(url or config.get_database_url())


_INSERT_RECEIPT = """
    INSERT INTO receipts (
        merchant_name, merchant_pan, invoice_number, date_ad, date_bs, bs_year, bs_month,
        subtotal_paisa, discount_paisa, service_charge_paisa, vat_paisa, total_paisa,
        category, status, user_override, image_path
    ) VALUES (
        %(merchant_name)s, %(merchant_pan)s, %(invoice_number)s, %(date_ad)s, %(date_bs)s,
        %(bs_year)s, %(bs_month)s, %(subtotal_paisa)s, %(discount_paisa)s,
        %(service_charge_paisa)s, %(vat_paisa)s, %(total_paisa)s, %(category)s, %(status)s,
        %(user_override)s, %(image_path)s
    )
    RETURNING id
"""

_INSERT_LINE_ITEM = """
    INSERT INTO line_items (receipt_id, line_no, description, quantity, unit_price_paisa, amount_paisa)
    VALUES (%(receipt_id)s, %(line_no)s, %(description)s, %(quantity)s,
            %(unit_price_paisa)s, %(amount_paisa)s)
"""

_INSERT_AUDIT = """
    INSERT INTO receipt_audit (
        receipt_id, model_name, prompt_version, image_sha256, raw_response,
        ai_extraction_json, ai_draft_json, flags_at_extraction_json, flags_at_save_json,
        edited_fields_json, extracted_at, confirmed_at
    ) VALUES (
        %(receipt_id)s, %(model_name)s, %(prompt_version)s, %(image_sha256)s, %(raw_response)s,
        %(ai_extraction_json)s, %(ai_draft_json)s, %(flags_at_extraction_json)s,
        %(flags_at_save_json)s, %(edited_fields_json)s, %(extracted_at)s, %(confirmed_at)s
    )
"""


def _receipt_params(receipt: ConfirmedReceipt) -> dict[str, Any]:
    """Columns for the receipts INSERT, one per ConfirmedReceipt field (minus line_items)."""
    params = receipt.model_dump(exclude={"line_items"})
    # Plain strings, not enum members: psycopg would dump an Enum by its member name.
    params["category"] = receipt.category.value
    return params


def _line_item_params(receipt_id: int, line_no: int, item: ConfirmedLineItem) -> dict[str, Any]:
    params = item.model_dump()
    # line_items.quantity is REAL (not money, A§7), so Decimal -> float is acceptable here.
    params["quantity"] = None if item.quantity is None else float(item.quantity)
    return {"receipt_id": receipt_id, "line_no": line_no, **params}


def _audit_params(receipt_id: int, audit: AuditRecord) -> dict[str, Any]:
    params = dict(audit)
    for column in ("ai_extraction_json", "ai_draft_json", "flags_at_extraction_json",
                   "flags_at_save_json", "edited_fields_json"):
        if params[column] is not None:
            params[column] = Jsonb(params[column])
    return {"receipt_id": receipt_id, **params}


def save_confirmed_receipt(
    conn: psycopg.Connection, receipt: ConfirmedReceipt, image_path: str, audit: AuditRecord
) -> int:
    """Insert the receipt, its line items and its audit row atomically; return the new id.

    Any error rolls back all three inserts, so a receipt is never half-saved.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_INSERT_RECEIPT, {**_receipt_params(receipt), "image_path": image_path})
        receipt_id = cur.fetchone()[0]
        if receipt.line_items:
            cur.executemany(_INSERT_LINE_ITEM, [
                _line_item_params(receipt_id, line_no, item)
                for line_no, item in enumerate(receipt.line_items, start=1)
            ])
        cur.execute(_INSERT_AUDIT, _audit_params(receipt_id, audit))
    return receipt_id


def get_receipt(conn: psycopg.Connection, receipt_id: int) -> dict[str, Any] | None:
    """One receipt row as a dict, with "line_items" (ordered by line_no); None if not found."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM receipts WHERE id = %s", (receipt_id,))
        receipt = cur.fetchone()
        if receipt is None:
            return None
        cur.execute(
            "SELECT line_no, description, quantity, unit_price_paisa, amount_paisa"
            " FROM line_items WHERE receipt_id = %s ORDER BY line_no",
            (receipt_id,),
        )
        receipt["line_items"] = cur.fetchall()
    return receipt


def list_receipts(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent receipts first (by date_ad, then id), without line items."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, date_ad, merchant_name, total_paisa, category, status, user_override"
            " FROM receipts ORDER BY date_ad DESC, id DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()

