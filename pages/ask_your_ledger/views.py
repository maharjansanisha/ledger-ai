"""Rendering for Ask Your Ledger: header, tips, chat turns and answers."""

import pandas as pd
import streamlit as st

from bahikhata.schemas import QueryResult

UPLOAD_FILE_TYPES = ["pdf", "txt", "md", "csv", "docx"]


def render_header(show_tips: bool) -> None:
    st.title("Ask Your Ledger")
    st.caption("Ask about your saved receipts. Each question is answered on its own — there's no follow-up memory.")
    if show_tips:
        with st.container(border=True, key="card_tips"):
            st.markdown(":material/lightbulb: **Try asking** — “How much did I spend this month?” · “Top 5 merchants by spend last month”")


def render_answer(result: QueryResult) -> None:
    if result.plan is not None and result.plan.date_range_start and result.plan.date_range_end:
        st.caption(f"Assumed date range: {result.plan.date_range_start} to {result.plan.date_range_end}")

    if result.formatted_rows:
        if result.explanation:
            st.write(result.explanation)
        st.dataframe(pd.DataFrame(result.formatted_rows, columns=result.columns), hide_index=True, width="stretch")
    elif result.message:
        st.info(result.message)

    sql_shown = result.sql_executed or (result.plan.sql if result.plan else None)
    if sql_shown:
        with st.expander("SQL" if result.sql_executed else "SQL (not run)"):
            st.code(sql_shown, language="sql")


def render_question(question: str, file_names: list[str] | None = None) -> None:
    with st.chat_message("user"):
        if question:
            st.write(question)
        for name in file_names or []:
            st.caption(f":material/attach_file: {name}")


def render_turn(question: str, result: QueryResult) -> None:
    render_question(question)
    with st.chat_message("assistant"):
        render_answer(result)


def render_chat_input():
    """Returns the submitted ChatInputValue (.text, .files), or None. Attachments are capped at 10 MB."""
    return st.chat_input(
        "Ask about your receipts…", accept_file=True, max_upload_size=10,
        file_type=UPLOAD_FILE_TYPES,
    )
