"""SQL safety guard for "Ask Your Ledger" (ARCHITECTURE.md §3.12, §6; Amendment A1; TASK-022).

check_sql() is the only entry point and the first of two independent locks
(the second is db.run_readonly_query's read-only role/transaction). It never
executes anything -- it only decides whether a SQL string may run, and may
rewrite its LIMIT clause. It must not "fix" dangerous SQL in any other way.

Checks, in order (sqlglot, postgres dialect, per Amendment A1):
1. Parse. A syntax error is retryable (the LLM can be shown the error and asked again).
2. Exactly one statement.
3. The root must be a SELECT (a WITH ... SELECT is allowed); no SELECT INTO, no FOR UPDATE/SHARE.
4. No Insert/Update/Delete/Drop/Create/Alter/Pragma/Command/Set/Copy/Truncate/Merge/Grant/Revoke
   node anywhere in the tree (catches e.g. a DELETE hidden inside a CTE).
5. Every table referenced is receipts, line_items, or a CTE defined in the same query, and
   any schema qualifier must be `public` (so pg_catalog, information_schema or any other
   schema is rejected regardless of table name).
6. No pg_*-prefixed function (pg_sleep, pg_read_file, ...), no file/network/LO function, no
   function that runs a SQL string of its own (query_to_xml, ...) and so would slip past
   check 5, and no session-settings function (set_config, current_setting).
7. Money-alias rule: any output column (in any SELECT in the tree, including subqueries)
   that reads a `*_paisa` column must itself be named with a `*_paisa`-ending alias.
8. LIMIT 200 is added if missing, and any larger LIMIT is capped to 200.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from bahikhata import config

DIALECT = "postgres"
ROW_LIMIT = config.SQL_ROW_LIMIT  # 200

_ALLOWED_TABLES = frozenset(name.lower() for name in config.QUERY_ALLOWED_TABLES)
_ALLOWED_SCHEMAS = frozenset({"", "public"})

# Statement-level nodes that are never allowed, anywhere in the tree (not just
# at the root) -- this is what catches "WITH x AS (DELETE ... ) SELECT * FROM x".
_UNSAFE_NODE_TYPES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Pragma, exp.Command, exp.Set, exp.Copy, exp.TruncateTable, exp.Merge,
    exp.Grant, exp.Revoke,
)

# Amendment A1: "functions starting with pg_ (e.g. pg_sleep, pg_read_file)", plus
# other file/network/large-object helpers that don't happen to start with pg_.
_FORBIDDEN_FUNCTION_NAMES = frozenset({
    "dblink", "dblink_connect", "dblink_connect_u", "dblink_exec",
    "lo_import", "lo_export", "lo_read", "lo_write", "lo_open", "lo_create",
    "copy_from_program", "copy_to_program",
    # Run a SQL string (or dump a whole table/schema) inside a SELECT -- the guard
    # can't see the tables named inside that string.
    "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
    "table_to_xml", "table_to_xml_and_xmlschema", "table_to_xmlschema",
    "schema_to_xml", "schema_to_xml_and_xmlschema", "schema_to_xmlschema",
    "database_to_xml", "database_to_xml_and_xmlschema", "database_to_xmlschema",
    "cursor_to_xml", "cursor_to_xmlschema",
    # Read or change session settings (e.g. try to switch off read-only mode).
    "set_config", "current_setting",
})


@dataclass(frozen=True)
class GuardResult:
    """ok: safe to run as `sql_to_run` (LIMIT already capped). reason: why not,
    when ok is False. retryable: True only for a format problem the LLM could
    fix given the reason (syntax error, money-alias rule) -- false for
    anything unsafe, which must never be retried."""

    ok: bool
    sql_to_run: str | None
    reason: str | None
    retryable: bool


def _reject(reason: str, *, retryable: bool) -> GuardResult:
    return GuardResult(ok=False, sql_to_run=None, reason=reason, retryable=retryable)


def _is_paisa_column(node: exp.Expression) -> bool:
    return any(c.name.lower().endswith("_paisa") for c in node.find_all(exp.Column))


def _money_alias_violation(select: exp.Select) -> str | None:
    """None if every projection of this one SELECT that touches a *_paisa column
    is itself named with a *_paisa-ending alias (or is a bare passthrough of one)."""
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or not _is_paisa_column(projection):
            continue
        if isinstance(projection, exp.Alias):
            output_name = projection.alias
        elif isinstance(projection, exp.Column):
            output_name = projection.name
        else:
            output_name = ""  # an unaliased expression has no money-signalling name
        if not output_name.lower().endswith("_paisa"):
            got = output_name or "no alias"
            return f"a money column must be aliased to a name ending in _paisa (got {got!r})"
    return None


def _table_violation(tree: exp.Expression) -> str | None:
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    for table in tree.find_all(exp.Table):
        name, schema = table.name.lower(), table.db.lower()
        if schema not in _ALLOWED_SCHEMAS or table.catalog:
            return f"{table.sql(dialect=DIALECT)} is not allowed"
        if name in cte_names:
            continue
        if name not in _ALLOWED_TABLES:
            return f"table {table.name!r} is not allowed"
    return None


def _function_violation(tree: exp.Expression) -> str | None:
    for func in tree.find_all(exp.Func, exp.Anonymous):
        name = (func.name or "").lower()
        if name.startswith("pg_") or name in _FORBIDDEN_FUNCTION_NAMES:
            return f"function {name!r} is not allowed"
    return None


def _cap_limit(select: exp.Select) -> exp.Select:
    """Add LIMIT 200 if missing; cap an existing LIMIT above 200 down to 200."""
    select = select.copy()
    current = select.args.get("limit")
    value = None
    if current is not None and isinstance(current.expression, exp.Literal) and current.expression.is_int:
        value = int(current.expression.this)
    if value is None or value > ROW_LIMIT:
        select.set("limit", exp.Limit(expression=exp.Literal.number(ROW_LIMIT)))
    return select


def check_sql(sql: str) -> GuardResult:
    """The only gate between an LLM-written SQL string and the database."""
    if not sql or not sql.strip():
        return _reject("empty SQL", retryable=True)

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as exc:
        return _reject(f"SQL syntax error: {exc}", retryable=True)

    if len(statements) != 1:
        return _reject("exactly one SQL statement is allowed", retryable=False)
    tree = statements[0]

    if not isinstance(tree, exp.Select):
        return _reject("only a single SELECT statement is allowed", retryable=False)
    if tree.args.get("into"):
        return _reject("SELECT INTO is not allowed (it creates a table)", retryable=False)
    if tree.args.get("locks"):
        return _reject("row locking (FOR UPDATE / FOR SHARE) is not allowed", retryable=False)

    unsafe_node = tree.find(*_UNSAFE_NODE_TYPES)
    if unsafe_node is not None:
        return _reject(f"{type(unsafe_node).__name__} is not allowed", retryable=False)

    reason = _table_violation(tree)
    if reason is not None:
        return _reject(reason, retryable=False)

    reason = _function_violation(tree)
    if reason is not None:
        return _reject(reason, retryable=False)

    for select in tree.find_all(exp.Select):
        reason = _money_alias_violation(select)
        if reason is not None:
            return _reject(reason, retryable=True)

    capped = _cap_limit(tree)
    return GuardResult(ok=True, sql_to_run=capped.sql(dialect=DIALECT), reason=None, retryable=False)
