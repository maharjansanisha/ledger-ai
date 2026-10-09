# Ledger AI

Receipt-to-ledger assistant for Nepali small businesses (student MVP). Snap a receipt, let Gemini extract it, review and confirm, then explore your ledger on a dashboard or ask questions in plain language.

Flow: capture → validate → confirm → store → query.

## Stack

- Python 3.12 (managed with [uv](https://docs.astral.sh/uv/))
- Streamlit UI (`app.py` + `pages/`)
- Google Gemini via `google-genai` for receipt extraction, text-to-SQL and answer explanations
- Neon Postgres via `psycopg`, schema managed with Alembic
- `sqlglot` guard plus a read-only DB role for "Ask Your Ledger"

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed
- A Gemini API key from Google AI Studio
- A Neon Postgres project with three databases: `neondb` (main), `ledger_eval`, `ledger_test`

## Setup

1. Install dependencies (uv picks up Python 3.12 from `.python-version`):

   ```bash
   uv sync
   ```

2. Create your env file and fill in the values:

   ```bash
   cp .env.example .env
   ```

   | Variable | Purpose |
   | --- | --- |
   | `GEMINI_API_KEY` | Gemini API key (required) |
   | `GEMINI_MODEL` | Optional model override |
   | `DATABASE_URL` | Owner role on `neondb`, used by the app |
   | `DATABASE_URL_READONLY` | `ledger_reader` role on `neondb`, used only by Ask Your Ledger |
   | `EVAL_DATABASE_URL` | Owner role on `ledger_eval` |
   | `TEST_DATABASE_URL` | Owner role on `ledger_test` |
   | `TEST_DATABASE_URL_READONLY` | Optional, `ledger_reader` on `ledger_test` for grant tests |

   Use Neon's direct host (not the `-pooler` one).

3. Create the read-only role once, in the Neon SQL editor (the password is never committed):

   ```sql
   CREATE ROLE ledger_reader WITH LOGIN PASSWORD '<choose-a-strong-password>';
   ```

4. Apply migrations:

   ```bash
   make migrate               # main database (DATABASE_URL)
   make migrate TARGET=test   # ledger_test, needed for DB tests
   make migrate TARGET=eval   # ledger_eval
   ```

   Migration `0002` grants `ledger_reader` `SELECT` on `receipts` and `line_items` only. `receipt_audit` stays unreadable to that role, as does `line_item_edits` (migrations `0003`/`0004`: chat edit proposals, their outcome, and database-enforced lifecycle rules).

## Run the app

```bash
uv run streamlit run app.py
```

Streamlit opens at http://localhost:8501. The home page shows whether your Gemini key was found. Pages:

- **Capture and Review**: upload a receipt image, review the extracted draft, confirm to save
- **Dashboard**: spending summaries from confirmed receipts
- **Ask Your Ledger**: natural-language questions answered with guarded, read-only SQL, with an optional explanation of the result. You can also ask it to correct one value on a saved line item (e.g. “Change the quantity of rice on receipt #12 from 2 to 5”): it shows the proposed change and nothing is saved until you click **Approve change** (see `docs/ARCHITECTURE.md` Amendment A3)

## Tests

```bash
uv run pytest
```

Database tests skip automatically when `TEST_DATABASE_URL` (or `TEST_DATABASE_URL_READONLY`) isn't set, and refuse to run against a database whose name doesn't end in `_test`.

## Migration commands

Run `make help` for the full list. Every target accepts `TARGET=main|eval|test` (default `main`).

| Command | What it does |
| --- | --- |
| `make migrate` | Apply pending migrations |
| `make migrate-down` | Roll back the latest migration |
| `make migrate-reset` | Drop all tables and re-apply. **Destroys data**, asks for confirmation |
| `make migrate-new name="..."` | Create a new empty migration |
| `make migrate-history` | Show migration history |
| `make migrate-current` | Show the applied revision |

`migrations/env.py` refuses `eval`/`test` targets unless the URL points at a database ending in `_eval`/`_test`, so the main ledger can't be hit by mistake.

## Project layout

```
app.py            Streamlit entry point (home page)
pages/            Streamlit pages (capture, dashboard, ask)
ledger/        App logic: extraction, validation, normalisation, DB, SQL guard, ask
prompts/          Versioned LLM prompts (extraction, SQL, explain)
migrations/       Alembic migrations
tests/            pytest suite
data/             Sample images, eval data and cache
eval/results/     Evaluation outputs
docs/             PRD, architecture and task list
```

See `docs/ARCHITECTURE.md` and `docs/PRD.md` for design details.
