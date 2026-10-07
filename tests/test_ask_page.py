"""TASK-025: the Ask Your Ledger page, driven by Streamlit's AppTest.

ask.answer_question is monkeypatched to canned QueryResult values (same style as
tests/test_dashboard_page.py's db monkeypatching): this page test is about the
chat wiring and rendering, not about ask.py's own logic, which tests/test_ask.py
already covers. No network, no database.
"""

from pathlib import Path

import psycopg
from streamlit.testing.v1 import AppTest

from bahikhata import ask, config, llm_client
from bahikhata.schemas import QueryPlan, QueryResult

PAGE = str(Path(__file__).resolve().parent.parent / "pages" / "ask_your_ledger" / "page.py")

BASE = {"prompt_version": config.SQL_PROMPT, "model_name": config.MODEL_NAME}


def start(monkeypatch, outcomes: dict[str, QueryResult]) -> AppTest:
    """outcomes maps question text -> the QueryResult answer_question should return for it."""
    monkeypatch.setattr(ask, "answer_question", lambda question, today, **kw: outcomes[question])
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    return at


def ask_question(at: AppTest, question: str) -> AppTest:
    return at.chat_input[0].set_value(question).run()


def test_out_of_scope_question_shows_the_refusal_message(monkeypatch):
    result = QueryResult(question="x", message="I can only answer questions about your saved receipts.", **BASE)
    at = start(monkeypatch, {"Should I take a loan?": result})
    at = ask_question(at, "Should I take a loan?")
    assert not at.exception
    assert len(at.chat_message) == 2  # one user bubble, one assistant bubble
    assert at.chat_message[0].name == "user"
    assert at.chat_message[1].name == "assistant"
    assert any("saved receipts" in i.value for i in at.info)


def test_answer_with_rows_shows_table_and_sql_expander(monkeypatch):
    plan = QueryPlan(status="ok", sql="SELECT SUM(total_paisa) AS total_paisa FROM receipts",
                      date_range_start="2026-09-01", date_range_end="2026-09-30")
    result = QueryResult(
        question="x", plan=plan, sql_executed="SELECT SUM(total_paisa) AS total_paisa FROM receipts LIMIT 200",
        columns=["total_paisa"], rows=[(125000,)], formatted_rows=[("Rs 1,250.00",)], **BASE,
    )
    at = start(monkeypatch, {"How much did I spend last month?": result})
    at = ask_question(at, "How much did I spend last month?")
    assert not at.exception
    assert len(at.dataframe) == 1
    table = at.dataframe[0].value
    assert list(table["total_paisa"]) == ["Rs 1,250.00"]
    assert "2026-09-01 to 2026-09-30" in "\n".join(c.value for c in at.caption)
    assert at.expander[0].label == "SQL"
    assert "LIMIT 200" in at.expander[0].code[0].value


def test_explanation_shown_above_the_table_when_present(monkeypatch):
    plan = QueryPlan(status="ok", sql="SELECT SUM(total_paisa) AS total_paisa FROM receipts")
    result = QueryResult(
        question="x", plan=plan, sql_executed="SELECT SUM(total_paisa) AS total_paisa FROM receipts LIMIT 200",
        columns=["total_paisa"], rows=[(125000,)], formatted_rows=[("Rs 1,250.00",)],
        explanation="You spent Rs 1,250.00.", **BASE,
    )
    at = start(monkeypatch, {"How much did I spend?": result})
    at = ask_question(at, "How much did I spend?")
    assert not at.exception
    assert "You spent Rs 1,250.00." in "\n".join(m.value for m in at.markdown)
    assert len(at.dataframe) == 1  # table is still shown alongside the sentence

    assistant_bubble = at.chat_message[1]  # user bubble, then assistant bubble
    child_types = [type(child).__name__ for child in assistant_bubble.children.values()]
    assert child_types.index("Markdown") < child_types.index("Dataframe")


def test_refused_unsafe_sql_shows_message_and_sql_not_run_expander(monkeypatch):
    plan = QueryPlan(status="ok", sql="DROP TABLE receipts")
    result = QueryResult(
        question="x", plan=plan, message="This request isn't allowed. I can only read your ledger.", **BASE,
    )
    at = start(monkeypatch, {"Delete everything": result})
    at = ask_question(at, "Delete everything")
    assert not at.exception
    assert any("isn't allowed" in i.value for i in at.info)
    assert at.expander[0].label == "SQL (not run)"
    assert "DROP TABLE" in at.expander[0].code[0].value


def test_unexpected_config_error_shows_config_message_and_logs_no_secret(monkeypatch, caplog):
    """Reproduces the real bug: DATABASE_URL_READONLY unset raises a RuntimeError
    that escapes answer_question entirely (TASK-025: distinguish Ask error kinds)."""
    def raise_error(question, today, **kw):
        raise RuntimeError("DATABASE_URL_READONLY is not set. postgresql://user:hunter2@host/db")

    monkeypatch.setattr(ask, "answer_question", raise_error)
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    with caplog.at_level("ERROR"):
        at = ask_question(at, "How much did I spend?")
    assert not at.exception
    assert any("contact the administrator" in i.value for i in at.info)
    assert not any("went wrong" in i.value for i in at.info)  # no more generic message
    our_errors = [r for r in caplog.records if r.message.startswith("Ask Your Ledger failed")]
    assert len(our_errors) == 1
    logged = our_errors[0].message
    assert "RuntimeError" in logged and "config" in logged
    assert "hunter2" not in logged and "postgresql://" not in logged  # redacted


def test_unexpected_database_error_shows_database_message(monkeypatch):
    def raise_error(question, today, **kw):
        raise psycopg.OperationalError("could not connect to server")

    monkeypatch.setattr(ask, "answer_question", raise_error)
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    at = ask_question(at, "How much did I spend?")
    assert not at.exception
    assert any("database error" in i.value.lower() for i in at.info)


def test_unexpected_llm_error_reuses_its_own_user_message(monkeypatch):
    def raise_error(question, today, **kw):
        raise llm_client.LLMError("rate_limit", "Free-tier limit reached — wait a minute and retry.")

    monkeypatch.setattr(ask, "answer_question", raise_error)
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    at = ask_question(at, "How much did I spend?")
    assert not at.exception
    assert any("Free-tier limit reached" in i.value for i in at.info)


def test_multiple_questions_each_render_as_their_own_chat_turn(monkeypatch):
    first = QueryResult(question="x", message="I can only answer questions about your saved receipts.", **BASE)
    second = QueryResult(question="x", message="No matching records.", **BASE)
    at = start(monkeypatch, {"Should I take a loan?": first, "How much did I spend on Mars?": second})
    at = ask_question(at, "Should I take a loan?")
    at = ask_question(at, "How much did I spend on Mars?")
    assert not at.exception
    assert len(at.chat_message) == 4  # 2 questions x (user + assistant)
    infos = [i.value for i in at.info]
    assert any("saved receipts" in m for m in infos)
    assert any("No matching records." in m for m in infos)


def test_no_conversation_memory_each_question_answered_independently(monkeypatch):
    """Nothing from an earlier turn is passed into a later answer_question call."""
    seen_calls = []

    def fake_answer(question, today, **kw):
        seen_calls.append((question, kw))
        return QueryResult(question=question, message="No matching records.", **BASE)

    monkeypatch.setattr(ask, "answer_question", fake_answer)
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    at = ask_question(at, "first question")
    at = ask_question(at, "second question")
    assert [c[0] for c in seen_calls] == ["first question", "second question"]
    assert all(kw == {} for _question, kw in seen_calls)  # no history/context argument is passed
