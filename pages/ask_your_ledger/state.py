"""Answering and the session transcript for Ask Your Ledger. No rendering here.

All query logic lives in bahikhata/ask.py, and document indexing in
bahikhata/rag.py (attached files are indexed in Pinecone and persist across
sessions; the session keeps only their names). No conversation memory -- each
question is answered on its own, and the history kept here is only a
session-local transcript so the user can see earlier answers; re-asking a
question never reuses a previous answer's SQL or rows, only the LLM response
cache (keyed by question + today, ARCHITECTURE.md §3.11).

answer_question() already turns every failure case it recognises into a
QueryResult.message rather than raising. The try/except in answer() is only for
whatever still escapes it (e.g. a missing DATABASE_URL_READONLY) -- it logs the
exception type and a redacted message to the terminal and returns a message that
differs by kind (config / database / LLM / query-building), never the
exception's own text, which could name a host or a query.

Edit requests ("change the quantity of rice on receipt #12 to 5") go to
bahikhata/edit.py first: answering one only *proposes* the change. The bill is
changed by decide(..., approve=True) alone, which the page calls only from that
proposal's Approve button. Proposals are bound to this session's id, which is
created here, kept server-side in session state, and never taken from the browser.
"""

import logging
import uuid

import streamlit as st

from bahikhata import ask, config, edit, rag
from bahikhata.schemas import QueryResult

logger = logging.getLogger(__name__)

S = st.session_state


def history() -> list[tuple[str, QueryResult]]:
    return S.setdefault("ask_history", [])


def record(question: str, result: QueryResult) -> None:
    history().append((question, result))


def session_id() -> str:
    """This chat session's id; proposals made here can only be decided here."""
    return S.setdefault("ask_session_id", uuid.uuid4().hex)


def edit_outcomes() -> dict[str, edit.EditOutcome]:
    """proposal_id -> what happened when the user clicked Approve or Cancel on it."""
    return S.setdefault("ask_edit_outcomes", {})


def decide(proposal_id: str, approve: bool) -> None:
    """Approve (apply) or cancel one proposal. edit.py re-checks everything server-side,
    so a repeated click can't apply it twice."""
    if approve:
        outcome = edit.apply_edit(proposal_id, session_id())
    else:
        outcome = edit.cancel_edit(proposal_id, session_id())
    edit_outcomes()[proposal_id] = outcome


def add_uploads(files) -> list[rag.IngestResult]:
    """Index each attached file in Pinecone. Never raises: a failure is a "failed" result."""
    results = []
    for f in files:
        if not config.has_rag_config():
            results.append(rag.IngestResult(f.name, "failed", message="Document search isn't configured."))
            continue
        try:
            results.append(rag.ingest(f.name, f.getvalue()))
        except Exception as exc:  # anything rag.ingest didn't recognise: log it redacted, never show it
            logger.error("Ask Your Ledger upload failed: %s: %s", type(exc).__name__, ask.redact_secrets(str(exc)))
            results.append(rag.IngestResult(f.name, "failed", message="I couldn't index that file."))
    return results


def documents() -> list[dict]:
    return rag.list_documents() if config.has_rag_config() else []


def delete_document(doc_id: str) -> str | None:
    """Remove one indexed document. Returns a user-safe error message, or None on success."""
    try:
        rag.delete_document(doc_id)
        return None
    except rag.RagError as exc:
        return exc.user_message


def answer(question: str) -> QueryResult:
    try:
        if edit.looks_like_edit(question):
            proposed = edit.propose_edits(question, session_id())
            if proposed is not None:
                return proposed
        return ask.answer_question(question, ask.today(), strategy="hybrid")
    except Exception as exc:  # never show a traceback for an unexpected failure
        kind, user_message = ask.classify_error(exc)
        logger.error(
            "Ask Your Ledger failed [%s]: %s: %s",
            kind, type(exc).__name__, ask.redact_secrets(str(exc)),
        )
        return QueryResult(
            question=question,
            message=user_message,
            prompt_version=config.SQL_PROMPT,
            model_name=config.MODEL_NAME,
        )
