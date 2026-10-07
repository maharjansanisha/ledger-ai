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
