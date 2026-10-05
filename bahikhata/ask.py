"""Ask Your Ledger: NL -> SQL orchestration (ARCHITECTURE.md §3.11, §5-6; TASK-023/024).

answer_question() is the single entry point the UI (and later the eval harness)
calls. Python precomputes today's named date ranges and hands them to the LLM in
the prompt; the LLM only copies a range into its WHERE clause, it never computes
one itself. The LLM's SQL always passes through sql_guard.check_sql and then
db.run_readonly_query -- this module never executes anything on its own.

Retries: ARCHITECTURE.md §3.11 allows exactly one retry, triggered by either a
guard rejection for a *format* reason (syntax error, money-alias rule) or a
database error -- never for an unsafe query, and never more than once per
question. Out-of-scope and ambiguous plans stop before any SQL is built.

strategy="text_to_sql" is the only strategy implemented. The parameter exists
now so a future "functions" fallback (ARCHITECTURE.md §6, designed but not
built) can plug into the same signature without changing the UI or eval.

The optional result explanation (ARCHITECTURE.md §6 "Optional explanation", S2,
TASK-050) is one more, non-retried LLM call made only after a non-empty result:
it receives the question and the formatted rows, never writes SQL and never
computes anything. check_explanation_numbers() -- a pure function, independent
of the LLM -- drops it if any number in it doesn't already appear in the rows
or the shown date range; the table is never blocked on it.

classify_error() and redact_secrets() are for the page's generic try/except
around answer_question() -- not for anything inside this module, which already
turns every failure case it recognises into a QueryResult.message instead of
raising (TASK-025: distinguish Ask error kinds).
"""

import re
from datetime import date, timedelta

import psycopg
from pydantic import BaseModel, ValidationError

from bahikhata import config, db, llm_client, sql_guard
from bahikhata.normalize import format_npr
from bahikhata.schemas import QueryPlan, QueryResult

_REFUSED_UNSAFE = "This request isn't allowed. I can only read your ledger."
_OUT_OF_SCOPE = "I can only answer questions about your saved receipts."
_NO_MATCH = "No matching records."
_COULD_NOT_BUILD = "I couldn't build a valid query — try rephrasing."
_GENERIC_CLARIFICATION = "Could you rephrase that? I wasn't sure what you meant."


def today() -> date:
    """The current date. A thin wrapper so tests can pin it (same pattern as db.today)."""
    return date.today()


def date_ranges(today: date) -> dict[str, tuple[date, date]]:
    """Named AD date ranges for the SQL prompt, computed in Python (ARCHITECTURE.md §6).

    The LLM copies one of these into its WHERE clause; it never computes a range
    itself. Weeks run Monday-Sunday. "last_month" is the previous AD calendar
    month, so a January `today` correctly gives December of the previous year.
    """
    monday = today - timedelta(days=today.weekday())
    return {
        "today": (today, today),
        "this_week": (monday, monday + timedelta(days=6)),
        "last_week": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "this_month": db.month_bounds(today),
        "last_month": db.month_bounds(today.replace(day=1) - timedelta(days=1)),
        "this_year": (date(today.year, 1, 1), date(today.year, 12, 31)),
        "last_year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
        "last_7_days": (today - timedelta(days=6), today),
        "last_30_days": (today - timedelta(days=29), today),
    }


def _format_date_ranges(ranges: dict[str, tuple[date, date]]) -> str:
    return "\n".join(f"- {name}: {start.isoformat()} to {end.isoformat()}" for name, (start, end) in ranges.items())


def _build_sql_prompt(question: str, today: date, extra_note: str = "") -> str:
    template = llm_client.load_prompt(config.SQL_PROMPT)
    filled = (
        template
        .replace("{today}", today.isoformat())
        .replace("{date_ranges}", _format_date_ranges(date_ranges(today)))
    )
    note = f"\n\n{extra_note}" if extra_note else ""
    return f"{filled}\n\nQuestion: {question}{note}\n\nReturn only the JSON."


def _get_plan(question: str, today: date, *, extra_note: str = "", client=None) -> tuple[QueryPlan | None, str | None]:
    """One LLM call -> (plan, None) on success, or (None, user-safe message) on any failure.

    Cached by question + today + extra_note (which differs between the first
    call and a retry, so a retry is never served a stale cache hit) + model +
    prompt version (added by llm_client.cache_key).
    """
    prompt_text = _build_sql_prompt(question, today, extra_note)
    cache_input = f"{question}\x1f{today.isoformat()}\x1f{extra_note}"
    try:
        response = llm_client.call_json(
            prompt_text, namespace="sql", prompt_version=config.SQL_PROMPT,
            cache_input=cache_input, response_schema=QueryPlan, client=client,
        )
    except llm_client.LLMError as exc:
        return None, exc.user_message
    try:
        return QueryPlan.model_validate_json(response.text), None
    except ValidationError:
        return None, _COULD_NOT_BUILD


def _run_plan(plan: QueryPlan) -> tuple[sql_guard.GuardResult, db.QueryExecutionResult | None]:
    """Guard-check plan.sql and, if it passes, execute it. Never executes an unguarded string."""
    guard_result = sql_guard.check_sql(plan.sql or "")
    if not guard_result.ok:
        return guard_result, None
    return guard_result, db.run_readonly_query(guard_result.sql_to_run)


def _is_empty_result(rows: list[tuple]) -> bool:
    """True for zero rows, or the single NULL row a bare SUM/AVG/MAX gives over no matches
    (ARCHITECTURE.md §5: shown as "No matching records.", never as Rs 0.00)."""
    if not rows:
        return True
    return len(rows) == 1 and all(v is None for v in rows[0])


def _format_rows(columns: list[str], rows: list[tuple]) -> list[tuple]:
    """*_paisa columns -> "Rs x,xxx.xx" by code; every other column passed through unchanged.
    The LLM never sees or produces a formatted amount (ARCHITECTURE.md §6 "Result").

    SUM/AVG over a bigint column comes back from psycopg as a Decimal (Postgres's
    NUMERIC), not a plain int, even though the guard's money-alias rule has already
    confirmed the column is money -- round to the nearest whole paisa before formatting
    (same reasoning as db.py's dashboard queries, which cast with ::bigint instead).
    """
    money = [i for i, name in enumerate(columns) if name.lower().endswith("_paisa")]
    return [
        tuple(
            format_npr(int(round(value))) if i in money and value is not None else value
            for i, value in enumerate(row)
        )
        for row in rows
    ]


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> set[str]:
    """Numeric tokens in `text`, with grouping commas stripped so "1,250.50" and "1250.50"
    compare equal. Each token keeps its own decimal places, so "1250" and "1250.50" are
    deliberately treated as different numbers."""
    return {match.replace(",", "") for match in _NUMBER_RE.findall(text)}


def check_explanation_numbers(explanation: str, formatted_rows: list[tuple], date_range_text: str = "") -> bool:
    """True if every number in `explanation` already appears in the formatted rows or the
    date range (ARCHITECTURE.md §6 "Optional explanation"). Pure function, no LLM or DB.

    formatted_rows must already be the *_paisa -> "Rs ..." display strings (see
    _format_rows): checking against raw paisa integers would let a 100x display
    error (rupees vs paisa) slip through unnoticed. A plain count (e.g. from
    COUNT(*)) is just an int cell and compares the same way.
    """
    allowed = _numbers_in(date_range_text)
    for row in formatted_rows:
        for cell in row:
            if cell is not None:
                allowed |= _numbers_in(str(cell))
    return _numbers_in(explanation) <= allowed


class _Explanation(BaseModel):
    """llm_client always requests JSON (A§3.4); this is the schema for the one-sentence answer."""

    text: str


def _maybe_explain(question: str, plan: QueryPlan, formatted_rows: list[tuple], *, client=None) -> str | None:
    """One optional, non-retried LLM call (ARCHITECTURE.md §6 "Optional explanation").

    Returns None (table shown alone) if the call fails, the response doesn't
    parse, or any number in it fails check_explanation_numbers. Called only
    after a non-empty result, so an empty result never reaches this at all.
    """
    date_range_text = ""
    if plan.date_range_start and plan.date_range_end:
        date_range_text = f"{plan.date_range_start} to {plan.date_range_end}"
    rows_text = "\n".join(", ".join(str(v) for v in row) for row in formatted_rows)
    prompt = (
        llm_client.load_prompt(config.EXPLANATION_PROMPT)
        .replace("{question}", question)
        .replace("{date_range}", date_range_text or "(none)")
        .replace("{rows}", rows_text)
    )
    cache_input = f"{question}\x1f{date_range_text}\x1f{rows_text}"
    try:
        response = llm_client.call_json(
            prompt, namespace="explain", prompt_version=config.EXPLANATION_PROMPT,
            cache_input=cache_input, response_schema=_Explanation, client=client,
        )
        explanation = _Explanation.model_validate_json(response.text).text.strip()
    except (llm_client.LLMError, ValidationError):
        return None
    if not explanation or not check_explanation_numbers(explanation, formatted_rows, date_range_text):
        return None
    return explanation


def answer_question(question: str, today: date, *, strategy: str = "text_to_sql", client=None) -> QueryResult:
    """question + today -> QueryResult. The single entry point for the UI and eval.

    Never raises for a bad question, an unsafe query, or a provider/database
    failure: every one of those comes back as `message`, not an exception.
    """
    if strategy != "text_to_sql":
        raise NotImplementedError(
            f"strategy={strategy!r} is not implemented (ARCHITECTURE.md §6: designed, not built)."
        )

    base = {"question": question, "prompt_version": config.SQL_PROMPT, "model_name": config.MODEL_NAME}
    stripped = question.strip()
    if not stripped:
        return QueryResult(**base, message="Please type a question.")
    if len(question) > config.MAX_QUESTION_CHARS:
        return QueryResult(
            **base, message=f"Please ask a shorter question (max {config.MAX_QUESTION_CHARS} characters).",
        )

    plan, error = _get_plan(stripped, today, client=client)
    if plan is None:
        return QueryResult(**base, message=error)
    if plan.status == "out_of_scope":
        return QueryResult(**base, plan=plan, message=_OUT_OF_SCOPE)
    if plan.status == "ambiguous":
        return QueryResult(**base, plan=plan, message=plan.clarification or _GENERIC_CLARIFICATION)
    if not plan.sql or not plan.sql.strip():
        return QueryResult(**base, plan=plan, message=_COULD_NOT_BUILD)

    guard_result, exec_result = _run_plan(plan)
    retried = False

    if not guard_result.ok and guard_result.retryable:
        retried = True
        retry_plan, _ = _get_plan(
            stripped, today,
            extra_note=f"Your previous SQL was rejected: {guard_result.reason}. Fix it and return the JSON again.",
            client=client,
        )
        if retry_plan is not None and retry_plan.status == "ok" and retry_plan.sql and retry_plan.sql.strip():
            plan, (guard_result, exec_result) = retry_plan, _run_plan(retry_plan)

    if not guard_result.ok:
        message = _REFUSED_UNSAFE if not guard_result.retryable else _COULD_NOT_BUILD
        return QueryResult(**base, plan=plan, message=message)

    if not retried and exec_result is not None and not exec_result.ok:
        retry_plan, _ = _get_plan(
            stripped, today,
            extra_note=f"Your previous SQL failed to run: {exec_result.error}. Fix it and return the JSON again.",
            client=client,
        )
        if retry_plan is not None and retry_plan.status == "ok" and retry_plan.sql and retry_plan.sql.strip():
            plan, (guard_result, exec_result) = retry_plan, _run_plan(retry_plan)
            if not guard_result.ok:
                message = _REFUSED_UNSAFE if not guard_result.retryable else _COULD_NOT_BUILD
                return QueryResult(**base, plan=plan, message=message)

    if exec_result is None or not exec_result.ok:
        return QueryResult(**base, plan=plan, sql_executed=guard_result.sql_to_run, message=_COULD_NOT_BUILD)

    if _is_empty_result(exec_result.rows):
        return QueryResult(
            **base, plan=plan, sql_executed=guard_result.sql_to_run, columns=exec_result.columns, message=_NO_MATCH,
        )

    formatted_rows = _format_rows(exec_result.columns, exec_result.rows)
    explanation = _maybe_explain(stripped, plan, formatted_rows, client=client)
    return QueryResult(
        **base, plan=plan, sql_executed=guard_result.sql_to_run,
        columns=exec_result.columns, rows=exec_result.rows, formatted_rows=formatted_rows,
        explanation=explanation,
    )


# --- Error-kind classification for the page's generic catch (TASK-025) -------
#
# answer_question() itself never raises for a bad question, an unsafe query, or
# a provider/database failure it recognises (see its docstring) -- these two
# helpers are for whatever still escapes it (e.g. a missing DATABASE_URL_READONLY,
# raised by config.get_database_url_readonly() before db.run_readonly_query's own
# try/except even starts). classify_error() never reads the exception's own text
# for the message it hands back, and redact_secrets() is for the separate,
# developer-facing log line, so neither a URL, a password, nor the user's SQL can
# reach the UI or an unredacted log.

_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_SECRET_KV_RE = re.compile(r"(?i)\b(password|pwd|apikey|api_key|token|secret)\s*=\s*\S+")


def redact_secrets(text: str) -> str:
    """Strip anything shaped like a URL/URI or a password=/key=-style field, so a raw
    driver or config exception's text is safe to put in a log line."""
    return _SECRET_KV_RE.sub("[redacted]", _URL_RE.sub("[redacted]", text))


def classify_error(exc: BaseException) -> tuple[str, str]:
    """(kind, user-safe message) for an exception that escaped answer_question.

    kind is for logs only, never shown to the user: "llm", "config", "database"
    or "query" (the fallback for anything unrecognised, e.g. a guard/parsing bug).
    """
    if isinstance(exc, llm_client.LLMError):
        return "llm", exc.user_message
    if isinstance(exc, RuntimeError):
        return "config", "The app isn't configured correctly — contact the administrator."
    if isinstance(exc, psycopg.Error):
        return "database", "A database error occurred — try again."
    return "query", "I couldn't build a safe query for that — try rephrasing."
