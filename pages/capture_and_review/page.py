"""Capture & Review page: upload -> extract -> review -> save (TASK-016/017/018, A§3.1, A§11).

Entry point (registered in app.py). Page flow only: rendering lives in views.py,
session-state transitions in state.py, domain logic in bahikhata/.
Each step renders, then acts on what the user clicked and reruns or stops.

Opened as ?edit=<id> (the Dashboard's Edit button), it skips upload and extract and
reviews the saved receipt instead. Every successful save returns to the Dashboard.
"""

import streamlit as st

from pages.capture_and_review import state, views

DASHBOARD = "pages/dashboard/page.py"

st.set_page_config(page_title="Capture & Review · BahiKhata AI", page_icon="🧾", layout="wide")
S = st.session_state

# --- Upload, or load the receipt being edited ---------------------------------
edit_id = state.requested_edit_id()
if edit_id is not None:
    if S.get("edit_id") != edit_id:
        state.start_edit(edit_id)
    if views.render_edit_header(edit_id):
        state.reset()
        st.switch_page(DASHBOARD)
else:
    if S.get("edit_id") is not None:  # left edit mode through the menu
        state.reset()
    views.render_header()
    state.handle_upload(views.render_uploader())

if S.get("intake_error"):
    views.render_intake_error(S.intake_error)
    st.stop()
if "phase" not in S and "processed" not in S:
    views.render_empty_state()
    st.stop()

left_col, right_col = st.columns([2, 3])
views.render_image(left_col, state.image_bytes())

with right_col.container(border=True, key="card_review"):
    # --- Extract --------------------------------------------------------------
    phase = S.get("phase")
    if phase is None:
        if views.render_extract_button():
            with st.spinner("Reading the receipt…"):
                state.run_extraction()
            st.rerun()
        st.stop()

    if phase == "failed":
        try_again, by_hand = views.render_failed(S.fail_message)
        if try_again:
            with st.spinner("Reading the receipt…"):
                state.run_extraction()
            st.rerun()
        if by_hand:
            state.enter_by_hand()
            st.rerun()
        st.stop()

    # --- Review ---------------------------------------------------------------
    submission = views.render_review_form(S.current, S.header_base, S.rows_base, S.version, S.get("save_error"))
    if submission is not None:
        state.submit_review(submission.header, submission.rows, submission.save, submission.override)
        # --- Saved: back to the Dashboard -------------------------------------
        if S.get("saved_id") is not None:
            state.finish_save()
            st.switch_page(DASHBOARD)
        st.rerun()
