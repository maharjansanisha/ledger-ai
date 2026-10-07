"""Rendering for Ask Your Ledger: header, tips, documents sidebar, chat turns and answers."""

import pandas as pd
import streamlit as st

from bahikhata import rag
from bahikhata.schemas import QueryResult

UPLOAD_FILE_TYPES = list(rag.SUPPORTED_EXTENSIONS)


def render_header(show_tips: bool, rag_enabled: bool) -> None:
    st.title("Ask Your Ledger")
    st.caption(
        "Ask about your saved receipts or the documents you attach. "
        "Each question is answered on its own — there's no follow-up memory."
    )
    if not rag_enabled:
        st.caption(":material/info: Document search isn't configured, so attachments won't be searchable.")
    if show_tips:
        with st.container(border=True, key="card_tips"):
            st.markdown(
                ":material/lightbulb: **Try asking** — “How much did I spend this month?” · "
                "“Top 5 merchants by spend last month” · attach a budget or policy and ask about it"
            )


def render_documents_sidebar(documents: list[dict]) -> str | None:
    """Indexed documents with a delete button each. Returns the doc_id whose delete was clicked."""
    clicked = None
    with st.sidebar:
        st.subheader("Indexed documents")
        if not documents:
            st.caption("None yet. Attach a file in the chat to make it searchable.")
        for doc in documents:
            name_col, button_col = st.columns([5, 1], vertical_alignment="center")
            name_col.caption(f"{doc['name']} · {doc['chunks']} chunks")
            if button_col.button(":material/delete:", key=f"delete_{doc['doc_id']}", help=f"Remove {doc['name']}"):
                clicked = doc["doc_id"]
    return clicked


def render_answer(result: QueryResult) -> None:
    if result.plan is not None and result.plan.date_range_start and result.plan.date_range_end:
        st.caption(f"Assumed date range: {result.plan.date_range_start} to {result.plan.date_range_end}")

    if result.answer:
        st.write(result.answer)
    if result.formatted_rows:
        if result.explanation and not result.answer:
            st.write(result.explanation)
        st.dataframe(pd.DataFrame(result.formatted_rows, columns=result.columns), hide_index=True, width="stretch")
        if result.message:
            st.caption(f":material/info: {result.message}")
    elif result.message:
        st.info(result.message)

    if result.sources:
        with st.expander(f"Sources ({len(result.sources)})"):
            for source in result.sources:
                st.markdown(f"**{source.doc_name}** · relevance {source.score:.2f}")
                st.caption(source.snippet)

    sql_shown = result.sql_executed or (result.plan.sql if result.plan else None)
    if sql_shown:
        with st.expander("SQL" if result.sql_executed else "SQL (not run)"):
            st.code(sql_shown, language="sql")


def _upload_line(upload: rag.IngestResult) -> str:
    if upload.status == "indexed":
        return f":material/attach_file: {upload.name} — indexed ({upload.chunks} chunks)"
    if upload.status == "duplicate":
        return f":material/attach_file: {upload.name} — already indexed"
    return f":material/error: {upload.name} — {upload.message}"


def render_question(question: str, uploads: list[rag.IngestResult] | None = None) -> None:
    with st.chat_message("user"):
        if question:
            st.write(question)
        for upload in uploads or []:
            st.caption(_upload_line(upload))


def render_turn(question: str, result: QueryResult) -> None:
    render_question(question)
    with st.chat_message("assistant"):
        render_answer(result)


def render_chat_input():
    """Returns the submitted ChatInputValue (.text, .files), or None. Attachments are capped at 10 MB."""
    return st.chat_input(
        "Ask about your receipts or documents…", accept_file="multiple", max_upload_size=10,
        file_type=UPLOAD_FILE_TYPES,
    )
