"""Ask Your Ledger page: chat-style, single-turn NL queries (TASK-025, PRD FR-8, A§5).

Entry point (registered in app.py). Page flow only: answering, document indexing
and the session transcript live in state.py, rendering in views.py, query logic
in bahikhata/ask.py, document search in bahikhata/rag.py and line-item edit
proposals in bahikhata/edit.py (applied only from a proposal's Approve button).
"""

import streamlit as st

from bahikhata import config
from pages.ask_your_ledger import state, views

st.set_page_config(page_title="Ask Your Ledger · BahiKhata AI", page_icon="💬", layout="wide")

rag_enabled = config.has_rag_config()
views.render_header(show_tips=not state.history(), rag_enabled=rag_enabled)
decision = None
for past_question, past_result in state.history():
    decision = views.render_turn(past_question, past_result, state.edit_outcomes()) or decision
if decision is not None:  # an Approve / Cancel click on a proposed edit
    proposal_id, approve = decision
    with st.spinner("Updating…" if approve else "Cancelling…"):
        state.decide(proposal_id, approve)
    st.rerun()

submitted = views.render_chat_input()
if submitted is not None:
    question = submitted.text.strip()
    uploads = []
    if submitted.files:
        with st.spinner("Indexing attachments…"):
            uploads = state.add_uploads(submitted.files)
    views.render_question(question, uploads)
    if question:
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                result = state.answer(question)
            views.render_answer(result, state.edit_outcomes())
        state.record(question, result)

# Rendered last so it already lists anything attached in this run.
if rag_enabled:
    deleted = views.render_documents_sidebar(state.documents())
    if deleted is not None:
        error = state.delete_document(deleted)
        if error:
            st.sidebar.error(error)
        else:
            st.rerun()
