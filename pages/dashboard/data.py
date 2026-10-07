"""Dashboard data: resolve the period, run the fixed queries, shape rows for display.

No rendering here. All aggregation is fixed, parameterised SQL in bahikhata/db.py;
this module only calls it and turns the rows into the frames and numbers the
views show. No LLM call.

Uses the owner connection (DATABASE_URL) via bahikhata.db.get_connection(),
the same connection the save flow uses. The ledger_reader read-only role is
reserved for "Ask Your Ledger" (Amendment A1, .env.example), not this page.
"""

from dataclasses import dataclass
from datetime import date

import pandas as pd
import psycopg

from bahikhata import db
from bahikhata.normalize import format_npr

PERIOD_LABELS = {"this_month": "This month", "last_month": "Last month", "custom": "Custom range"}
STATUS_OPTIONS = ("All", "clean", "needs_review", "invalid")


class DashboardError(Exception):
    """A problem to show the user as-is (its text is safe to display)."""


@dataclass
class Filters:
    period_key: str
    custom_start: date | None
    custom_end: date | None
    status: str | None  # None means all statuses


@dataclass
class DashboardData:
    start: date
    end: date
    total_paisa: int
    by_category: list[dict]
    receipts: list[dict]


@dataclass
class Kpis:
    total: str
    receipt_count: str
    top_category: str
    average: str


def period_key_for(label: str) -> str:
    return next(key for key, text in PERIOD_LABELS.items() if text == label)


def default_custom_range() -> tuple[date, date]:
    return db.month_bounds(db.today())


def load(filters: Filters) -> DashboardData:
    """Resolve the period and run every dashboard query; raises DashboardError with a user message."""
    try:
        start, end = db.dashboard_period(filters.period_key, db.today(), filters.custom_start, filters.custom_end)
    except ValueError as exc:
        raise DashboardError(str(exc)) from exc
    try:
        with db.get_connection() as conn:
            return DashboardData(
                start=start, end=end,
                total_paisa=db.dashboard_total(conn, start, end),
                by_category=db.dashboard_by_category(conn, start, end),
                receipts=db.dashboard_receipts(conn, start, end, status=filters.status),
            )
    except RuntimeError as exc:
        raise DashboardError("The database is not configured: set DATABASE_URL in .env, then try again.") from exc
    except psycopg.Error as exc:
        raise DashboardError("Couldn't reach the database right now. Check your connection and try again.") from exc


def kpis(data: DashboardData) -> Kpis:
    """Receipt count and top category come from the by-category rows (one query, consistent numbers)."""
    count = sum(row["receipt_count"] for row in data.by_category)
    return Kpis(
        total=format_npr(data.total_paisa),
        receipt_count=f"{count:,}",
        top_category=data.by_category[0]["category"] if data.by_category else "—",
        average=format_npr(round(data.total_paisa / count)) if count else "—",
    )


def receipts_table(receipts: list[dict]) -> pd.DataFrame:
    receipts_df = pd.DataFrame(receipts)
    return pd.DataFrame({
        "#": receipts_df["id"],
        "Date": receipts_df["date_ad"],
        "Merchant": receipts_df["merchant_name"],
        "Total": receipts_df["total_paisa"].map(format_npr),
        "Category": receipts_df["category"],
        "Status": receipts_df["status"],
    })


def no_receipts_message(status: str | None) -> str:
    if status is None:
        return "No receipts in this period."
    return f"No {status.replace('_', ' ')} receipts in this period."
