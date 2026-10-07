"""Hybrid Ask Your Ledger: router + documents + SQL (ask.answer_question(strategy="hybrid")).

FakeClient stands in for Gemini and FakeIndex for Pinecone; db.run_readonly_query
is monkeypatched. Queued FakeClient outcomes are consumed in call order: route,
then (for "both") the SQL plan and its explanation, then the document answer.
"""

import json
from datetime import date

import pytest
from pinecone import PineconeError

from bahikhata import ask, config, db, rag
from tests.fakes import FakeClient, FakeIndex

TODAY = date(2026, 10, 3)
SQL = "SELECT SUM(total_paisa) AS total_paisa FROM receipts"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RAG_DIR", tmp_path / "rag")
    monkeypatch.setattr(config, "LLM_RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "has_rag_config", lambda: True)
    monkeypatch.setattr(
        db, "run_readonly_query",
        lambda sql, url=None: db.QueryExecutionResult(ok=True, columns=["total_paisa"], rows=[(725000,)]),
    )
    fake = FakeIndex()
    rag.set_index(fake)
    yield fake
    rag.set_index(None)


def route_json(route):
    return json.dumps({"route": route})


def plan_json(sql=SQL, start="2026-09-01", end="2026-09-30"):
    return json.dumps({"status": "ok", "sql": sql, "date_range_start": start, "date_range_end": end,
                       "clarification": None})


def answer_json(answer, cited=(1,)):
    return json.dumps({"answer": answer, "cited": list(cited)})


def index_budget():
    rag.ingest("budget.txt", b"Monthly transport budget is Rs 5,000.")


def test_no_documents_skips_the_router_entirely():
    client = FakeClient(plan_json(), json.dumps({"text": "You spent Rs 7,250.00."}))
    result = ask.answer_question("How much did I spend?", TODAY, strategy="hybrid", client=client)
    assert result.formatted_rows == [("Rs 7,250.00",)]
    assert result.route is None
    assert len(client.calls) == 2  # plan + explanation, no route call


def test_rag_not_configured_skips_the_router(monkeypatch):
    index_budget()
    monkeypatch.setattr(config, "has_rag_config", lambda: False)
    client = FakeClient(plan_json(), json.dumps({"text": "x"}))
    ask.answer_question("How much did I spend?", TODAY, strategy="hybrid", client=client)
    assert len(client.calls) == 2


def test_sql_route_behaves_like_text_to_sql():
    index_budget()
    client = FakeClient(route_json("sql"), plan_json(), json.dumps({"text": "You spent Rs 7,250.00."}))
    result = ask.answer_question("How much did I spend?", TODAY, strategy="hybrid", client=client)
    assert result.route == "sql"
    assert result.formatted_rows == [("Rs 7,250.00",)]
    assert result.explanation == "You spent Rs 7,250.00."
    assert result.sources == []


def test_docs_route_answers_from_the_cited_excerpt():
    index_budget()
    client = FakeClient(route_json("docs"), answer_json("Your monthly transport budget is Rs 5,000."))
    result = ask.answer_question("What is my transport budget?", TODAY, strategy="hybrid", client=client)
    assert result.route == "docs"
    assert result.answer == "Your monthly transport budget is Rs 5,000."
    assert [s.doc_name for s in result.sources] == ["budget.txt"]
    assert result.sql_executed is None
    assert "Monthly transport budget is Rs 5,000." in client.calls[1]["contents"][0]


def test_docs_route_with_no_relevant_chunks_says_so():
    index_budget()
    client = FakeClient(route_json("docs"))
    result = ask.answer_question("Who is the landlord?", TODAY, strategy="hybrid", client=client)
    assert result.message == "I couldn't find anything relevant in your uploaded documents."
    assert len(client.calls) == 1  # no answer call when nothing was retrieved


def test_both_route_merges_rows_and_document_answer():
    index_budget()
    client = FakeClient(
        route_json("both"), plan_json(), json.dumps({"text": "You spent Rs 7,250.00."}),
        answer_json("You spent Rs 7,250.00 on transport, above the Rs 5,000 budget."),
    )
    result = ask.answer_question("Did transport spending exceed the budget?", TODAY,
                                 strategy="hybrid", client=client)
    assert result.route == "both"
    assert result.formatted_rows == [("Rs 7,250.00",)]
    assert result.answer == "You spent Rs 7,250.00 on transport, above the Rs 5,000 budget."
    assert [s.doc_name for s in result.sources] == ["budget.txt"]
    answer_prompt = client.calls[3]["contents"][0]
    assert "Rs 7,250.00" in answer_prompt and "2026-09-01 to 2026-09-30" in answer_prompt


def test_invented_number_drops_the_answer_but_keeps_sources():
    index_budget()
    client = FakeClient(route_json("docs"), answer_json("Your budget is Rs 6,000, so you have Rs 1,000 left."))
    result = ask.answer_question("What is my transport budget?", TODAY, strategy="hybrid", client=client)
    assert result.answer is None
    assert "couldn't write a reliable answer" in result.message
    assert [s.doc_name for s in result.sources] == ["budget.txt"]


def test_both_route_rejects_a_computed_difference():
    index_budget()
    client = FakeClient(
        route_json("both"), plan_json(), json.dumps({"text": "x"}),
        answer_json("You went Rs 2,250.00 over your Rs 5,000 budget."),  # 2,250.00 computed, not in context
    )
    result = ask.answer_question("Over budget?", TODAY, strategy="hybrid", client=client)
    assert result.answer is None
    assert result.formatted_rows == [("Rs 7,250.00",)]  # ledger table still shown


def test_router_failure_falls_back_to_sql():
    index_budget()
    client = FakeClient("not json", plan_json(), json.dumps({"text": "x"}))
    result = ask.answer_question("How much did I spend?", TODAY, strategy="hybrid", client=client)
    assert result.route == "sql"
    assert result.formatted_rows == [("Rs 7,250.00",)]


def test_pinecone_down_on_docs_route_returns_a_safe_message(env):
    index_budget()
    env.fail = PineconeError("connection reset by https://secret-host")
    client = FakeClient(route_json("docs"))
    result = ask.answer_question("What is my transport budget?", TODAY, strategy="hybrid", client=client)
    assert result.message == "Document search is unavailable right now — try again shortly."
    assert "secret-host" not in result.message


def test_pinecone_down_on_both_route_still_shows_ledger_rows(env):
    index_budget()
    env.fail = PineconeError("down")
    client = FakeClient(route_json("both"), plan_json(), json.dumps({"text": "You spent Rs 7,250.00."}))
    result = ask.answer_question("Over budget?", TODAY, strategy="hybrid", client=client)
    assert result.formatted_rows == [("Rs 7,250.00",)]
    assert "unavailable" in result.message
    assert result.answer is None


def test_empty_question_is_refused_before_any_call():
    index_budget()
    client = FakeClient()
    result = ask.answer_question("  ", TODAY, strategy="hybrid", client=client)
    assert result.message == "Please type a question."
    assert client.calls == []


def test_classify_error_maps_rag_errors_to_documents():
    kind, message = ask.classify_error(rag.RagError("unavailable", "Document search is unavailable."))
    assert (kind, message) == ("documents", "Document search is unavailable.")


@pytest.mark.parametrize("answer,ok", [
    ("Your budget is Rs 5,000.", True),
    ("Your budget is Rs 5,000 and you spent Rs 7,250.00.", True),
    ("See excerpt 1.", True),
    ("Your budget is Rs 4,000.", False),
])
def test_check_answer_numbers(answer, ok):
    context = ["2026-09-01 to 2026-09-30", "1", "Monthly transport budget is Rs 5,000."]
    assert ask.check_answer_numbers(answer, [("Rs 7,250.00",)], context) is ok
