"""TASK-021: the Dashboard page, driven by Streamlit's AppTest.

A fake query layer stands in for the database: db.get_connection returns a
dummy connection (never used for anything beyond the `with` protocol) and the
four db.dashboard_* functions are monkeypatched to return canned rows. No
network, no real database.
"""

from datetime import date
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from bahikhata import db

PAGE = str(Path(__file__).resolve().parent.parent / "pages" / "2_Dashboard.py")


class DummyConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def start(monkeypatch, *, today=date(2026, 10, 3), total_paisa=0, by_category=(), by_month=(),
          receipts=(), connect_error: Exception | None = None) -> AppTest:
    monkeypatch.setattr(db, "today", lambda: today)
    if connect_error is not None:
        def raise_error():
            raise connect_error
        monkeypatch.setattr(db, "get_connection", raise_error)
    else:
        monkeypatch.setattr(db, "get_connection", lambda: DummyConnection())
    monkeypatch.setattr(db, "dashboard_total", lambda conn, start, end: total_paisa)
    monkeypatch.setattr(db, "dashboard_by_category", lambda conn, start, end: list(by_category))
    monkeypatch.setattr(db, "dashboard_by_month", lambda conn, start, end: list(by_month))
    monkeypatch.setattr(
        db, "dashboard_receipts",
        lambda conn, start, end, status=None, limit=500: [
            r for r in receipts if status is None or r["status"] == status
        ],
    )
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    return at


CATEGORY_ROWS = [
    {"category": "Food", "total_paisa": 150000, "receipt_count": 2},
    {"category": "Inventory", "total_paisa": 200000, "receipt_count": 1},
]
MONTH_ROWS = [
    {"month": date(2026, 9, 1), "total_paisa": 100000},
    {"month": date(2026, 10, 1), "total_paisa": 200000},
]
RECEIPT_ROWS = [
    {"id": 2, "date_ad": date(2026, 9, 20), "merchant_name": "Shree Traders", "total_paisa": 200000,
     "category": "Inventory", "status": "clean", "user_override": False},
    {"id": 1, "date_ad": date(2026, 9, 1), "merchant_name": "Himal Store", "total_paisa": 50000,
     "category": "Food", "status": "needs_review", "user_override": False},
]


def test_default_period_is_this_month(monkeypatch):
    at = start(monkeypatch, today=date(2026, 10, 3))
    assert at.selectbox(key="period").value == "This month"
    assert "2026-10-01 to 2026-10-31" in "\n".join(c.value for c in at.caption)
    assert not at.exception


def test_total_metric_shows_formatted_npr(monkeypatch):
    at = start(monkeypatch, total_paisa=350000)
    assert at.metric[0].value == "Rs 3,500.00"


def test_empty_database_shows_friendly_message_no_crash(monkeypatch):
    at = start(monkeypatch, total_paisa=0, by_category=[], by_month=[], receipts=[])
    assert not at.exception
    assert at.metric[0].value == "Rs 0.00"
    infos = [i.value for i in at.info]
    assert infos.count("No receipts in this period.") == 3  # category, month and receipts sections
    assert len(at.dataframe) == 0  # no category table, no receipts table rendered


def test_by_category_table_and_chart(monkeypatch):
    at = start(monkeypatch, by_category=CATEGORY_ROWS)
    assert not at.exception  # includes the st.bar_chart call, which AppTest can't inspect directly
    assert len(at.dataframe) == 1
    table = at.dataframe[0].value
    assert list(table["Category"]) == ["Food", "Inventory"]
    assert list(table["Total"]) == ["Rs 1,500.00", "Rs 2,000.00"]


def test_by_month_separates_boundary_receipts(monkeypatch):
    at = start(monkeypatch, by_month=MONTH_ROWS)
    assert not at.exception  # a receipt on 2026-09-30 and one on 2026-10-01 render as two distinct bars


def test_receipts_table_newest_first_with_formatted_total(monkeypatch):
    at = start(monkeypatch, receipts=RECEIPT_ROWS)
    table = at.dataframe[-1].value
    assert list(table["Date"]) == [date(2026, 9, 20), date(2026, 9, 1)]
    assert list(table["Total"]) == ["Rs 2,000.00", "Rs 500.00"]
    assert list(table["Status"]) == ["clean", "needs_review"]


def test_status_filter_narrows_receipts_table(monkeypatch):
    at = start(monkeypatch, receipts=RECEIPT_ROWS)
    at.selectbox(key="status_filter").select("needs_review").run()
    table = at.dataframe[-1].value
    assert list(table["Status"]) == ["needs_review"]


def test_status_filter_no_match_shows_specific_message(monkeypatch):
    at = start(monkeypatch, receipts=RECEIPT_ROWS)
    at.selectbox(key="status_filter").select("invalid").run()
    infos = [i.value for i in at.info]
    assert "No invalid receipts in this period." in infos


def test_custom_range_start_after_end_shows_error_no_crash(monkeypatch):
    at = start(monkeypatch)
    at.selectbox(key="period").select("Custom range").run()
    at.date_input(key="custom_start").set_value(date(2026, 6, 1)).run()
    at.date_input(key="custom_end").set_value(date(2026, 1, 1)).run()
    assert not at.exception
    assert any("start date must not be after" in e.value for e in at.error)


def test_missing_database_url_shows_one_message_no_traceback(monkeypatch):
    at = start(monkeypatch, connect_error=RuntimeError("DATABASE_URL is not set."))
    assert not at.exception
    assert len(at.error) == 1
    assert "DATABASE_URL" in at.error[0].value


def test_connection_failure_shows_one_message_no_traceback(monkeypatch):
    at = start(monkeypatch, connect_error=psycopg.OperationalError("could not connect to server"))
    assert not at.exception
    assert len(at.error) == 1
    assert "Couldn't reach the database" in at.error[0].value
