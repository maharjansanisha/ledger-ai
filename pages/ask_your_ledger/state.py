"""Answering and the session transcript for Ask Your Ledger. No rendering here.

All query logic lives in bahikhata/ask.py. No conversation memory -- each
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
"""

import logging

import streamlit as st

from bahikhata import ask, config
from bahikhata.schemas import QueryResult

logger = logging.getLogger(__name__)

S = st.session_state


def history() -> list[tuple[str, QueryResult]]:
    return S.setdefault("ask_history", [])


def record(question: str, result: QueryResult) -> None:
    history().append((question, result))


def uploads() -> dict[str, bytes]:
    """Files attached in the chat input this session, by name. Kept for RAG; not used for answering yet."""
    return S.setdefault("ask_uploads", {})


def add_uploads(files) -> list[str]:
    for f in files:
        uploads()[f.name] = f.getvalue()
    return [f.name for f in files]


def answer(question: str) -> QueryResult:
    try:
        return ask.answer_question(question, ask.today())
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
