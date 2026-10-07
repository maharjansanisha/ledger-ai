"""TASK-021: the Dashboard page, driven by Streamlit's AppTest.

A fake query layer stands in for the database: db.get_connection returns a
dummy connection (never used for anything beyond the `with` protocol) and the
db.dashboard_* functions are monkeypatched to return canned rows. No
network, no real database.
"""

from datetime import date
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from bahikhata import db

PAGE = str(Path(__file__).resolve().parent.parent / "pages" / "dashboard" / "page.py")


class DummyConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def start(monkeypatch, *, today=date(2026, 10, 3), total_paisa=0, by_category=(),
          receipts=(), connect_error: Exception | None = None, notice: str | None = None) -> AppTest:
    monkeypatch.setattr(db, "today", lambda: today)
    if connect_error is not None:
        def raise_error():
            raise connect_error
        monkeypatch.setattr(db, "get_connection", raise_error)
    else:
        monkeypatch.setattr(db, "get_connection", lambda: DummyConnection())
    monkeypatch.setattr(db, "dashboard_total", lambda conn, start, end: total_paisa)
    monkeypatch.setattr(db, "dashboard_by_category", lambda conn, start, end: list(by_category))
    monkeypatch.setattr(
        db, "dashboard_receipts",
        lambda conn, start, end, status=None, limit=500: [
            r for r in receipts if status is None or r["status"] == status
        ],
    )
    at = AppTest.from_file(PAGE, default_timeout=30)
    if notice is not None:
        at.session_state["ledger_notice"] = notice
    at.run()
    return at


CATEGORY_ROWS = [
    {"category": "Food", "total_paisa": 150000, "receipt_count": 2},
    {"category": "Inventory", "total_paisa": 200000, "receipt_count": 1},
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
    at = start(monkeypatch, total_paisa=0, by_category=[], receipts=[])
    assert not at.exception
    assert at.metric[0].value == "Rs 0.00"
    assert [i.value for i in at.info] == ["No receipts in this period."]
    assert len(at.dataframe) == 0


def test_banner_kpis_filters_table_in_order(monkeypatch):
    at = start(monkeypatch, by_category=CATEGORY_ROWS, receipts=RECEIPT_ROWS)
    keys = [getattr(e, "key", None) or getattr(e, "type", None) for e in elements_in_order(at)]
    order = [keys.index(k) for k in ("html", "metric", "period", "receipts_table")]
    assert order == sorted(order)
    assert len(at.dataframe) == 1  # no category table any more, only the receipts


def test_kpis_from_categories(monkeypatch):
    at = start(monkeypatch, total_paisa=350000, by_category=CATEGORY_ROWS)
    assert [m.value for m in at.metric] == ["Rs 3,500.00", "3", "Food", "Rs 1,166.67"]


def test_receipts_table_newest_first_with_formatted_total(monkeypatch):
    at = start(monkeypatch, receipts=RECEIPT_ROWS)
    table = at.dataframe[-1].value
    assert list(table["#"]) == [2, 1]
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


def test_edit_is_disabled_until_a_row_is_selected(monkeypatch):
    at = start(monkeypatch, receipts=RECEIPT_ROWS)
    assert at.button(key="edit_receipt").disabled


def test_notice_from_a_save_is_shown_once(monkeypatch):
    at = start(monkeypatch, notice="Updated receipt #2.")
    assert at.success[0].value == "Updated receipt #2."
    at.run()
    assert len(at.success) == 0


def elements_in_order(at: AppTest) -> list:
    def walk(node):
        yield node
        for child in getattr(node, "children", {}).values():
            yield from walk(child)
    return list(walk(at.main))
