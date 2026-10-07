"""Dashboard page: KPI tiles, filters, and the receipts table with Edit
(TASK-020/021, ARCHITECTURE.md §3.10, PRD.md FR-7). The app's landing page.

Entry point (registered in app.py). Page flow only: queries and table shaping
live in data.py, rendering in views.py, SQL in bahikhata/db.py.
Edit hands the selected receipt to Capture & Review as ?edit=<id>.
"""

import streamlit as st

from pages.capture_and_review.state import NOTICE_KEY
from pages.dashboard import data, views

st.set_page_config(page_title="Dashboard · BahiKhata AI", page_icon="📊", layout="wide")

views.render_notice(st.session_state.pop(NOTICE_KEY, None))
views.render_banner()
kpi_slot = st.container()  # the tiles sit above the filters but depend on them, so they fill in later
filters = views.render_filters()

try:
    dashboard = data.load(filters)
except data.DashboardError as exc:
    views.render_error(str(exc))
    st.stop()

views.render_period(dashboard)
views.render_kpis(kpi_slot, data.kpis(dashboard))

receipt_id = views.render_receipts(dashboard.receipts, filters.status)
if receipt_id is not None:
    st.switch_page("pages/capture_and_review/page.py", query_params={"edit": receipt_id})
