"""Ask Your Ledger page: chat-style, single-turn NL queries (TASK-025, PRD FR-8, A§5).

Entry point (registered in app.py). Page flow only: answering and the session
transcript live in state.py, rendering in views.py, query logic in bahikhata/ask.py.
"""

import streamlit as st

from pages.ask_your_ledger import state, views

st.set_page_config(page_title="Ask Your Ledger · BahiKhata AI", page_icon="💬", layout="wide")

views.render_header(show_tips=not state.history())
for past_question, past_result in state.history():
    views.render_turn(past_question, past_result)

submitted = views.render_chat_input()
if submitted is not None:
    file_names = state.add_uploads(submitted.files)
    question = submitted.text.strip()
    views.render_question(question, file_names)
    if not question:
        st.stop()
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            result = state.answer(question)
        views.render_answer(result)
    state.record(question, result)
