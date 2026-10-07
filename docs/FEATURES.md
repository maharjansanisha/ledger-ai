# Bahikhata AI — Features & GenAI Roadmap

As of 2026-10-07.

## Overview

Bahikhata AI turns photos of purchase receipts and VAT bills into a checked ledger for small Nepali businesses, then lets the owner query it in plain English. The core loop is capture, extract, validate, human confirm, store, query. Scope today is purchase and expense bills only, one user, one business, NPR, 13% VAT.

| Layer | Choice |
| --- | --- |
| App and UI | Python 3.12 + Streamlit (3 pages), managed with uv |
| Database | Neon serverless Postgres via psycopg v3, raw parameterised SQL, Alembic migrations |
| LLM | Google Gemini (`gemini-3.5-flash-lite` default) via `google-genai`, temperature 0, JSON output with Pydantic schemas |
| Key libraries | pydantic, pandas, nepali-datetime, pillow, sqlglot, pytest (~250 tests) |
| Auth | None; access control is a read-only `ledger_reader` DB role |
| Deployment | Local only (`streamlit run app.py`); only the database is hosted |

## Existing features

The app ships 3 pages and 3 LLM uses (receipt extraction, question to SQL, result explanation); everything else is deterministic code.

| Area | Feature | Where |
| --- | --- | --- |
| Capture | Upload one JPG/PNG up to 10 MB; type sniffed from content; HEIC and corrupt files rejected with friendly messages | `pages/capture_and_review/`, `bahikhata/images.py` |
| Capture | Image prep: EXIF rotation, transparency flattened, long edge capped at 2000 px, re-encoded as JPEG; nothing sent until "Extract" | `bahikhata/images.py` |
| AI extraction | One multimodal Gemini call fills a structured schema: merchant, PAN, invoice no., printed date + calendar hint, line items, subtotal, discount, service charge, VAT, total, category | `bahikhata/extraction.py`, `prompts/extraction_v1.txt` |
| AI extraction | Schema-failure retry with the parse error; "Try again" or "Enter by hand" fallback | `bahikhata/extraction.py` |
| Normalisation | Amounts to integer paisa (Rs/NPR/रू, lakh grouping, Devanagari digits); quantities like "1.5 kg" | `bahikhata/normalize.py` |
| Normalisation | Dates to AD + Bikram Sambat (year, month) with year-range calendar rule and ambiguity flags | `bahikhata/normalize.py` |
| Validation | Rules V1–V9: required fields, non-negative amounts, line maths, subtotal and total reconciliation, ~13% VAT, 9-digit PAN, date sanity, allowed category; status clean / needs_review / invalid | `bahikhata/validate.py` |
| Human review | Editable form and line-item table, inline flags, Re-validate, Confirm & Save blocked by blocking flags, explicit "save anyway" override | `pages/capture_and_review/views.py`, `bahikhata/review.py` |
| Persistence and audit | One transaction across `receipts`, `line_items`, `receipt_audit` (raw model output, AI draft, flags, human-edited fields, model and prompt version, image hash) | `bahikhata/db.py`, migration 0001 |
| Dashboard | Landing page: hero banner, KPI tiles, period and status filters, receipts table; fixed SQL, no LLM | `pages/dashboard/` |
| Edit | Select a receipt on the Dashboard and Edit it in Capture & Review (no LLM call); saving updates it and returns to the Dashboard | `pages/capture_and_review/`, `bahikhata/review.py` |
| Ask Your Ledger | Plain-English question to guarded read-only SQL; out-of-scope and clarification replies; results table, assumed date range, collapsible SQL | `pages/ask_your_ledger/`, `bahikhata/ask.py` |
| Ask Your Ledger | SQL guard: single SELECT, whitelisted tables, banned functions, money alias rule, LIMIT 200; read-only role, 5 s timeout | `bahikhata/sql_guard.py`, `bahikhata/db.py` |
| Ask Your Ledger | Optional 1–2 sentence explanation, dropped if it cites any number not in the results | `bahikhata/ask.py`, `prompts/explain_v1.txt` |
| LLM platform | Disk cache keyed by model + prompt version + input, 1 retry on 5xx (never on 429), typed user-safe errors, versioned prompts, secret redaction in logs | `bahikhata/llm_client.py` |
| Localisation | BS calendar support, Devanagari digit and currency parsing; UI is English only | `bahikhata/normalize.py` |

**Built but not wired up or unfinished:** `list_receipts` and `dashboard_by_month` unused, V10 duplicate detection (parameter unused), "functions" query strategy (raises `NotImplementedError`), and the evaluation harness (schema exists, no labelled data or runner).

**Not present:** auth, export, delete of saved records, sales or credit (udharo) ledgers, multi-business.

## Generative AI feature suggestions

Start with the evaluation dataset, follow-up questions and learning from corrections: each builds on code that already exists and keeps the current pattern where the model proposes and code or a human verifies. Effort: S is days, M is 1–2 weeks, L is longer. Ratings are estimates, not measured.

| # | Feature | What it does | Builds on | Effort | Impact |
| --- | --- | --- | --- | --- | --- |
| 1 | Synthetic receipts + LLM-assisted labelling for evals | Generate varied test receipts (layouts, BS/AD dates, Devanagari) and pre-label real ones for human checking, to unblock the empty eval harness | `GroundTruthReceipt`, TASK-027–039 | M | High |
| 2 | Follow-up questions in Ask Your Ledger | Rewrite "and last month?" into a standalone question using the previous turn, then run the existing guarded SQL path | `ask.py`, session history | S | High |
| 3 | Learn from corrections | Feed the user's own past edits for the same merchant (from `receipt_audit`) as few-shot examples into extraction | `diff_fields`, audit table | M | High |
| 4 | Second-pass verification | For flagged receipts only, a second vision call re-reads just the disputed fields and proposes fixes, shown as suggestions | V3–V6 flags, TASK-062 | S | Medium |
| 5 | Monthly spending narrative | A short AI-written month summary on the dashboard, with every number checked against SQL results | `check_explanation_numbers`, dashboard queries | S | Medium |
| 6 | Price-change and anomaly alerts | Code detects unit-price jumps per item and supplier or unusual spend; the LLM writes a one-line, number-checked explanation | `line_items`, explain prompt | M | High |
| 7 | Nepali and voice questions | Ask in Nepali or Romanised Nepali, typed or spoken; Gemini transcribes and translates, SQL path unchanged; BS month names like "Bhadra" | TASK-052, TASK-063 | M | High |
| 8 | Line-item categorisation and product normalisation | Map "chamal 25kg" and "rice (sona mansuli)" to one product and category for item-level reporting | extraction schema, `line_items` | M | Medium |
| 9 | Image quality pre-check | A cheap vision call flags blurry, cropped or non-receipt images before full extraction, with retake guidance | `images.py` | S | Medium |
| 10 | Batch and PDF or multi-page bills | Extract several receipts or a multi-page PDF in one go, each into its own review card | TASK-061 | L | Medium |
| 11 | Auto-charts in answers | The model proposes a chart type for an Ask result (bar, line); code validates columns and renders it | Ask results table | S | Low |
| 12 | Suggested questions | Generate 3–4 relevant starter questions from the user's actual categories and date range | Ask tips box | S | Low |
| 13 | Prompt-injection screening | Flag receipt text that looks like instructions to the model before extraction output is shown | ARCHITECTURE §15 | S | Medium |
| 14 | VAT purchase-register draft | Assemble a period's VAT purchase summary from confirmed receipts for the bookkeeper to check | `receipts` VAT and PAN fields | M | High |

Guardrails to keep for every item: temperature 0 with schema output, numbers computed in code not by the model, human confirmation before anything is saved, and the read-only role for anything that queries the ledger.

## Prioritisation

|  | Small effort (days) | Medium or large effort (weeks) |
| --- | --- | --- |
| **High impact** | **Quick wins:** 2 Follow-up questions | **Big bets:** 1 Synthetic eval data, 3 Learn from corrections, 6 Price-change alerts, 7 Nepali and voice questions, 14 VAT purchase-register draft |
| **Medium or low impact** | **Fill-ins:** 4 Second-pass verification, 5 Monthly narrative, 9 Image quality pre-check, 13 Injection screening, 11 Auto-charts, 12 Suggested questions | **Later:** 8 Line-item categorisation, 10 Batch and PDF bills |

Suggested order: item 2 now, item 1 alongside it so every later feature can be measured, then items 3 and 6.

## Review notes

- Item 14 and any user-facing tax or privacy wording need compliance and legal review before external use.
- Items 1 and 7 send more data to Google's API; review against the free-tier data-use terms.
- No accuracy claims are supported yet, because the evaluation harness has not been run.
