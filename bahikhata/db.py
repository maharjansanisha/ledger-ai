"""Persistence on Neon Postgres (ARCHITECTURE.md §3.9, §7, Amendments A1/A2).

Raw psycopg v3 and hand-written, parameterised SQL; no ORM. The tables are
created by the Alembic migrations in migrations/versions/, not here.

save_confirmed_receipt() and update_confirmed_receipt() are the only write
paths: receipts + line_items + receipt_audit in one transaction. There is deliberately no general "execute any
SQL" function on the writable connection.

Connections are opened per operation (Neon is serverless; no global connection):

    with db.get_connection() as conn:
        receipt_id = db.save_confirmed_receipt(conn, receipt, image_path, audit)
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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


_UPDATE_RECEIPT = """
    UPDATE receipts SET
        merchant_name = %(merchant_name)s, merchant_pan = %(merchant_pan)s,
        invoice_number = %(invoice_number)s, date_ad = %(date_ad)s, date_bs = %(date_bs)s,
        bs_year = %(bs_year)s, bs_month = %(bs_month)s, subtotal_paisa = %(subtotal_paisa)s,
        discount_paisa = %(discount_paisa)s, service_charge_paisa = %(service_charge_paisa)s,
        vat_paisa = %(vat_paisa)s, total_paisa = %(total_paisa)s, category = %(category)s,
        status = %(status)s, user_override = %(user_override)s
    WHERE id = %(id)s
"""

_UPDATE_AUDIT = """
    UPDATE receipt_audit SET
        flags_at_save_json = %(flags_at_save_json)s, edited_fields_json = %(edited_fields_json)s,
        confirmed_at = %(confirmed_at)s
    WHERE receipt_id = %(receipt_id)s
"""


def update_confirmed_receipt(
    conn: psycopg.Connection, receipt_id: int, receipt: ConfirmedReceipt, *,
    flags_at_save: list[dict[str, Any]], edited_fields: list[str], confirmed_at: datetime,
) -> None:
    """Overwrite a saved receipt with re-reviewed values, atomically.

    The image and the AI's side of the audit row (raw response, AI draft) are kept;
    the line items are replaced, and the audit row's save-time fields are refreshed.
    Raises LookupError if the receipt does not exist (nothing is changed).
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_UPDATE_RECEIPT, {**_receipt_params(receipt), "id": receipt_id})
        if cur.rowcount == 0:
            raise LookupError(f"receipt {receipt_id} not found")
        cur.execute("DELETE FROM line_items WHERE receipt_id = %s", (receipt_id,))
        if receipt.line_items:
            cur.executemany(_INSERT_LINE_ITEM, [
                _line_item_params(receipt_id, line_no, item)
                for line_no, item in enumerate(receipt.line_items, start=1)
            ])
        cur.execute(_UPDATE_AUDIT, {
            "receipt_id": receipt_id, "flags_at_save_json": Jsonb(flags_at_save),
            "edited_fields_json": Jsonb(edited_fields), "confirmed_at": confirmed_at,
        })


def get_ai_draft_json(conn: psycopg.Connection, receipt_id: int) -> dict[str, Any] | None:
    """The AI's normalized draft saved with the receipt (receipt_audit.ai_draft_json), or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT ai_draft_json FROM receipt_audit WHERE receipt_id = %s", (receipt_id,))
        row = cur.fetchone()
    return None if row is None else row[0]


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


# --- Dashboard reads (ARCHITECTURE.md §3.10, Amendment A1; TASK-020) ---------
#
# Fixed, parameterised SQL only -- no LLM, no general "run any SQL" function.
# Aggregation happens in SQL (SUM/GROUP BY), never by looping over rows in
# Python. Uses the owner connection (db.get_connection / DATABASE_URL): the
# ledger_reader read-only role is reserved for "Ask Your Ledger" (.env.example).

def today() -> date:
    """The current date. A thin wrapper so tests can monkeypatch `db.today`."""
    return date.today()


def month_bounds(d: date) -> tuple[date, date]:
    """First and last day of d's calendar month."""
    start = d.replace(day=1)
    end = d.replace(day=calendar.monthrange(d.year, d.month)[1])
    return start, end


def dashboard_period(
    period: str, today: date, custom_start: date | None = None, custom_end: date | None = None
) -> tuple[date, date]:
    """AD date range for the dashboard period filter. Python computes it (never SQL/the LLM).

    period: "this_month", "last_month" or "custom" (requires custom_start/custom_end).
    """
    if period == "this_month":
        return month_bounds(today)
    if period == "last_month":
        last_day_of_prev_month = today.replace(day=1) - timedelta(days=1)
        return month_bounds(last_day_of_prev_month)
    if period == "custom":
        if custom_start is None or custom_end is None:
            raise ValueError("Choose both a start and an end date for a custom range.")
        if custom_start > custom_end:
            raise ValueError("The start date must not be after the end date.")
        return custom_start, custom_end
    raise ValueError(f"Unknown period: {period!r}")


def dashboard_total(conn: psycopg.Connection, start: date, end: date) -> int:
    """Total spend in paisa for receipts dated in [start, end]; 0 if none match."""
    with conn.cursor() as cur:
        # SUM(bigint) is NUMERIC in Postgres (psycopg would hand back a Decimal);
        # cast back to bigint so callers (format_npr, charts) always get a plain int.
        cur.execute(
            "SELECT COALESCE(SUM(total_paisa), 0)::bigint FROM receipts WHERE date_ad BETWEEN %s AND %s",
            (start, end),
        )
        return cur.fetchone()[0]


def dashboard_by_category(conn: psycopg.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """[{category, total_paisa, receipt_count}, ...] for [start, end], highest spend first.

    Only categories with at least one matching receipt are returned.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT category, SUM(total_paisa)::bigint AS total_paisa, COUNT(*) AS receipt_count"
            " FROM receipts WHERE date_ad BETWEEN %s AND %s"
            " GROUP BY category ORDER BY total_paisa DESC",
            (start, end),
        )
        return cur.fetchall()


def dashboard_by_month(conn: psycopg.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """[{month, total_paisa}, ...] for [start, end], ordered oldest to newest.

    `month` is the first day of each calendar month (AD); a receipt on the last
    day of a month and one on the first of the next land in different rows.
    Only months with at least one matching receipt are returned.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT date_trunc('month', date_ad)::date AS month, SUM(total_paisa)::bigint AS total_paisa"
            " FROM receipts WHERE date_ad BETWEEN %s AND %s"
            " GROUP BY month ORDER BY month",
            (start, end),
        )
        return cur.fetchall()


def dashboard_receipts(
    conn: psycopg.Connection, start: date, end: date, status: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    """Receipts dated in [start, end] (optionally filtered by status), newest first."""
    with conn.cursor(row_factory=dict_row) as cur:
        if status is None:
            cur.execute(
                "SELECT id, date_ad, merchant_name, total_paisa, category, status, user_override"
                " FROM receipts WHERE date_ad BETWEEN %s AND %s"
                " ORDER BY date_ad DESC, id DESC LIMIT %s",
                (start, end, limit),
            )
        else:
            cur.execute(
                "SELECT id, date_ad, merchant_name, total_paisa, category, status, user_override"
                " FROM receipts WHERE date_ad BETWEEN %s AND %s AND status = %s"
                " ORDER BY date_ad DESC, id DESC LIMIT %s",
                (start, end, status, limit),
            )
        return cur.fetchall()


# --- Read-only executor (ARCHITECTURE.md §3.13, §6; Amendment A1; TASK-022) --
#
# Runs one already-guarded SELECT. Defence in depth even if sql_guard.check_sql
# is bypassed: the transaction is forced read-only and time-capped before the
# query runs (so a write fails even under the owner role, as Postgres refuses to
# switch a transaction to read-write once it has run a query), and the default target (DATABASE_URL_READONLY) connects
# as `ledger_reader`, which has no grant on receipt_audit or any write grant at
# all -- two independent locks. Never raises a raw psycopg error to the caller.

@dataclass(frozen=True)
class QueryExecutionResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    error: str | None = None
    # True when the connection itself failed (bad host/credentials, network) -- the
    # SQL never reached the database, so rewriting it can't help.
    connection_failed: bool = False


def _safe_execution_error(exc: psycopg.Error) -> str:
    """A short, user-safe message -- never the raw driver text (may name the host/role)."""
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return "The query took too long and was stopped."
    if isinstance(exc, psycopg.errors.ReadOnlySqlTransaction):
        return "That statement would modify the database, which isn't allowed here."
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return "That query reaches data this connection isn't allowed to read."
    return "The database could not run that query."


def run_readonly_query(sql: str, url: str | None = None) -> QueryExecutionResult:
    """Run one guarded SELECT (sql_guard.check_sql must have approved it first).

    `url` defaults to DATABASE_URL_READONLY (the `ledger_reader` role); tests
    pass TEST_DATABASE_URL to exercise the read-only transaction and timeout
    without needing a reader role on the test database.
    """
    target = url if url is not None else config.get_database_url_readonly()
    try:
        conn = psycopg.connect(target)
    except psycopg.Error:
        return QueryExecutionResult(
            ok=False, error="Couldn't connect to the database.", connection_failed=True,
        )
    try:
        with conn, conn.cursor() as cur:
            # Set per transaction (SET LOCAL / SET TRANSACTION), not as connection startup
            # options: Neon's pooler rejects startup options, and a session-level SET
            # could leak to other clients sharing a pooled server connection. Both run
            # before the query, inside the same transaction, which then commits/rolls back.
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {int(config.READONLY_STATEMENT_TIMEOUT_MS)}")
            cur.execute(sql)
            if cur.description is None:
                return QueryExecutionResult(ok=True)
            return QueryExecutionResult(
                ok=True,
                columns=[d.name for d in cur.description],
                rows=cur.fetchall(),
            )
    except psycopg.Error as exc:
        return QueryExecutionResult(ok=False, error=_safe_execution_error(exc))

