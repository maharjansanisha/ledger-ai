# Ledger AI — Product Requirements Document

| | |
|---|---|
| **Status** | Draft v0.1 — for review, not yet approved |
| **Owner** | San |
| **Submission deadline** | Friday, 9 October 2026 |
| **Build window** | Wed 30 Sep → Fri 9 Oct (1 weekend + ~7 weekday evenings ≈ 28–32 working hours) |
| **Next documents (after approval)** | ARCHITECTURE.md → TASKS.md → LEARNING.md → README.md |

---

## 1. Product title

**Ledger AI** — Intelligent Bookkeeping Assistant: a receipt-to-ledger assistant for Nepali small businesses.

## 2. One-sentence product definition

Ledger AI turns photos of Nepali purchase receipts and VAT bills into validated, human-confirmed ledger records stored in a local database, and answers questions about those records by running SQL against the database, never from the model's memory.

## 3. Problem statement

Small shop owners in Nepal get purchase bills in many formats: PAN/VAT invoices, thermal POS receipts, pre-printed cash memos filled in by hand, and handwritten slips. Most of these bills are never digitised. If they are, someone types them into Excel or accounting software by hand, which is slow, error-prone, and often skipped. So the owner can't easily answer basic questions like *"How much did I spend on stock last month?"* or *"How much VAT did I pay this quarter?"*

Current AI models can read receipt images, but their output can't be trusted as-is. They misread numbers, invent missing fields, and get arithmetic wrong. In financial records, a confident wrong number is worse than a blank field.

**The product problem is capture and trust together:** make receipt entry much faster than typing, while making every saved number verifiable.

## 4. Target users

- **Primary:** Owner-operator of a small retail business in Nepal (kirana, hardware, stationery, pharmacy counter) who receives supplier bills and wants to track purchases and expenses.
- **Secondary:** A freelance bookkeeper who digitises bills for several small shops.
- **Project audience (real for this submission):** Evaluators and interviewers who will judge whether the system works, is measured, and is understood by its builder.

> **Assumption:** These users come from domain knowledge, not user interviews. No user research will be done within this deadline.

## 5. User personas

**Ramesh, 45, kirana shop owner, Lalitpur**
- Gets 10–20 supplier bills a week, mostly thermal receipts and PAN bills from wholesalers.
- Keeps bills in a drawer and in a paper khata. Is comfortable with a smartphone but not with spreadsheets.
- Wants to know monthly stock spending and VAT paid without typing anything.
- Thinks in Bikram Sambat months (Asoj, Kartik), not Gregorian ones.

**Sita, 29, freelance bookkeeper, Kathmandu**
- Enters bills for 5 small clients into accounting software at month-end.
- Wants fast, accurate capture and will happily correct a few fields. She will not accept silently wrong totals.
- Cares about PAN, invoice number, VAT amount and duplicates, because those matter for VAT filing.

## 6. User journey

1. User opens the app, which runs locally in a browser.
2. User uploads a receipt photo (JPG/PNG).
3. System reads the image and pre-fills a structured form with merchant, date, amounts, line items and category.
4. System runs deterministic checks and highlights problems, e.g. *"Subtotal + VAT ≠ Total (diff NPR 45)"* or *"Date missing"*.
5. User compares the form with the image side by side, fixes any wrong fields, and clicks **Confirm & Save**.
6. The record is stored in the database (Neon Postgres) with the original AI output kept for audit.
7. User opens the **Dashboard** to see spending by category and month, and a list of saved receipts.
8. User types a question such as *"How much did I spend on inventory last month?"* The system shows the answer, the SQL it ran, and the result rows.

## 7. Core use case

> **As a shop owner, I photograph a supplier bill, check and correct the pre-filled record in under a minute, save it, and later ask how much I spent in a category or period, getting an answer computed from my saved records.**

Everything in the MVP serves this one loop: **capture → validate → confirm → store → query.**

## 8. MVP definition

The MVP is a **single-user, local Streamlit app** with a **reproducible evaluation script**.

### MUST HAVE (required for submission)

| ID | Feature | Why it's a must |
|---|---|---|
| M1 | Upload one receipt image (JPG/PNG) | Entry point |
| M2 | Structured extraction from image to a schema-valid record, using one multimodal LLM call | Core AI capability |
| M3 | Deterministic normalisation: amounts to numbers, date string to AD date plus BS date (converted by a library, not the LLM) | LLMs must not do calendar maths |
| M4 | Deterministic validation rules with flags (see §15) | Core trust mechanism |
| M5 | Human review screen: image beside editable fields and line items, with flags shown | Human-in-the-loop |
| M6 | Save confirmed records to the database (Neon Postgres), keeping both the AI output and the final values | Source of truth plus audit |
| M7 | Basic dashboard: total spend, spend by category, spend by month, receipts table | Makes stored data useful |
| M8 | Chat-style "Ask your ledger" page: question → read-only SQL → result, with the SQL and rows shown | Query over the ledger |
| M9 | SQL safety guard: read-only connection, a single SELECT only, allowed tables only | Non-negotiable safety |
| M10 | Labelled evaluation dataset of about 30 receipts, plus an eval script that prints per-field metrics | Evaluation is part of the product |
| M11 | NL-query eval: about 15 questions with known answers, plus 5 unsafe prompts | Measures the query feature |

### SHOULD HAVE (do only after all MUSTs work end-to-end)

| ID | Feature |
|---|---|
| S1 | Duplicate warning (same PAN + invoice number, or same merchant + date + total) |
| S2 | Short LLM-written explanation of the query result, with a check that every number it mentions appears in the result rows |
| S3 | BS-month queries, e.g. "How much did I spend in Bhadra?", using stored BS year/month columns |
| S4 | Correction-rate report: which fields humans edited most often |
| S5 | CSV export of the ledger |
| S6 | Basic image handling: EXIF auto-rotate, downscale large photos, HEIC rejection message |

### STRETCH (only if time remains; otherwise list under "future work")

| ID | Feature |
|---|---|
| X1 | Comparison experiment: OCR (PaddleOCR/EasyOCR) text → LLM, versus image → LLM directly, on ~10 dev receipts |
| X2 | Batch upload of several receipts |
| X3 | Second-model or second-pass verification for flagged receipts |
| X4 | Nepali-language (Devanagari) questions in the query box |
| X5 | Evaluation dashboard page inside the app |

## 9. Explicit non-goals / OUT OF SCOPE

These are **out of scope for this submission**. If an idea appears on this list, the answer during the build is *"not now"*.

**Product scope**
- ❌ A full accounting system: no double-entry, chart of accounts, balance sheet, P&L, or VAT return filing.
- ❌ Sales tracking or customer credit (udharo/khata) ledgers. **MVP covers purchase/expense bills only.**
- ❌ Multi-user, login, authentication, roles, or multi-business support.
- ❌ Mobile app or camera capture. Upload from file only; the photo can be taken on a phone.
- ❌ Cloud deployment, hosting or Docker. The app runs locally; only the database is hosted (Neon, Amendment A1).
- ❌ Currencies other than NPR.
- ❌ PDF invoices and multi-page documents.
- ❌ Integration with Tally, accounting software or IRD systems.

**Documents**
- ❌ Guaranteed support for fully handwritten khata pages or handwritten slips. A few are included in evaluation **only to measure and document the limitation**.
- ❌ Receipts with several bills in one photo.

**AI / engineering**
- ❌ Training or fine-tuning any model.
- ❌ RAG, vector databases or embeddings. The data is structured rows, so SQL is the correct retrieval tool. Similarity search returns the top-k *similar* records, not *all matching* records, so totals would be computed from an incomplete subset by the LLM.
- ❌ Agents, multi-step tool loops, or autonomous actions.
- ❌ LlamaIndex, LangChain, or any LLM orchestration framework.
- ❌ A separate backend API (FastAPI/Flask) or a separate frontend framework (React).
- ❌ Heavy OpenCV preprocessing (deskew, binarisation, denoising) in the MVP path.
- ❌ Latency or cost optimisation beyond "one call per receipt".
- ❌ LLM "confidence scores" as a trust signal. Validation flags are used instead (see §16).
- ❌ Chat memory or multi-turn conversation. Each question is answered on its own.
- ❌ Answering questions unrelated to the stored ledger (general chatbot behaviour).

## 10. Stretch goals

See the STRETCH table in §8. **X1 (OCR vs vision comparison)** is the most valuable stretch goal for learning and interviews. It should only be attempted after the MUST list is complete and the final evaluation has run.

## 11. Functional requirements

Each MUST feature has acceptance criteria (AC). A feature is done only when every AC passes.

### FR-1 Receipt upload (M1)
- AC1: User can upload a single JPG or PNG up to a reasonable size limit (e.g. 10 MB).
- AC2: The uploaded image is displayed on screen.
- AC3: Unsupported file types show a clear error and do not crash the app.
- AC4: The original image file is saved locally and linked to the record once saved.

### FR-2 Structured extraction (M2)
- AC1: One call to a multimodal model returns output that parses into the defined Pydantic schema.
- AC2: The schema includes: merchant_name, merchant_pan, invoice_number, date_raw, date_calendar_hint (BS/AD/unknown), line_items[] (description, quantity, unit_price, amount), subtotal, discount, service_charge, vat_amount, total, category, currency.
- AC3: Fields the model cannot read come back as `null`, not guessed. The prompt says this explicitly, and it is tested on at least one receipt with a missing field.
- AC4: If the output fails schema validation, the system retries once. If it fails again, the user sees an empty review form with the error message instead of a crash.
- AC5: The raw model response and the prompt version are stored with the record.
- AC6: Category is one of the fixed enum values: Inventory, Office Supplies, Transportation, Utilities, Rent, Equipment, Marketing, Food, Other.

### FR-3 Normalisation (M3)
- AC1: Amount strings like "1,250.00" and "Rs. 1250/-" become numeric values.
- AC2: date_raw is parsed into a date. If the date is BS, it is converted to AD using a deterministic library. If it is AD, the BS equivalent is computed the same way.
- AC3: Both AD date and BS date (plus BS year and BS month) are stored.
- AC4: An unparseable date results in `null` plus a validation flag, never a guessed date.
- AC5: The LLM is never asked to convert calendars or compute totals.

### FR-4 Validation (M4)
- AC1: Every rule in §15 runs on every extraction and again after human edits.
- AC2: Each failed rule produces a flag with severity (ERROR/WARNING) and a human-readable message that includes the numbers involved.
- AC3: Validation is plain Python with unit tests covering at least one pass and one fail case per rule.
- AC4: Record status is `clean` (no flags), `needs_review` (warnings only) or `invalid` (at least one error).

### FR-5 Human review and correction (M5)
- AC1: The image and the editable form are visible side by side.
- AC2: The user can edit every extracted field, and can add, edit or delete line items.
- AC3: Flags are shown next to the fields they relate to, or at the top of the form.
- AC4: Editing a field and re-validating updates the flags.
- AC5: Nothing is saved without an explicit **Confirm & Save** click.
- AC6: If ERROR flags remain, saving requires an explicit "Save anyway" acknowledgement, and the record is stored with `user_override = true`.

### FR-6 Persistence (M6)
- AC1: Confirmed records are written to the database (Neon Postgres): a receipt header plus its line items.
- AC2: Both the original AI extraction (JSON) and the final human-confirmed values are stored.
- AC3: Which fields were edited by the human is recorded or derivable.
- AC4: Saved records survive an app restart.
- AC5: Money values are stored without floating-point drift. The exact method is decided in ARCHITECTURE.md.

### FR-7 Dashboard (M7)
- AC1: Shows total spend for a selectable period.
- AC2: Shows spend by category (chart or table).
- AC3: Shows spend by month.
- AC4: Shows a table of saved receipts (date, merchant, total, category, status).
- AC5: Every number on the dashboard comes from a SQL/Pandas aggregation over the database, not from an LLM.

### FR-8 Natural-language query (M8, M9)
- AC1: User types a question. The system generates one SQL SELECT statement from the schema description, today's date and a few examples.
- AC2: The SQL passes the safety guard (§17) before execution. Unsafe SQL is rejected with a message and is never executed.
- AC3: The SQL runs on a **read-only** database connection.
- AC4: The UI shows the question, the generated SQL, the date range it assumed (if relevant) and the result rows.
- AC5: The displayed answer numbers come only from the result rows.
- AC6: Questions outside the ledger's scope get a fixed reply such as *"I can only answer questions about your saved receipts."*
- AC7: SQL errors are caught and shown. The app does not crash.

### FR-9 Evaluation harness (M10, M11)
- AC1: One command runs extraction on every image in the test set and compares results to ground-truth JSON.
- AC2: Outputs a per-receipt, per-field results file (CSV) and a summary table of the metrics in §18.
- AC3: One command runs the NL-query question set and reports execution accuracy and unsafe-query rejection.
- AC4: Each run records the model name, prompt version and date, so results are reproducible and comparable.

## 12. AI/ML requirements

| Component | What the AI does | What the AI must NOT do |
|---|---|---|
| Receipt extraction | Read the image and fill the schema fields; pick one category from the enum | Compute or "fix" totals; convert BS↔AD; guess unreadable fields |
| NL → SQL | Translate a question into one SELECT over a known schema | Write data; produce an answer number itself; answer off-topic questions |
| Result explanation (SHOULD) | Rephrase result rows in one or two sentences | Introduce any number not present in the rows |

Requirements:
- **R-AI-1** One vision-capable LLM via API. No local model training. Model choice is a decision in §27.
- **R-AI-2** Use the provider's structured-output / tool-calling mode where available, validated by Pydantic on our side regardless.
- **R-AI-3** Temperature 0 (or the provider's most deterministic setting) for extraction and SQL generation.
- **R-AI-4** Prompts live in version-controlled files with a version label. Evaluation results cite the prompt version.
- **R-AI-5** Prompt iteration uses **only the dev split**. The test split is run for a baseline and a final measurement, not tuned against.
- **R-AI-6** No confidence scores from the LLM are used to decide trust.

## 13. Data requirements

### Evaluation dataset (the minimum that is still meaningful)

**Size: ~30 real receipts**, split into **8 dev** (for prompt iteration) and **22 test** (held out).
Why not 50: each receipt takes roughly 5–8 minutes to label by hand, including line items. 30 receipts is about 3–4 hours of labelling, which fits the deadline. 50 would take a whole weekend day.

| Slice | Target count | Purpose |
|---|---|---|
| Printed PAN/VAT invoices (wholesalers, shops) | ~10 | Main use case; has PAN, invoice no., VAT |
| Thermal POS receipts (supermarkets, pharmacies, restaurants) | ~8 | Common, faded, long item lists; restaurant bills test service charge |
| Pre-printed cash memo, filled by hand | ~5 | Mixed printed/handwritten |
| Fully handwritten slips | ~3 | **Limitation slice**, reported separately and not expected to pass |
| Poor capture (angled, crumpled, shadowed, blurry) | ~3 | Robustness |
| Deliberate duplicate (same bill, re-photographed) | 1–2 | Tests S1 if built |

Also aim for: a mix of BS-dated and AD-dated receipts, at least 3 with Devanagari text, at least 3 with a discount, and at least 2 with a missing field (no invoice number, no date).

### Ground-truth labelling
- One JSON file per receipt, in the **same schema** as the extraction output, written by hand from the physical receipt.
- Label what is **printed on the receipt**, not what "should" be there. If the receipt's own arithmetic is wrong, label the printed values and add a note.
- Record the slice and any notes (e.g. "total partly torn") per receipt.
- Category ground truth is your judgement as the shop owner. Write a one-line rule for each category so labels are consistent.

### Privacy
- Use your own or consenting shops' receipts. Mask personal phone numbers or customer names before committing images to GitHub, or keep images out of the public repo.
- Images are sent to a third-party model API. That is acceptable for this project and should be stated in the README.

### Seed data for query evaluation
- A fixed evaluation database (Neon `ledger_eval`) built from the confirmed test-set records (plus a few hand-entered records if needed to cover several months and categories), so query answers are known and stable.

## 14. Database requirements

Conceptual level only. Exact tables and types are decided in ARCHITECTURE.md.

- **DB-1** Neon serverless PostgreSQL (free tier), per ARCHITECTURE Amendment A1 (2026-10-03). Previously: SQLite.
- **DB-2** Receipt header entity: id, merchant_name, merchant_pan, invoice_number, date_ad, date_bs, bs_year, bs_month, subtotal, discount, service_charge, vat_amount, total, category, status, user_override, image_path, created_at.
- **DB-3** Line-item entity linked to a receipt: description, quantity, unit_price, amount.
- **DB-4** Audit data: raw AI extraction JSON, model name, prompt version, validation flags at extraction time, list of human-edited fields.
- **DB-5** Money stored without float rounding errors (e.g. integer paisa or fixed decimal), decided in architecture.
- **DB-6** The schema is simple enough to describe fully in the NL→SQL prompt (target: ≤ 2 queryable tables).
- **DB-7** The NL-query feature uses a separate read-only database role (`ledger_reader`) with read-only transactions.

## 15. Validation requirements

All rules are deterministic Python. Default tolerance is ±NPR 1 to allow for rounding on receipts (this is a decision in §27).

| ID | Rule | Severity |
|---|---|---|
| V1 | merchant_name, date and total are present | ERROR |
| V2 | total > 0; no negative amounts | ERROR |
| V3 | For each line item with qty and unit_price: qty × unit_price ≈ amount | WARNING |
| V4 | Sum of line-item amounts ≈ subtotal (if both exist) | WARNING |
| V5 | subtotal − discount + service_charge + vat_amount ≈ total | ERROR |
| V6 | If vat_amount present: vat_amount ≈ 13% of taxable amount (subtotal − discount + service_charge), within ±1% or ±NPR 2 | WARNING |
| V7 | merchant_pan, if present, is exactly 9 digits | WARNING |
| V8 | Date parses and converts; not in the future; not implausibly old (e.g. before 2075 BS / 2018 AD) | ERROR if unparseable, WARNING if implausible |
| V9 | category is in the enum | ERROR |
| V10 | Possible duplicate of an existing record (SHOULD, S1) | WARNING |
| V11 | No subtotal printed: sum of line items − discount + service_charge (+ vat_amount, if present) ≈ total (added 2026-10-08) | WARNING |

Notes:
- VAT-inclusive receipts (no separate VAT line) must pass V5 when vat_amount is null. V6 is skipped.
- V11 runs only when subtotal is missing (so V4/V5 cannot run). Line prices may or may not include VAT, so V11 passes if either reading adds up: with vat_amount added on top, or with VAT already in the line prices.
- Validation **flags** problems and never auto-corrects values. Correction is the human's job.

## 16. Human-in-the-loop requirements

- **H-1** Every record is reviewed by a human before saving in the MVP, including `clean` ones. Auto-save of clean records is out of scope.
- **H-2** Flags point the reviewer's attention to likely problems. The goal is fast review, not blind trust.
- **H-3** The system keeps the AI's original values alongside the human's final values, so correction rate per field can be measured (S4).
- **H-4** Trust indicators come from **validation results**, not from model self-reported confidence. Reason: LLM confidence scores are poorly calibrated, while a failed arithmetic check is objective evidence.
- **H-5** Target review experience: a clean, printed receipt can be reviewed and saved in under ~60 seconds. Measure this informally during the demo rehearsal; it is not a formal metric.

## 17. Natural-language query requirements

**Supported question types (MVP):**
- Total spend for a period: *"How much did I spend last month?"*
- Spend by category and period: *"How much on inventory in September?"*
- Spend by merchant: *"How much have I paid Shree Traders?"*
- Counts: *"How many bills did I save this week?"*
- Top-N: *"Top 3 merchants by spend"*
- Extremes: *"What was my largest bill?"*
- VAT: *"How much VAT did I pay last month?"*

**Pipeline:** question → LLM (given schema, today's date, few-shot examples) → SQL → safety guard → read-only execution → result rows → display (+ optional explanation, S2).

**Safety guard (MUST):**
- Exactly one statement, and it must start with SELECT (or WITH … SELECT).
- Reject any of: INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, PRAGMA, REPLACE, or multiple statements.
- Only the allowed tables and columns may be referenced.
- A row LIMIT is added if missing.
- Execution happens on a read-only connection, so even a bypass cannot modify data (defence in depth).

**Interpretation rules:**
- "Last month" means the previous calendar month in AD, relative to today. The assumed range is displayed.
- BS-month questions are SHOULD (S3).
- If a question is ambiguous or out of scope, the system says so instead of guessing.

**Hallucination rule:** No number in the answer may come from anywhere except the SQL result. An empty result is shown as "no matching records", never as 0 unless the SQL returned 0.

## 18. Evaluation requirements

### 18.1 Extraction metrics (computed on the 22-receipt test split, reported per slice as well as overall)

| Field | Match rule | Metric |
|---|---|---|
| merchant_name | Case/space/punctuation-normalised; fuzzy similarity ≥ 0.85 counts, borderline cases judged manually and noted | Accuracy |
| merchant_pan | Exact match on digits | Accuracy (on receipts that have a PAN) |
| invoice_number | Exact after removing spaces | Accuracy (on receipts that have one) |
| date (AD, after normalisation) | Exact date match | Accuracy |
| subtotal / vat_amount / total | Absolute difference ≤ NPR 1 | Accuracy per field |
| line items | Match predicted to gold items by amount + fuzzy description | Item precision, item recall, and % receipts with correct item count |
| category | Exact enum match | Accuracy + confusion table |
| **Core record accuracy** | merchant + date + total **all** correct | % of receipts |
| **Full record accuracy** | All header fields correct (line items excluded) | % of receipts |

**Hallucination and omission** (counted across all header fields):
- **Hallucination:** gold is null, prediction is non-null. Count and rate.
- **Omission:** gold is non-null, prediction is null.
- **Wrong value:** both non-null and not matching.

These three are reported separately because they have different causes and different fixes.

### 18.2 Validation effectiveness (does the safety net work?)
- **Catch rate:** of receipts with at least one wrong core field (merchant/date/total), the % that raised at least one flag.
- **Flag precision:** of receipts that raised a flag, the % that actually had an error. (False alarms waste reviewer time.)
- **Review burden:** % of receipts with zero flags.

### 18.3 NL-query metrics
- **Execution accuracy:** of ~15 questions with hand-computed gold answers, the % where the returned result equals the gold answer.
- **Unsafe-query rejection:** of 5 adversarial prompts (e.g. "delete all receipts", "drop the table", "update total to 0"), **100% must be rejected or have no effect.** This is a pass/fail gate.
- **Out-of-scope handling:** of 3 off-topic questions, the % that received the refusal message.

### 18.4 Protocol
1. Label all 30 receipts before tuning prompts.
2. Iterate prompts on the **8 dev receipts only**.
3. Run the test split once for the **baseline** and once for the **final** measurement. Report both numbers honestly.
4. No accuracy targets are set in advance. Report what you measure. With 22 receipts, one receipt is about 4.5 percentage points, so say this when presenting results.

### 18.5 Error-analysis process
1. For every field mismatch in the results CSV, add a row: receipt id, slice, field, predicted, gold, **error category**.
2. Error categories: `misread` (visual reading error), `hallucinated`, `omitted`, `format/normalisation` (e.g. date format, comma parsing), `calendar_conversion`, `wrong_category`, `receipt_ambiguous` (even a human is unsure), `schema_failure`.
3. Tally errors by category and slice.
4. Choose the **top one or two categories**, make one targeted change (prompt wording, a normalisation fix, a schema tweak), and re-run on dev.
5. Write down what you changed, why, and what happened, including changes that didn't help.
6. Final test run. Summarise which errors remain and which slices fail (expected: handwritten).

## 19. Success criteria

The submission succeeds if:
1. **End-to-end loop works live:** upload → extract → flags → correct → save → dashboard updates → question answered from the DB.
2. **Evaluation is real:** metrics in §18 are computed by a script on ≥ 20 held-out receipts, with baseline and final numbers and an error-analysis summary.
3. **Safety holds:** 0 unsafe SQL statements executed; 100% of adversarial prompts rejected.
4. **No unreviewed data:** every saved record went through the human confirm step.
5. **No invented numbers:** every number shown in the dashboard and query answers is traceable to the database.
6. **Explainability:** you can explain, without notes, why each component exists, what the LLM is and isn't trusted with, and what the main failure modes are.

## 20. Constraints

- **Time:** ≈ 28–32 hours total: one weekend (Sat 3 & Sun 4 Oct) plus weekday evenings. Plan to build in ~24 hours and keep the rest as buffer.
- **People:** solo developer with ~2 months of AI/ML experience.
- **Runtime:** local laptop, Python, browser UI.
- **Budget: zero.** Free tiers and open-source tools only. Free-tier rate limits apply, so API responses must be cached to disk so that eval re-runs and UI reloads don't use up quota.
- **Free-tier privacy:** some free API tiers allow the provider to use submitted content to improve their products. Receipts must be masked of personal data (customer names, phone numbers) before upload.
- **Stack limits:** Python, Pydantic, Neon Postgres (psycopg), Pandas, Streamlit, one LLM provider SDK, one BS↔AD date library. Any other dependency needs a stated reason.

## 21. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Collecting and labelling receipts takes longer than planned | High | High | Start collecting **today**; label on weekday evenings **before** the weekend; cap at 30 |
| Extraction quality poor on thermal/Devanagari receipts | Medium | Medium | Measure it and report it by slice. Human review is the fallback. This is a finding, not a failure |
| Streamlit review form with editable line items gets fiddly | Medium | High | Use a built-in editable table component; keep layout plain; no custom styling |
| NL→SQL produces wrong but valid SQL (e.g. wrong date range) | High | Medium | Show SQL and assumed range; few-shot examples; small schema; measured in eval |
| BS↔AD conversion or ambiguous date formats (DD/MM vs MM/DD) | Medium | Medium | Use a library; store date_raw; flag instead of guessing |
| Scope creep (OCR comparison, batch upload, pretty UI) | High | High | §9 out-of-scope list; no SHOULD work until all MUSTs pass end-to-end |
| API key, rate limit or provider outage on demo day | Low | High | Pre-save demo records; record a backup screen video |
| Receipt arithmetic itself inconsistent (rounding, hidden charges) | Medium | Low | Tolerance; service_charge/discount fields; label notes |

## 22. Assumptions

- Receipts are **purchase/expense** bills received by the business.
- One receipt per image, one page.
- Currency is NPR. VAT rate is 13%.
- The user can read the receipt and correct fields.
- A vision-capable LLM API is available and affordable for ~100–200 calls during development.
- A Python library for BS↔AD conversion exists and is accurate for the date range in the dataset (verify on day one).
- Receipt category is assigned at **receipt level**, not per line item.

## 23. Expected limitations (to state openly in the demo)

- Handwritten slips and khata pages will often be wrong or incomplete.
- Accuracy numbers come from a small sample (~22 receipts), so they have wide uncertainty.
- Category is one label per receipt; mixed-purpose bills are simplified.
- NL queries cover simple aggregations; complex comparative or multi-step questions may fail.
- Nepali-language questions are not supported in the MVP.
- Receipt images leave the device (sent to an API provider).
- Single user, single business, local only.

## 24. Demo scenario (~5 minutes)

1. **Setup (pre-loaded):** the DB already has ~20 confirmed receipts spanning at least two months.
2. **Clean capture:** upload a printed PAN bill → fields pre-filled → no flags → confirm → dashboard total updates.
3. **Trust moment:** upload a thermal receipt where the model misreads a number → validation shows *"Subtotal + VAT ≠ Total (diff NPR 90)"* → fix the field → flag clears → save. *Point: the system doesn't trust the model blindly.*
4. **Query:** *"How much did I spend on inventory last month?"* → show the SQL, the assumed date range, the result, and cross-check it against the dashboard.
5. **Safety:** type *"delete all my receipts"* → rejected.
6. **Honesty:** show the evaluation summary: per-field metrics, validation catch rate, and one handwritten failure with its error category.
7. **Close:** two sentences on what you'd do next (e.g. OCR-vs-vision comparison, BS-month queries).

## 25. Definition of Done

- [ ] All MUST features (M1–M11) meet their acceptance criteria.
- [ ] Validation rules have passing unit tests.
- [ ] Evaluation script runs from one command and reproduces the reported numbers.
- [ ] Baseline and final extraction metrics are reported, with error analysis.
- [ ] NL-query eval run; 100% adversarial rejection.
- [ ] Demo scenario rehearsed end-to-end at least once from a fresh app start.
- [ ] Repo on GitHub with setup instructions; no API keys or unmasked personal data committed.
- [ ] Known limitations written down.
- [ ] Out-of-scope list respected, or any exception written down with its reason.

---

## 26. Major components the architecture will need (list only; design comes in ARCHITECTURE.md)

1. **UI layer:** upload, review/correct, dashboard, query pages.
2. **Image intake:** file checks, basic resize/rotate.
3. **Extraction client:** prompt + multimodal LLM call + structured output.
4. **Schema layer:** Pydantic models for extraction output and saved records.
5. **Normaliser:** amounts, dates, BS↔AD conversion.
6. **Validator:** deterministic rules → flags → status.
7. **Persistence layer:** Postgres schema (Neon), write path, read-only query path.
8. **Dashboard aggregations:** SQL/Pandas summaries.
9. **NL→SQL module:** prompt, SQL generation, safety guard, executor.
10. **Evaluation harness:** dataset loader, field comparators, metric reports, error-analysis output.
11. **Configuration:** model name, prompt versions, tolerances, API key via environment variable.

## 27. Open decisions (approve before architecture)

See the conversation summary. Decisions are recorded here once approved.

| # | Decision | Recommended default | Approved? |
|---|---|---|---|
| D1 | Scope: purchase/expense bills only (no sales / udharo) | Yes | ☐ |
| D2 | Extraction approach: image → vision LLM directly; OCR only as stretch X1 | Yes | ☐ |
| D3 | LLM provider/model | Gemini Flash (free tier) for extraction + SQL; GitHub Models as backup | ☐ |
| D4 | UI: Streamlit | Yes | ☐ |
| D5 | Dataset: ~30 receipts, 8 dev / 22 test, handwritten as limitation slice | Yes | ☐ |
| D6 | Tolerance ±NPR 1; VAT 13% check as warning | Yes | ☐ |
| D7 | Query answer = SQL result rows; LLM explanation only as SHOULD | Yes | ☐ |
| D8 | "Last month" = previous AD calendar month; BS months = SHOULD | Yes | ☐ |
| D9 | Category at receipt level, not per line item | Yes | ☐ |
| D10 | No LlamaIndex / LangChain / RAG / agents | Yes | ☐ |
| D11 | Query method: free-form text-to-SQL with guard, vs LLM choosing from predefined query functions | Text-to-SQL; fall back to predefined functions if eval accuracy is poor | ☐ |
| D12 | Chat UI: single-turn questions in a chat layout (no conversation memory) | Yes | ☐ |
