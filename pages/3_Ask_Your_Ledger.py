"""Ask Your Ledger page: chat-style, single-turn NL queries (TASK-025, PRD FR-8, A§5).

UI only: all logic lives in bahikhata/ask.py. No conversation memory -- each
question is answered on its own, and the chat log below is only a session-local
transcript so the user can see earlier answers; re-asking a question never
reuses a previous answer's SQL or rows, only the LLM response cache (keyed by
question + today, ARCHITECTURE.md §3.11).
"""

import pandas as pd
import streamlit as st

from bahikhata import ask, config
from bahikhata.schemas import QueryResult

st.set_page_config(page_title="Ask Your Ledger · BahiKhata AI", page_icon="💬", layout="wide")
S = st.session_state
S.setdefault("ask_history", [])

st.title("Ask Your Ledger")
st.caption("Ask about your saved receipts. Each question is answered on its own — there's no follow-up memory.")


def render_answer(result: QueryResult) -> None:
    if result.plan is not None and result.plan.date_range_start and result.plan.date_range_end:
        st.caption(f"Assumed date range: {result.plan.date_range_start} to {result.plan.date_range_end}")

    if result.formatted_rows:
        st.dataframe(pd.DataFrame(result.formatted_rows, columns=result.columns), hide_index=True, width="stretch")
        if result.explanation:
            st.write(result.explanation)
    elif result.message:
        st.info(result.message)

    sql_shown = result.sql_executed or (result.plan.sql if result.plan else None)
    if sql_shown:
        with st.expander("SQL" if result.sql_executed else "SQL (not run)"):
            st.code(sql_shown, language="sql")


for past_question, past_result in S.ask_history:
    with st.chat_message("user"):
        st.write(past_question)
    with st.chat_message("assistant"):
        render_answer(past_result)

question = st.chat_input("Ask about your receipts…")
if question is not None:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Thinking…"):
                result = ask.answer_question(question, ask.today())
        except Exception:  # never show a traceback for an unexpected failure
            result = QueryResult(
                question=question,
                message="Something went wrong answering that — try again.",
                prompt_version=config.SQL_PROMPT,
                model_name=config.MODEL_NAME,
            )
        render_answer(result)
    S.ask_history.append((question, result))
