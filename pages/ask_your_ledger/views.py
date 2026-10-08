"""Rendering for Ask Your Ledger: header, tips, documents sidebar, chat turns and answers.

Proposed edits render as confirmation cards; render_answer/render_turn return the
(proposal_id, approve) a card's button was clicked with, and the page acts on it.
"""

import re

import pandas as pd
import streamlit as st

from bahikhata import edit, rag
from bahikhata.schemas import EditProposal, QueryResult

EditDecision = tuple[str, bool]  # (proposal_id, True = Approve / False = Cancel)

UPLOAD_FILE_TYPES = list(rag.SUPPORTED_EXTENSIONS)


def render_header(show_tips: bool, rag_enabled: bool) -> None:
    st.title("Ask Your Ledger")
    st.caption(
        "Ask about your saved receipts or the documents you attach. "
        "Each question is answered on its own — there's no follow-up memory. "
        "You can also ask to correct a saved line item; nothing changes until you approve it."
    )
    if not rag_enabled:
        st.caption(":material/info: Document search isn't configured, so attachments won't be searchable.")
    if show_tips:
        with st.container(border=True, key="card_tips"):
            st.markdown(
                ":material/lightbulb: **Try asking** — “How much did I spend this month?” · "
                "“Top 5 merchants by spend last month” · attach a budget or policy and ask about it · "
                "“Change the quantity of rice on receipt #12 to 5” (you approve before anything changes)"
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


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>$:])")


def _md(text: str) -> str:
    """Saved text (merchant, item names) shown literally inside markdown."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def render_edit_proposal(proposal: EditProposal, outcome: edit.EditOutcome | None) -> EditDecision | None:
    """One pending change as a card with Approve / Cancel, or its outcome once decided."""
    pid = proposal.proposal_id
    with st.container(border=True, key=f"edit_{pid}"):
        st.markdown(":material/edit_note: **Confirm change**" if outcome is None else ":material/edit_note: **Change**")
        bill = [f"Receipt #{proposal.receipt_id}", _md(proposal.merchant_name)]
        if proposal.invoice_number:
            bill.append(_md(proposal.invoice_number))
        bill.append(proposal.date_ad.isoformat())
        st.markdown(" · ".join(bill))
        item = f" — {_md(proposal.item)}" if proposal.item else ""
        st.markdown(f"Items table, line {proposal.line_no}{item}")
        st.markdown(f"**{edit.FIELD_LABELS[proposal.field]}:** {_md(proposal.current_text)} → "
                    f"**{_md(proposal.new_text)}**")
        if outcome is not None:
            (st.success if outcome.applied else st.info)(outcome.message)
            return None
        st.caption("Only this one value changes. Nothing is saved until you approve.")
        approve_col, cancel_col = st.columns(2)
        if approve_col.button("Approve change", key=f"approve_{pid}", type="primary", icon=":material/check:",
                              width="stretch"):
            return pid, True
        if cancel_col.button("Cancel", key=f"cancel_{pid}", icon=":material/close:", width="stretch"):
            return pid, False
    return None


def render_answer(result: QueryResult, edit_outcomes: dict[str, edit.EditOutcome] | None = None) -> EditDecision | None:
    decision = None
    for proposal in result.edits:
        decision = render_edit_proposal(proposal, (edit_outcomes or {}).get(proposal.proposal_id)) or decision

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
    return decision


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


def render_turn(
    question: str, result: QueryResult, edit_outcomes: dict[str, edit.EditOutcome] | None = None,
) -> EditDecision | None:
    render_question(question)
    with st.chat_message("assistant"):
        return render_answer(result, edit_outcomes)


def render_chat_input():
    """Returns the submitted ChatInputValue (.text, .files), or None. Attachments are capped at 10 MB."""
    return st.chat_input(
        "Ask about your receipts or documents…", accept_file="multiple", max_upload_size=10,
        file_type=UPLOAD_FILE_TYPES,
    )
