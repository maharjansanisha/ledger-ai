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

The optional result explanation (S2) is TASK-050, a P1 "SHOULD" item that
TASKS.md §18 says not to start before the end-of-Sunday (query slice) checkpoint
passes -- so QueryResult.explanation is always None here; it is not produced by
this module yet.
"""

from datetime import date, timedelta

from pydantic import ValidationError

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
    return QueryResult(
        **base, plan=plan, sql_executed=guard_result.sql_to_run,
        columns=exec_result.columns, rows=exec_result.rows, formatted_rows=formatted_rows,
    )
