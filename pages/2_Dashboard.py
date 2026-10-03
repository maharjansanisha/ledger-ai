"""Dashboard page: total spend, spend by category, spend by month, and the
receipts table (TASK-020/021, ARCHITECTURE.md §3.10, PRD.md FR-7).

UI only: all aggregation is fixed, parameterised SQL in bahikhata/db.py, and
every number shown here traces to one of those functions. No LLM call.

Uses the owner connection (DATABASE_URL) via bahikhata.db.get_connection(),
the same connection the save flow uses. The ledger_reader read-only role is
reserved for "Ask Your Ledger" (Amendment A1, .env.example), not this page.
"""

import pandas as pd
import psycopg
import streamlit as st

from bahikhata import db
from bahikhata.normalize import format_npr

st.set_page_config(page_title="Dashboard · BahiKhata AI", page_icon="📊", layout="wide")

PERIOD_LABELS = {"this_month": "This month", "last_month": "Last month", "custom": "Custom range"}
STATUS_OPTIONS = ("All", "clean", "needs_review", "invalid")

st.title("Dashboard")

period_label = st.selectbox("Period", list(PERIOD_LABELS.values()), key="period")
period_key = next(key for key, label in PERIOD_LABELS.items() if label == period_label)

custom_start = custom_end = None
if period_key == "custom":
    default_start, default_end = db.month_bounds(db.today())
    col_start, col_end = st.columns(2)
    custom_start = col_start.date_input("From", default_start, key="custom_start")
    custom_end = col_end.date_input("To", default_end, key="custom_end")

try:
    start, end = db.dashboard_period(period_key, db.today(), custom_start, custom_end)
except ValueError as exc:
    st.error(str(exc))
    st.stop()

status_label = st.selectbox("Receipt status", STATUS_OPTIONS, key="status_filter")
status_filter = None if status_label == "All" else status_label

try:
    with db.get_connection() as conn:
        total_paisa = db.dashboard_total(conn, start, end)
        by_category = db.dashboard_by_category(conn, start, end)
        by_month = db.dashboard_by_month(conn, start, end)
        receipts = db.dashboard_receipts(conn, start, end, status=status_filter)
except RuntimeError:
    st.error("The database is not configured: set DATABASE_URL in .env, then try again.")
    st.stop()
except psycopg.Error:
    st.error("Couldn't reach the database right now. Check your connection and try again.")
    st.stop()

st.caption(f"Period: {start.isoformat()} to {end.isoformat()}")
st.metric("Total spend", format_npr(total_paisa))

st.subheader("Spend by category")
if by_category:
    category_df = pd.DataFrame(by_category)
    chart_df = (category_df.set_index("category")[["total_paisa"]] / 100).rename(
        columns={"total_paisa": "NPR"})
    st.bar_chart(chart_df)
    table_df = pd.DataFrame({
        "Category": category_df["category"],
        "Total": category_df["total_paisa"].map(format_npr),
        "Receipts": category_df["receipt_count"],
    })
    st.dataframe(table_df, hide_index=True, width="stretch")
else:
    st.info("No receipts in this period.")

st.subheader("Spend by month")
if by_month:
    month_df = pd.DataFrame(by_month)
    month_df["label"] = month_df["month"].map(lambda d: d.strftime("%Y-%m"))
    chart_df = (month_df.set_index("label")[["total_paisa"]] / 100).rename(
        columns={"total_paisa": "NPR"})
    st.bar_chart(chart_df)
else:
    st.info("No receipts in this period.")

st.subheader("Receipts")
if receipts:
    receipts_df = pd.DataFrame(receipts)
    table_df = pd.DataFrame({
        "Date": receipts_df["date_ad"],
        "Merchant": receipts_df["merchant_name"],
        "Total": receipts_df["total_paisa"].map(format_npr),
        "Category": receipts_df["category"],
        "Status": receipts_df["status"],
    })
    st.dataframe(table_df, hide_index=True, width="stretch")
else:
    label = "No receipts in this period." if status_filter is None else (
        f"No {status_filter.replace('_', ' ')} receipts in this period.")
    st.info(label)
