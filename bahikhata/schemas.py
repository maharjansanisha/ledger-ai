"""Pydantic models shared by every component (ARCHITECTURE.md §9, TASK-006).

Design rule: lenient at the input boundary, strict at the output boundary.
- ReceiptExtraction (from the LLM): every field optional, amounts and dates as
  the text that was printed (decision D3). One bad field never discards the rest.
- ReceiptDraft (review form): typed but still nullable, and still allows values
  the validator must flag (e.g. negative amounts for V2), so a bad receipt is
  representable and can be reviewed.
- ConfirmedReceipt (the only input to the save function): strict, and at least
  as strict as the CHECK constraints in migrations/versions/0001_create_ledger_tables.py.

Money is always integer paisa (1 NPR = 100 paisa) in fields ending in `_paisa`.
Paisa fields are strict ints: floats such as 12.5 or 12.0 are rejected.

Shape and single-field checks live here; rules across fields (V3-V6 arithmetic,
V8 date plausibility, V10 duplicates) live in validate.py.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Category(StrEnum):
    """Receipt-level category. Values must match the receipts.category CHECK."""

    INVENTORY = "Inventory"
    OFFICE_SUPPLIES = "Office Supplies"
    TRANSPORTATION = "Transportation"
    UTILITIES = "Utilities"
    RENT = "Rent"
    EQUIPMENT = "Equipment"
    MARKETING = "Marketing"
    FOOD = "Food"
    OTHER = "Other"


ReceiptStatus = Literal["clean", "needs_review", "invalid"]
CalendarHint = Literal["BS", "AD", "unknown"]

# Integer paisa, never a float. Draft amounts may be negative so V2 can flag them;
# confirmed amounts may not (DB CHECK >= 0, and total_paisa > 0).
Paisa = Annotated[int, Field(strict=True)]
NonNegativePaisa = Annotated[int, Field(strict=True, ge=0)]
PositivePaisa = Annotated[int, Field(strict=True, gt=0)]

# receipts.merchant_name has CHECK (length(btrim(merchant_name)) > 0).
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# --- From the LLM (lenient) --------------------------------------------------

class ExtractedLineItem(BaseModel):
    description: str | None = None
    quantity_raw: str | None = None
    unit_price_raw: str | None = None
    amount_raw: str | None = None


class ReceiptExtraction(BaseModel):
    """Sent to Gemini as the response schema. `null` is always a valid answer."""

    merchant_name: str | None = None
    merchant_pan: str | None = None          # transcribed; format checked by V7
    invoice_number: str | None = None
    date_raw: str | None = None              # as printed; never converted by the LLM
    date_calendar_hint: CalendarHint | None = None  # advisory only (year-range rule decides)
    line_items: list[ExtractedLineItem] = Field(default_factory=list)
    subtotal_raw: str | None = None          # amounts are text; normalize.py parses them
    discount_raw: str | None = None
    service_charge_raw: str | None = None
    vat_amount_raw: str | None = None
    total_raw: str | None = None
    category: str | None = None              # checked against Category by V9
    currency_raw: str | None = None          # audit only; MVP assumes NPR


# --- Validation output -------------------------------------------------------

class ValidationFlag(BaseModel):
    rule_id: Annotated[str, StringConstraints(pattern=r"^(V([1-9]|10)|N[1-3]|EXTRACTION_FAILED)$")]
    severity: Literal["BLOCKING", "ERROR", "WARNING"]
    field: str | None = None
    message: str                             # includes the numbers involved


# --- Review form (typed, still nullable) -------------------------------------

class DraftLineItem(BaseModel):
    """No line_no: it is derived from list order at save time."""

    description: str | None = None
    quantity: Decimal | None = None          # not money (e.g. 1.5 kg)
    unit_price_paisa: Paisa | None = None
    amount_paisa: Paisa | None = None


class ReceiptDraft(BaseModel):
    merchant_name: str | None = None
    merchant_pan: str | None = None
    invoice_number: str | None = None
    date_raw: str | None = None
    date_calendar_hint: CalendarHint | None = None
    date_ad: date | None = None
    date_bs: str | None = None               # 'YYYY-MM-DD' in BS
    bs_year: int | None = None
    bs_month: int | None = None
    subtotal_paisa: Paisa | None = None
    discount_paisa: Paisa | None = None
    service_charge_paisa: Paisa | None = None
    vat_paisa: Paisa | None = None
    total_paisa: Paisa | None = None
    category: Category | None = None
    line_items: list[DraftLineItem] = Field(default_factory=list)


# --- Strict output: the only thing the save function accepts -----------------

class ConfirmedLineItem(BaseModel):
    """Maps 1:1 to line_items columns, except id, receipt_id and line_no (set at save)."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: Decimal | None = None
    unit_price_paisa: NonNegativePaisa | None = None
    amount_paisa: NonNegativePaisa | None = None


class ConfirmedReceipt(BaseModel):
    """Maps 1:1 to receipts columns, except id and created_at (DB-generated) and
    image_path (passed separately to the save function)."""

    model_config = ConfigDict(extra="forbid")

    merchant_name: NonBlankStr
    merchant_pan: str | None = None
    invoice_number: str | None = None
    date_ad: date
    date_bs: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    bs_year: int
    bs_month: Annotated[int, Field(ge=1, le=12)]   # 1 = Baisakh
    subtotal_paisa: NonNegativePaisa | None = None
    discount_paisa: NonNegativePaisa | None = None
    service_charge_paisa: NonNegativePaisa | None = None
    vat_paisa: NonNegativePaisa | None = None      # None for VAT-inclusive bills
    total_paisa: PositivePaisa
    category: Category
    status: ReceiptStatus
    user_override: bool = False
    line_items: list[ConfirmedLineItem] = Field(default_factory=list)


# --- Evaluation labels -------------------------------------------------------

# Labels write amounts as NPR decimal strings ("1250.50"); the eval converts them.
NprText = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d+(\.\d{1,2})?$")]


class GroundTruthLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: Decimal | None = None
    unit_price: NprText | None = None
    amount: NprText | None = None


class GroundTruthReceipt(BaseModel):
    """One hand-written label file per receipt. extra="forbid" so typos in keys fail loudly."""

    model_config = ConfigDict(extra="forbid")

    id: str                                  # e.g. "r001"
    split: Literal["dev", "test"]
    slice: str                               # PRD §13 slice name
    notes: str | None = None
    merchant_name: str | None = None
    merchant_pan: str | None = None
    invoice_number: str | None = None
    date_printed: str | None = None          # as printed on the receipt
    calendar: Literal["BS", "AD"] | None = None
    subtotal: NprText | None = None
    discount: NprText | None = None
    service_charge: NprText | None = None
    vat: NprText | None = None
    total: NprText | None = None
    category: Category | None = None
    line_items: list[GroundTruthLineItem] = Field(default_factory=list)


# --- Ask Your Ledger ---------------------------------------------------------

class QueryPlan(BaseModel):
    """From the LLM. Python precomputes date ranges; the LLM only copies them."""

    status: Literal["ok", "out_of_scope", "ambiguous"]
    sql: str | None = None
    date_range_start: str | None = None
    date_range_end: str | None = None
    clarification: str | None = None


class Source(BaseModel):
    """A document chunk an answer cited (hybrid Ask Your Ledger)."""

    doc_name: str
    chunk_id: str
    score: float
    snippet: str


class QueryResult(BaseModel):
    """To the UI and eval. Money in formatted_rows is formatted by code, never the LLM."""

    question: str
    plan: QueryPlan | None = None            # None if the question failed the input check
    sql_executed: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[tuple[Any, ...]] = Field(default_factory=list)
    formatted_rows: list[tuple[Any, ...]] = Field(default_factory=list)
    message: str | None = None               # refusal / error / "No matching records."
    explanation: str | None = None           # S2, only if it passed the number check
    route: Literal["sql", "docs", "both"] | None = None  # hybrid strategy only
    answer: str | None = None                # from documents (and rows); only if it passed the number check
    sources: list[Source] = Field(default_factory=list)
    prompt_version: str
    model_name: str
