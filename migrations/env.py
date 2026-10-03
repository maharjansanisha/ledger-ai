"""Alembic environment: resolves the target database from environment variables.

Schema migrations run through Alembic (Amendment A2 in ARCHITECTURE.md, which
replaces the plain-SQL runner from Amendment A1). Target selection keeps the
same three databases the project already uses:

    make migrate                    # TARGET=main -> DATABASE_URL       (Neon "neondb")
    make migrate TARGET=eval        #              -> EVAL_DATABASE_URL (Neon "ledger_eval")
    make migrate TARGET=test        #              -> TEST_DATABASE_URL (Neon "ledger_test")

Each env var holds a full Postgres URL (`postgresql://...`). Alembic/SQLAlchemy
need the psycopg v3 dialect spelled out explicitly, so the scheme is rewritten
to `postgresql+psycopg://` here -- `.env` itself keeps the plain
`postgresql://` scheme so other tools (psql, etc.) still accept it unmodified.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bahikhata import config as app_config  # noqa: E402,F401  (importing loads .env)

# This is the Alembic Config object, which provides access to the values
# within the .ini file in use.
config = context.config

# Interpret the config file for Python logging. This sets up loggers.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models in this project (Amendment A1 principles unchanged): every
# migration is plain DDL via op.execute(), so there is no metadata to
# autogenerate against.
target_metadata = None

TARGET_ENV_VARS = {
    "main": "DATABASE_URL",
    "eval": "EVAL_DATABASE_URL",
    "test": "TEST_DATABASE_URL",
}
REQUIRED_DB_SUFFIX = {"main": None, "eval": "_eval", "test": "_test"}


def _resolve_url() -> str:
    """Pick the database URL for ALEMBIC_TARGET (default "main") and adapt it for psycopg v3."""
    target = os.getenv("ALEMBIC_TARGET", "main").strip().lower()
    if target not in TARGET_ENV_VARS:
        raise RuntimeError(
            f"Unknown ALEMBIC_TARGET={target!r}; expected one of {sorted(TARGET_ENV_VARS)}."
        )

    env_var = TARGET_ENV_VARS[target]
    url = os.getenv(env_var, "").strip()
    if not url or "<" in url:
        raise RuntimeError(f"{env_var} is not set in .env (see .env.example).")

    # Guard against running eval/test migrations on the real ledger by mistake.
    required_suffix = REQUIRED_DB_SUFFIX[target]
    db_name = url.split("/")[-1].split("?")[0]
    if required_suffix and not db_name.endswith(required_suffix):
        raise RuntimeError(
            f"Refusing: ALEMBIC_TARGET={target} must point at a database ending in "
            f"'{required_suffix}' (got '{db_name}')."
        )

    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):  # some providers use the short scheme
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    return url


config.set_main_option("sqlalchemy.url", _resolve_url())


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL instead of executing it)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,  # each revision is all-or-nothing, not the whole run
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode, against a real connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,  # each revision is all-or-nothing, not the whole run
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
