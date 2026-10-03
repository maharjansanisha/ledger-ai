"""Normalizer: transcribed text -> typed values (ARCHITECTURE.md §3.6, §8).

TASK-007 scope: amounts. Amount text is parsed into integer paisa with Decimal,
never float (decision D1). When text cannot be parsed unambiguously the result
is None plus an N1 flag; the parser never guesses.

Date parsing and BS<->AD conversion are TASK-008 and are not done here:
date_raw is copied through unchanged.
"""

import re
from decimal import Decimal

from bahikhata.schemas import (
    DraftLineItem,
    ExtractedLineItem,
    ReceiptDraft,
    ReceiptExtraction,
    ValidationFlag,
)

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Currency markers at the start (Rs, Rs., NRs, NPR, रू, रु) and the end ("/-", "only").
_PREFIX = re.compile(r"^(?:n?rs\.?|npr\.?|रू\.?|रु\.?)\s*", re.IGNORECASE)
_SUFFIX = re.compile(r"\s*(?:/-|only)$", re.IGNORECASE)

# Integer part: plain digits, or correctly grouped thousands (1,250 / 1,250,000)
# or Indian lakh grouping (1,25,000). Anything else (e.g. "12,50") is ambiguous.
_AMOUNT = re.compile(
    r"^(?P<int>\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.(?P<frac>\d{1,2}))?$"
)
_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)\s*[a-z.]*$", re.IGNORECASE)  # "2", "1.5 kg", "3 pcs"

_AMOUNT_FIELDS = {
    "subtotal_raw": "subtotal_paisa",
    "discount_raw": "discount_paisa",
    "service_charge_raw": "service_charge_paisa",
    "vat_amount_raw": "vat_paisa",
    "total_raw": "total_paisa",
}


def _is_blank(text: str | None) -> bool:
    return text is None or not text.strip()


def parse_amount_to_paisa(text: str | None) -> int | None:
    """Parse printed amount text into integer paisa, or None if absent/unparseable.

    "Rs. 1,250.00" -> 125000, "1,25,000/-" -> 12500000, "१२५०" -> 125000, "1250.5" -> 125050.
    Negative, parenthesised, >2 decimals, several decimal points, stray letters -> None.
    """
    if _is_blank(text):
        return None
    s = text.strip().translate(_DEVANAGARI_DIGITS)
    s = _PREFIX.sub("", s)
    while (stripped := _SUFFIX.sub("", s)) != s:  # e.g. "1250/- only"
        s = stripped
    s = re.sub(r"\s+", "", s)
    match = _AMOUNT.match(s)
    if not match:
        return None
    value = Decimal(match["int"].replace(",", "") + "." + (match["frac"] or "0"))
    return int(value * 100)  # exact: at most 2 decimal places


def parse_quantity(text: str | None) -> Decimal | None:
    """Parse a line-item quantity ("2", "1.5 kg", "३"); not money. None if unparseable."""
    if _is_blank(text):
        return None
    match = _QUANTITY.match(text.strip().translate(_DEVANAGARI_DIGITS))
    return Decimal(match[1]) if match else None


def format_npr(paisa: int) -> str:
    """Display formatter shared by dashboard and Ask page: 125050 -> "Rs 1,250.50"."""
    sign = "-" if paisa < 0 else ""
    rupees, rest = divmod(abs(paisa), 100)
    return f"{sign}Rs {rupees:,}.{rest:02d}"


def _amount(text: str | None, field: str, flags: list[ValidationFlag]) -> int | None:
    """Parse one amount; add an N1 flag if text was present but could not be parsed."""
    paisa = parse_amount_to_paisa(text)
    if paisa is None and not _is_blank(text):
        flags.append(ValidationFlag(
            rule_id="N1", severity="WARNING", field=field,
            message=f"N1: amount {text!r} could not be read as a number; please enter it",
        ))
    return paisa


def _line_item(item: ExtractedLineItem, index: int, flags: list[ValidationFlag]) -> DraftLineItem:
    prefix = f"line_items[{index}]"
    return DraftLineItem(
        description=item.description,
        quantity=parse_quantity(item.quantity_raw),
        unit_price_paisa=_amount(item.unit_price_raw, f"{prefix}.unit_price_paisa", flags),
        amount_paisa=_amount(item.amount_raw, f"{prefix}.amount_paisa", flags),
    )


def normalize_amounts(extraction: ReceiptExtraction) -> tuple[ReceiptDraft, list[ValidationFlag]]:
    """Build a ReceiptDraft from an extraction, parsing the AMOUNT fields only.

    Text fields and date_raw are copied unchanged; date_ad/date_bs/bs_* stay None
    (TASK-008), and category stays None (checked against the enum later, V9).
    Returns the draft plus N1 flags. The extraction is not modified.
    """
    flags: list[ValidationFlag] = []
    amounts = {
        paisa_field: _amount(getattr(extraction, raw_field), paisa_field, flags)
        for raw_field, paisa_field in _AMOUNT_FIELDS.items()
    }
    line_items = [_line_item(item, i, flags) for i, item in enumerate(extraction.line_items)]
    draft = ReceiptDraft(
        merchant_name=extraction.merchant_name,
        merchant_pan=extraction.merchant_pan,
        invoice_number=extraction.invoice_number,
        date_raw=extraction.date_raw,
        date_calendar_hint=extraction.date_calendar_hint,
        line_items=line_items,
        **amounts,
    )
    return draft, flags
