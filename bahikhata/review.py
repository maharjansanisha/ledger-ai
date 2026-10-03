"""Review-and-save logic behind the Capture & Review page (A§3.8, A§11, TASK-017/018).

No Streamlit here: the page only collects widget values and calls these functions,
so the logic is testable without a browser.

Human edits go through THE SAME normalizer as the AI's output (A§3.6): the form's
text values are rebuilt into a ReceiptExtraction and normalized again, then the
validator runs. Saving re-validates first (the user may have edited after the last
re-validate), enforces the save rules, builds the strict ConfirmedReceipt, writes the
processed image to data/images/<sha256>.jpg and saves in one DB transaction.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

import psycopg
from pydantic import ValidationError

from bahikhata import config, db
from bahikhata.extraction import diff_fields
from bahikhata.images import ProcessedImage
from bahikhata.normalize import normalize_extraction
from bahikhata.schemas import (
    ConfirmedReceipt,
    ExtractedLineItem,
    ReceiptDraft,
    ReceiptExtraction,
    ReceiptStatus,
    ValidationFlag,
)
from bahikhata.validate import may_save, summarize_flags, validate_draft

# Form keys for the money fields: form key -> (ReceiptExtraction text field, ReceiptDraft paisa field)
AMOUNT_FIELDS = {
    "subtotal": ("subtotal_raw", "subtotal_paisa"),
    "discount": ("discount_raw", "discount_paisa"),
    "service_charge": ("service_charge_raw", "service_charge_paisa"),
    "vat": ("vat_amount_raw", "vat_paisa"),
    "total": ("total_raw", "total_paisa"),
}
LINE_COLUMNS = ["description", "quantity", "unit_price", "amount"]
# Form fields that block saving when empty (V1, V9); the page marks them with "*".
REQUIRED_FORM_FIELDS = ("merchant_name", "date", "category", "total")
CALENDARS = ["BS", "AD", "unknown"]


def today() -> date:
    """The date used for V8. Kept in one place so tests can pin it."""
    return date.today()


@dataclass
class Review:
    """Result of normalize + validate on the current form values."""

    draft: ReceiptDraft
    flags: list[ValidationFlag] = field(default_factory=list)
    status: ReceiptStatus = "clean"
    can_save: bool = True
    needs_override: bool = False

    @classmethod
    def from_flags(cls, draft: ReceiptDraft, flags: list[ValidationFlag]) -> "Review":
        return cls(draft, list(flags), *summarize_flags(flags))


class SaveError(Exception):
    """Saving did not happen. str(error) is safe to show; the form keeps the user's edits."""


# --- Flags -> form ------------------------------------------------------------

_FLAG_FIELD_TO_FORM = {
    "date_ad": "date",
    **{paisa_field: key for key, (_, paisa_field) in AMOUNT_FIELDS.items()},
}


def form_field_for(flag_field: str | None) -> str | None:
    """Which form field a flag belongs under: "total_paisa" -> "total", "date_ad" -> "date",
    "line_items[2].amount_paisa" -> "line_items", "merchant_name" -> "merchant_name".
    None (e.g. EXTRACTION_FAILED) means the flag belongs in the panel at the top."""
    if flag_field is None:
        return None
    if flag_field.startswith("line_items"):
        return "line_items"
    return _FLAG_FIELD_TO_FORM.get(flag_field, flag_field)


def flags_by_form_field(flags: list[ValidationFlag]) -> dict[str | None, list[ValidationFlag]]:
    grouped: dict[str | None, list[ValidationFlag]] = {}
    for flag in flags:
        grouped.setdefault(form_field_for(flag.field), []).append(flag)
    return grouped


def _short_message(flag: ValidationFlag) -> str:
    """"V1: total is missing" -> "total is missing"; drops the long category list."""
    message = flag.message.removeprefix(f"{flag.rule_id}: ")
    return re.sub(r"\s*\(one of:.*\)$", "", message)


def save_hint(current: Review) -> str | None:
    """One line under the buttons explaining why saving is not possible yet, or None."""
    blocking = [_short_message(f) for f in current.flags if f.severity == "BLOCKING"]
    if blocking:
        return "Can't save yet: " + "; ".join(blocking) + "."
    if current.needs_override:
        return ("The amounts don't add up (V5): check them, then tick "
                "“I checked this, save anyway” to save.")
    return None


# --- Text helpers ------------------------------------------------------------

def paisa_to_text(paisa: int | None) -> str:
    """125050 -> "1250.50" (the NPR text the user edits); None -> ""."""
    if paisa is None:
        return ""
    sign = "-" if paisa < 0 else ""
    rupees, rest = divmod(abs(paisa), 100)
    return f"{sign}{rupees}.{rest:02d}"


def _text(value: Any) -> str | None:
    """Widget/data_editor cell -> stripped text, or None for blank/NaN."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    return text or None


# --- Draft <-> form ----------------------------------------------------------

def draft_to_form(
    draft: ReceiptDraft, extraction: ReceiptExtraction | None = None
) -> tuple[dict[str, str | None], list[dict[str, str]]]:
    """Initial form values. Parsed amounts are shown as NPR text; an amount that could
    not be parsed (N1) shows the AI's original text so the user sees what was read."""
    header: dict[str, str | None] = {
        "merchant_name": draft.merchant_name or "",
        "merchant_pan": draft.merchant_pan or "",
        "invoice_number": draft.invoice_number or "",
        "date_raw": draft.date_raw or "",
        "calendar": draft.date_calendar_hint or "unknown",
        "category": draft.category.value if draft.category else None,
    }
    for key, (raw_field, paisa_field) in AMOUNT_FIELDS.items():
        paisa = getattr(draft, paisa_field)
        raw = getattr(extraction, raw_field) if extraction else None
        header[key] = paisa_to_text(paisa) if paisa is not None else (raw or "")

    extracted_items = extraction.line_items if extraction else []
    rows = []
    for i, item in enumerate(draft.line_items):
        raw = extracted_items[i] if i < len(extracted_items) else ExtractedLineItem()
        rows.append({
            "description": item.description or "",
            "quantity": str(item.quantity) if item.quantity is not None else (raw.quantity_raw or ""),
            "unit_price": paisa_to_text(item.unit_price_paisa) if item.unit_price_paisa is not None
            else (raw.unit_price_raw or ""),
            "amount": paisa_to_text(item.amount_paisa) if item.amount_paisa is not None
            else (raw.amount_raw or ""),
        })
    return header, rows


def form_to_extraction(header: dict[str, Any], rows: list[dict[str, Any]]) -> ReceiptExtraction:
    """Rebuild the human's edits as text fields, ready for the same normalizer as AI output.
    Line-item rows whose cells are all blank (e.g. a new empty data_editor row) are dropped."""
    calendar = _text(header.get("calendar"))
    line_items = [
        ExtractedLineItem(
            description=_text(row.get("description")),
            quantity_raw=_text(row.get("quantity")),
            unit_price_raw=_text(row.get("unit_price")),
            amount_raw=_text(row.get("amount")),
        )
        for row in rows
        if any(_text(row.get(column)) for column in LINE_COLUMNS)
    ]
    return ReceiptExtraction(
        merchant_name=_text(header.get("merchant_name")),
        merchant_pan=_text(header.get("merchant_pan")),
        invoice_number=_text(header.get("invoice_number")),
        date_raw=_text(header.get("date_raw")),
        date_calendar_hint=calendar if calendar in CALENDARS else None,
        category=_text(header.get("category")),
        line_items=line_items,
        **{raw_field: _text(header.get(key)) for key, (raw_field, _) in AMOUNT_FIELDS.items()},
    )


def revalidate(header: dict[str, Any], rows: list[dict[str, Any]], on: date) -> Review:
    """Form values -> normalize -> validate. Nothing is modified; a new Review is returned."""
    draft, n_flags = normalize_extraction(form_to_extraction(header, rows))
    v_flags, *_ = validate_draft(draft, on)
    return Review.from_flags(draft, [*n_flags, *v_flags])


# --- Save --------------------------------------------------------------------

def build_confirmed(draft: ReceiptDraft, status: ReceiptStatus, user_override: bool) -> ConfirmedReceipt:
    """Strict record for the DB. Raises pydantic.ValidationError if a required value is missing."""
    data = draft.model_dump(exclude={"date_raw", "date_calendar_hint"})
    return ConfirmedReceipt.model_validate({**data, "status": status, "user_override": user_override})


def write_image(image: ProcessedImage) -> str:
    """Store the processed JPEG as <IMAGES_DIR>/<sha256>.jpg; return the path saved in receipts.image_path."""
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = config.IMAGES_DIR / f"{image.processed_sha256}.jpg"
    if not path.exists():  # content-addressed: same image, same file
        path.write_bytes(image.processed_bytes)
    try:
        return str(path.relative_to(config.PROJECT_ROOT))
    except ValueError:
        return str(path)


def save_receipt(
    *,
    current: Review,
    ai_draft: ReceiptDraft,
    audit_payload: dict[str, Any],
    image: ProcessedImage,
    user_override: bool,
    connect: Callable[[], psycopg.Connection] | None = None,
) -> int:
    """Save a reviewed receipt; return its id. `current` must be a Review computed from the
    form values just now (the page re-validates immediately before calling this).

    Raises SaveError (user-safe message) if the save rules refuse it or anything fails;
    nothing is half-saved (one DB transaction).
    """
    if not current.can_save:
        raise SaveError("Fix the ⛔ problems above before saving.")
    override = user_override and current.needs_override  # only meaningful for an ERROR (V5)
    if not may_save(current.flags, override):
        raise SaveError("The amounts don't add up (V5). Check them, then tick “I checked this, save anyway”.")
    try:
        confirmed = build_confirmed(current.draft, current.status, override)
    except ValidationError as exc:
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors())
        raise SaveError(f"Some values are missing or invalid: {fields}.") from exc

    audit = db.AuditRecord(
        **audit_payload,
        flags_at_save_json=[f.model_dump() for f in current.flags],
        edited_fields_json=diff_fields(ai_draft, confirmed),
        confirmed_at=datetime.now(timezone.utc),
    )
    try:
        image_path = write_image(image)
    except OSError as exc:
        raise SaveError("Couldn't store the receipt image — your edits are kept, try again.") from exc
    try:
        with (connect or db.get_connection)() as conn:
            return db.save_confirmed_receipt(conn, confirmed, image_path, audit)
    except RuntimeError as exc:  # DATABASE_URL missing (config.get_database_url)
        raise SaveError("The database is not configured: set DATABASE_URL in .env, then try again. "
                        "Your edits are kept.") from exc
    except psycopg.Error as exc:  # connection or constraint failure; message may name the host, so not shown
        raise SaveError(f"Couldn't save ({type(exc).__name__}) — your edits are kept, try again.") from exc
