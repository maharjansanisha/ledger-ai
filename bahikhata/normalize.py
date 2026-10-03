"""Normalizer: transcribed text -> typed values (ARCHITECTURE.md §3.6, §8).

Amounts (TASK-007): text -> integer paisa with Decimal, never float (decision D1).
Dates (TASK-008): printed text -> AD date + BS date via nepali-datetime; the LLM
never converts calendars. The calendar comes from the year-range rule
(year >= 2070 -> BS, <= 2035 -> AD); the model's hint is used only when the year
is two-digit.

When text cannot be read unambiguously the value is None plus a flag
(N1 amount, N2 date); an assumed day/month order is visible as N3. Nothing guesses.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import nepali_datetime

from bahikhata import config
from bahikhata.schemas import (
    CalendarHint,
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
    (normalize_extraction fills them), and category stays None (checked later, V9).
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


# --- Dates (TASK-008) --------------------------------------------------------

_DATE_LABEL = re.compile(r"^(?:date|मिति)\s*[:.]?\s*", re.IGNORECASE)
_CALENDAR_MARKER = re.compile(
    r"\s*(?P<marker>b\.?\s?s\.?|a\.?\s?d\.?|वि\.?\s?सं\.?)$", re.IGNORECASE
)
_TRAILING_TIME = re.compile(r"\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?$", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"^(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,4})$")


@dataclass
class DateResult:
    """Normalized date fields for a ReceiptDraft. All None when the date is absent or unreadable."""

    date_ad: date | None = None
    date_bs: str | None = None          # 'YYYY-MM-DD' in BS
    bs_year: int | None = None
    bs_month: int | None = None
    flags: list[ValidationFlag] = field(default_factory=list)


def _date_flag(rule_id: str, message: str) -> ValidationFlag:
    return ValidationFlag(rule_id=rule_id, severity="WARNING", field="date_ad", message=message)


def _unreadable(date_raw: str, reason: str) -> DateResult:
    return DateResult(flags=[_date_flag("N2", f"N2: date {date_raw!r} {reason}; please enter it")])


def normalize_date(date_raw: str | None, calendar_hint: CalendarHint | None = None) -> DateResult:
    """Parse a printed date and fill AD + BS fields, or return None fields plus N2.

    Accepts year-first ("2083-06-14", "2083/06/14 B.S.") and day-first
    ("14/06/2083", "30/09/2026") numeric dates with -, / or . separators,
    Devanagari digits, a leading "Date:" label and a trailing time.
    Day-first dates where both numbers are <= 12 are read as DD/MM with an N3 warning.
    """
    if _is_blank(date_raw):
        return DateResult()  # missing date: V1 reports it

    s = _DATE_LABEL.sub("", date_raw.strip().translate(_DEVANAGARI_DIGITS))
    s = _TRAILING_TIME.sub("", s)
    marker = _CALENDAR_MARKER.search(s)
    if marker:
        s = s[: marker.start()]
        hint = "AD" if marker["marker"].lower().startswith("a") else "BS"
    else:
        hint = calendar_hint
    s = re.sub(r"\s*([-/.])\s*", r"\1", s.strip())

    match = _NUMERIC_DATE.match(s)
    if not match:
        return _unreadable(date_raw, "is not a numeric day/month/year date")
    first, second, third = match.groups()
    flags: list[ValidationFlag] = []

    if len(first) == 4:                                 # YYYY-MM-DD
        year, month, day = int(first), int(second), int(third)
    elif len(third) in (2, 4) and len(first) <= 2:      # DD/MM/YYYY or DD/MM/YY
        a, b = int(first), int(second)
        if a > 12 and b > 12:
            return _unreadable(date_raw, "has no valid month")
        if a <= 12 < b:                                 # only MM/DD can be valid
            month, day = a, b
        else:
            day, month = a, b
            if b <= 12 and a <= 12 and a != b:
                flags.append(_date_flag(
                    "N3", f"N3: day/month order in {date_raw!r} assumed DD/MM "
                          f"(day {a}, month {b}) — check",
                ))
        year = int(third)
        if len(third) == 2:                             # two-digit year: only now is the hint used
            if hint not in ("BS", "AD"):
                return _unreadable(date_raw, "has a two-digit year and no clear calendar")
            year += 2000
            if (hint == "BS") != (year >= config.BS_YEAR_MIN):
                return _unreadable(date_raw, f"has a two-digit year that does not fit {hint}")
    else:
        return _unreadable(date_raw, "is not a numeric day/month/year date")

    if year >= config.BS_YEAR_MIN:
        calendar = "BS"
    elif year <= config.AD_YEAR_MAX:
        calendar = "AD"
    else:
        return _unreadable(date_raw, f"has year {year}, which is neither a plausible AD nor BS year")

    try:
        if calendar == "BS":
            bs_date = nepali_datetime.date(year, month, day)
            ad_date = bs_date.to_datetime_date()
        else:
            ad_date = date(year, month, day)
            bs_date = nepali_datetime.date.from_datetime_date(ad_date)
    except ValueError as exc:
        return _unreadable(date_raw, f"is not a valid {calendar} date ({exc.args[0]})")

    return DateResult(
        date_ad=ad_date,
        date_bs=bs_date.strftime("%Y-%m-%d"),
        bs_year=bs_date.year,
        bs_month=bs_date.month,
        flags=flags,
    )


def normalize_extraction(extraction: ReceiptExtraction) -> tuple[ReceiptDraft, list[ValidationFlag]]:
    """Full ReceiptExtraction -> (ReceiptDraft, N-flags): amounts and dates. Input not modified."""
    draft, flags = normalize_amounts(extraction)
    result = normalize_date(extraction.date_raw, extraction.date_calendar_hint)
    draft = draft.model_copy(update={
        "date_ad": result.date_ad,
        "date_bs": result.date_bs,
        "bs_year": result.bs_year,
        "bs_month": result.bs_month,
    })
    return draft, flags + result.flags
