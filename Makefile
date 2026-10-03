# BahiKhata AI -- database migrations (Alembic, Amendment A2).
#
# Targets a Neon Postgres database. Which one is picked by TARGET:
#   TARGET=main (default) -> DATABASE_URL       (the real Neon "neondb")
#   TARGET=eval            -> EVAL_DATABASE_URL  (Neon "ledger_eval")
#   TARGET=test             -> TEST_DATABASE_URL (Neon "ledger_test")
# All three env vars are read from .env (see .env.example); migrations/env.py
# does the lookup and refuses eval/test targets that don't point at a
# database whose name ends in _eval / _test, so main (your real ledger data)
# can't be hit by mistake.
#
# Examples:
#   make migrate                 # apply pending migrations to Neon (DATABASE_URL)
#   make migrate TARGET=test     # apply pending migrations to ledger_test
#   make migrate-reset           # DROP everything and re-apply from scratch (destructive)
#   make migrate-new name="add foo column"

TARGET ?= main
ALEMBIC := ALEMBIC_TARGET=$(TARGET) uv run alembic

.PHONY: help migrate migrate-up migrate-down migrate-reset migrate-new migrate-history migrate-current

help: ## Show this list of targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

migrate: migrate-up ## Alias for migrate-up

migrate-up: ## Apply all pending migrations (alembic upgrade head)
	$(ALEMBIC) upgrade head

migrate-down: ## Roll back the most recent migration (alembic downgrade -1)
	$(ALEMBIC) downgrade -1

migrate-reset: ## DROP every ledger table and re-apply all migrations from scratch (DESTROYS DATA)
	@echo "This will DROP ALL TABLES in the '$(TARGET)' database and recreate them from migrations."
	@read -p "Type 'reset' to continue: " ans; \
	if [ "$$ans" != "reset" ]; then echo "Aborted."; exit 1; fi
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head

migrate-new: ## Create a new empty migration file (usage: make migrate-new name="add foo column")
	$(ALEMBIC) revision -m "$(name)"

migrate-history: ## Show the full migration history
	$(ALEMBIC) history --verbose

migrate-current: ## Show the migration currently applied to the target database
	$(ALEMBIC) current --verbose
