"""Rendering for the Dashboard. Every number shown comes from `data`; nothing is computed here."""

import streamlit as st

from pages.dashboard import data
from pages.dashboard.data import DashboardData, Filters


def render_banner() -> None:
    st.html("""
<div class="bk-hero">
  <div class="bk-tag">Receipt-to-ledger assistant</div>
  <h1>BahiKhata AI</h1>
  <p>Receipt-to-ledger assistant for Nepali small businesses. Snap a receipt, review what was read,
  then explore your spending on the dashboard or ask questions in plain language.</p>
</div>
""")


def render_notice(message: str | None) -> None:
    """One-off confirmation left by Capture & Review after a save."""
    if message:
        st.success(message, icon=":material/check_circle:")


def render_filters() -> Filters:
    with st.container(border=True, key="card_filters"):
        col_period, col_status, col_start, col_end = st.columns([2, 2, 1.5, 1.5], vertical_alignment="bottom")
        period_key = data.period_key_for(
            col_period.selectbox("Period", list(data.PERIOD_LABELS.values()), key="period"))

        custom_start = custom_end = None
        if period_key == "custom":
            default_start, default_end = data.default_custom_range()
            custom_start = col_start.date_input("From", default_start, key="custom_start")
            custom_end = col_end.date_input("To", default_end, key="custom_end")

        status_label = col_status.selectbox("Receipt status", data.STATUS_OPTIONS, key="status_filter")
    return Filters(period_key, custom_start, custom_end, None if status_label == "All" else status_label)


def render_error(message: str) -> None:
    st.error(message)


def render_period(dashboard: DashboardData) -> None:
    st.caption(f"Period: {dashboard.start.isoformat()} to {dashboard.end.isoformat()}")


def render_kpis(container, kpis: data.Kpis) -> None:
    kpi_total, kpi_count, kpi_top, kpi_avg = container.columns(4)
    kpi_total.metric("Total spend", kpis.total)
    kpi_count.metric("Receipts", kpis.receipt_count)
    kpi_top.metric("Top category", kpis.top_category)
    kpi_avg.metric("Average receipt", kpis.average)


def render_receipts(receipts: list[dict], status: str | None) -> int | None:
    """The receipts table; returns the id of the selected receipt when "Edit" is clicked."""
    with st.container(border=True, key="card_receipts"):
        col_title, col_edit = st.columns([4, 1], vertical_alignment="bottom")
        col_title.subheader("Receipts")
        if not receipts:
            st.info(data.no_receipts_message(status))
            return None
        selection = st.dataframe(
            data.receipts_table(receipts), hide_index=True, width="stretch",
            on_select="rerun", selection_mode="single-row", key="receipts_table",
        )
        rows = selection.selection.rows
        edit_clicked = col_edit.button(
            "Edit", icon=":material/edit:", key="edit_receipt", width="stretch", disabled=not rows,
            help="Open the selected receipt in Capture & Review" if rows else "Select a row in the table first",
        )
        st.caption("Select a row, then click **Edit** to correct it.")
    return receipts[rows[0]]["id"] if edit_clicked and rows else None
