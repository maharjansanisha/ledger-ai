"""TASK-022: the SQL safety guard (ARCHITECTURE.md §3.12, §6; Amendment A1).

Pure logic -- sql_guard.check_sql never touches a database. The adversarial
lists below are the first lock; tests/test_db.py::test_run_readonly_query_*
cover the second lock (the read-only role/transaction), including running
every one of these unsafe strings through the real executor.
"""

import pytest

from bahikhata.sql_guard import ROW_LIMIT, check_sql

# --- Safe: must be accepted -------------------------------------------------

SAFE = [
    "SELECT * FROM receipts",
    "SELECT * FROM receipts LIMIT 50",
    "SELECT total_paisa FROM receipts",
    "SELECT total_paisa AS total_paisa FROM receipts",
    "SELECT SUM(total_paisa) AS total_paisa FROM receipts",
    "SELECT category, SUM(total_paisa) AS total_paisa FROM receipts GROUP BY category",
    "SELECT merchant_name, COUNT(*) AS n FROM receipts GROUP BY merchant_name",
    "WITH x AS (SELECT * FROM receipts) SELECT * FROM x",
    "WITH x AS (SELECT * FROM receipts), y AS (SELECT * FROM x) SELECT * FROM y",
    "SELECT r.* FROM receipts r JOIN line_items li ON li.receipt_id = r.id",
    "SELECT * FROM receipts r JOIN line_items li ON li.receipt_id = r.id",
    'SELECT * FROM "receipts"',                       # quoted identifier
    "select * from RECEIPTS",                          # case trick
    "SeLeCt * FrOm ReCeIpTs",                           # mixed-case keyword trick
    "SELECT * FROM public.receipts",                    # explicit default schema is fine
    "SELECT * FROM receipts WHERE merchant_name = 'Shree Traders'",
    "/* comment */ SELECT * FROM receipts -- trailing",  # comment trick (harmless)
    "select/**/ *from receipts",                         # whitespace-via-comment trick
    'SELECT SUM(total_paisa) AS "total_paisa" FROM receipts',  # quoted alias, still ends in _paisa
    "WITH receipts AS (SELECT 1 AS n) SELECT * FROM receipts",  # CTE shadows the name; body is harmless
]

# --- Unsafe: must be rejected, and never retried ----------------------------

UNSAFE_NON_RETRYABLE = [
    # DML/DDL
    "DELETE FROM receipts",
    "DROP TABLE receipts",
    "UPDATE receipts SET total_paisa = 0",
    "INSERT INTO receipts DEFAULT VALUES",
    "TRUNCATE receipts",
    "MERGE INTO receipts USING line_items ON true WHEN MATCHED THEN DELETE",
    "CREATE TABLE evil AS SELECT * FROM receipts",
    "ALTER TABLE receipts DROP COLUMN total_paisa",
    "GRANT SELECT ON receipts TO PUBLIC",
    "REVOKE SELECT ON receipts FROM ledger_reader",
    # Stacked / multiple statements (SQL-injection style)
    "SELECT 1; DROP TABLE receipts",
    "SELECT * FROM receipts; DROP TABLE receipts",
    "SELECT * FROM receipts WHERE merchant_name = 'x'; SELECT pg_sleep(5)--",
    # Nested: unsafe statement hidden inside a CTE or subquery
    "WITH x AS (DELETE FROM receipts RETURNING *) SELECT * FROM x",
    "WITH x AS (SELECT * FROM receipts), y AS (DELETE FROM line_items RETURNING *) SELECT * FROM x",
    # Postgres-specific admin/session statements
    "PRAGMA table_info(receipts)",
    "SET statement_timeout = 0",
    "COPY receipts TO STDOUT",
    "COPY receipts FROM '/etc/passwd'",
    "CALL some_proc()",
    "DO $$ BEGIN DELETE FROM receipts; END $$",
    "VACUUM receipts",
    "EXPLAIN SELECT * FROM receipts",
    "LISTEN foo",
    # SELECT that is secretly a write, or takes locks
    "SELECT * INTO newtable FROM receipts",
    "SELECT * FROM receipts FOR UPDATE",
    "SELECT * FROM receipts FOR SHARE",
    # Disallowed tables, including the audit table and system catalogs
    "SELECT * FROM receipt_audit",
    "SELECT * FROM users",
    "SELECT * FROM (SELECT * FROM receipt_audit) t",        # nested subquery trick
    "WITH x AS (SELECT * FROM receipt_audit) SELECT * FROM x",  # CTE-wrapped trick
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM information_schema.columns WHERE table_name = 'receipts'",
    "SELECT * FROM other_schema.receipts",                  # allowed name, wrong schema
    "SELECT * FROM otherdb.public.receipts",                # catalog-qualified
    # Functions that read files, sleep, or reach other databases/processes
    "SELECT pg_sleep(10)",
    "SELECT PG_SLEEP(10)",                                   # case trick
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT dblink('x', 'y')",
    "SELECT lo_import('/etc/passwd')",
    # Functions that run their own SQL string (invisible to the table check) or touch settings
    "SELECT query_to_xml('SELECT * FROM receipt_audit', true, true, '')",
    "SELECT table_to_xml('receipt_audit', true, true, '')",
    "SELECT * FROM receipts WHERE merchant_name = CAST(query_to_xml('SELECT 1', true, true, '') AS TEXT)",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT current_setting('server_version')",
    # Query shape not supported (narrow, safe default-deny)
    "SELECT * FROM receipts UNION SELECT * FROM line_items",
]

# --- Unsafe, but retryable: a format problem the LLM could fix -------------

RETRYABLE_REJECTS = [
    "",
    "   ",
    "SELEKT * FROM receipts",                                 # syntax error
    "SELECT * FROM receipts WHERE (",                         # syntax error
    "SELECT SUM(total_paisa) AS total FROM receipts",         # money-alias rule
    "SELECT SUM(total_paisa) FROM receipts",                  # money-alias rule, no alias at all
    "SELECT total_paisa AS total FROM receipts",              # money-alias rule, bare column renamed
    'SELECT SUM(total_paisa) AS "total" FROM receipts',       # money-alias rule, quoted alias
    "SELECT column1 FROM (SELECT total_paisa AS column1 FROM receipts) t",  # renamed one level down
]


@pytest.mark.parametrize("sql", SAFE)
def test_accepts_safe_sql(sql):
    result = check_sql(sql)
    assert result.ok, result.reason
    assert result.sql_to_run is not None
    assert "LIMIT" in result.sql_to_run.upper()


@pytest.mark.parametrize("sql", UNSAFE_NON_RETRYABLE)
def test_rejects_unsafe_sql_without_retry(sql):
    result = check_sql(sql)
    assert not result.ok, f"expected rejection for {sql!r}"
    assert result.retryable is False, f"expected non-retryable for {sql!r}, got: {result.reason}"
    assert result.sql_to_run is None


@pytest.mark.parametrize("sql", RETRYABLE_REJECTS)
def test_rejects_format_problems_as_retryable(sql):
    result = check_sql(sql)
    assert not result.ok, f"expected rejection for {sql!r}"
    assert result.retryable is True, f"expected retryable for {sql!r}, got: {result.reason}"
    assert result.sql_to_run is None


def test_appends_limit_when_missing():
    result = check_sql("SELECT * FROM receipts")
    assert result.ok
    assert result.sql_to_run.rstrip().endswith(f"LIMIT {ROW_LIMIT}")


def test_caps_a_limit_above_the_row_limit():
    result = check_sql("SELECT * FROM receipts LIMIT 999999999")
    assert result.ok
    assert result.sql_to_run.rstrip().endswith(f"LIMIT {ROW_LIMIT}")


def test_keeps_a_limit_at_or_below_the_row_limit():
    result = check_sql("SELECT * FROM receipts LIMIT 10")
    assert result.ok
    assert result.sql_to_run.rstrip().endswith("LIMIT 10")


def test_allows_a_join_across_both_allowed_tables():
    result = check_sql(
        "SELECT r.merchant_name, li.description FROM receipts r "
        "JOIN line_items li ON li.receipt_id = r.id"
    )
    assert result.ok, result.reason
