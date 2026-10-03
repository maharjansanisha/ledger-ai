"""Validation rules V1-V9 (ARCHITECTURE.md §10, PRD §15, TASK-010).

The validator DETECTS and never corrects: it receives a draft and returns flags.
There is no code path that returns a modified draft.

Severities (decision D2):
- BLOCKING (V1 required fields, V2 positive amounts, V9 category): cannot save.
- ERROR (V5 bill arithmetic): can be saved only with an explicit "Save anyway".
- WARNING (V3, V4, V6, V7, V8): shown, never block.
V10 (duplicates) is P1 (TASK-051); `existing` is the hook for it and is unused here.

All money arithmetic is on integer paisa; tolerances come from config.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

import nepali_datetime

from bahikhata import config
from bahikhata.normalize import format_npr
from bahikhata.schemas import Category, ReceiptDraft, ReceiptStatus, ValidationFlag

_PAISA_FIELDS = ("subtotal_paisa", "discount_paisa", "service_charge_paisa", "vat_paisa", "total_paisa")


def _flag(rule_id: str, severity: str, field: str | None, message: str) -> ValidationFlag:
    return ValidationFlag(rule_id=rule_id, severity=severity, field=field, message=f"{rule_id}: {message}")


def _npr(paisa: int) -> str:
    """125050 -> "1,250.50", -1250 -> "-12.50" (format_npr without the "Rs ")."""
    return ("-" if paisa < 0 else "") + format_npr(abs(paisa)).removeprefix("Rs ")


def _round_paisa(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _earliest_plausible_ad() -> date:
    y, m, d = (int(p) for p in config.EARLIEST_PLAUSIBLE_DATE_BS.split("-"))
    return nepali_datetime.date(y, m, d).to_datetime_date()


def _v1_required(draft: ReceiptDraft) -> list[ValidationFlag]:
    flags = []
    if draft.merchant_name is None or not draft.merchant_name.strip():
        flags.append(_flag("V1", "BLOCKING", "merchant_name", "merchant name is missing"))
    if draft.date_ad is None:
        flags.append(_flag("V1", "BLOCKING", "date_ad", "date is missing or could not be read"))
    if draft.total_paisa is None:
        flags.append(_flag("V1", "BLOCKING", "total_paisa", "total is missing"))
    return flags


def _v2_positive(draft: ReceiptDraft) -> list[ValidationFlag]:
    flags = []
    if draft.total_paisa is not None and draft.total_paisa <= 0:
        flags.append(_flag("V2", "BLOCKING", "total_paisa",
                           f"total must be more than 0 (is {_npr(draft.total_paisa)})"))
    for name in _PAISA_FIELDS[:-1]:
        value = getattr(draft, name)
        if value is not None and value < 0:
            flags.append(_flag("V2", "BLOCKING", name, f"{name.removesuffix('_paisa')} is negative ({_npr(value)})"))
    for i, item in enumerate(draft.line_items):
        for name in ("unit_price_paisa", "amount_paisa"):
            value = getattr(item, name)
            if value is not None and value < 0:
                flags.append(_flag("V2", "BLOCKING", f"line_items[{i}].{name}",
                                   f"line {i + 1} {name.removesuffix('_paisa')} is negative ({_npr(value)})"))
    return flags


def _v3_line_arithmetic(draft: ReceiptDraft) -> list[ValidationFlag]:
    flags = []
    for i, item in enumerate(draft.line_items):
        if item.quantity is None or item.unit_price_paisa is None or item.amount_paisa is None:
            continue
        expected = _round_paisa(item.quantity * item.unit_price_paisa)
        diff = item.amount_paisa - expected
        if abs(diff) > config.AMOUNT_TOLERANCE_PAISA:
            flags.append(_flag(
                "V3", "WARNING", f"line_items[{i}].amount_paisa",
                f"line {i + 1}: {item.quantity} × {_npr(item.unit_price_paisa)} = {_npr(expected)} "
                f"but amount is {_npr(item.amount_paisa)} (diff {_npr(diff)})",
            ))
    return flags


def _v4_lines_vs_subtotal(draft: ReceiptDraft) -> list[ValidationFlag]:
    amounts = [item.amount_paisa for item in draft.line_items]
    if draft.subtotal_paisa is None or not amounts or None in amounts:
        return []  # skipped unless the subtotal and every line amount are present
    lines_total = sum(amounts)
    diff = lines_total - draft.subtotal_paisa
    if abs(diff) <= config.AMOUNT_TOLERANCE_PAISA:
        return []
    return [_flag("V4", "WARNING", "subtotal_paisa",
                  f"line items add up to {_npr(lines_total)} but subtotal is "
                  f"{_npr(draft.subtotal_paisa)} (diff {_npr(diff)})")]


def _v5_bill_arithmetic(draft: ReceiptDraft) -> list[ValidationFlag]:
    if draft.subtotal_paisa is None or draft.total_paisa is None:
        return []
    discount = draft.discount_paisa or 0
    service_charge = draft.service_charge_paisa or 0
    vat = draft.vat_paisa or 0
    expected = draft.subtotal_paisa - discount + service_charge + vat
    diff = draft.total_paisa - expected
    if abs(diff) <= config.AMOUNT_TOLERANCE_PAISA:
        return []
    return [_flag("V5", "ERROR", "total_paisa",
                  f"subtotal {_npr(draft.subtotal_paisa)} − discount {_npr(discount)} + service charge "
                  f"{_npr(service_charge)} + VAT {_npr(vat)} = {_npr(expected)} but total is "
                  f"{_npr(draft.total_paisa)} (diff {_npr(diff)})")]


def _v6_vat_rate(draft: ReceiptDraft) -> list[ValidationFlag]:
    if draft.vat_paisa is None or draft.subtotal_paisa is None:
        return []  # VAT-inclusive bill, or nothing to compare against
    taxable = draft.subtotal_paisa - (draft.discount_paisa or 0) + (draft.service_charge_paisa or 0)
    expected = _round_paisa(Decimal(taxable) * config.VAT_RATE_PERCENT / 100)
    diff = draft.vat_paisa - expected
    # Allowed: max(NPR 2, 1% of expected), compared in integers: |diff|*100 <= expected*1%*100.
    if abs(diff) <= config.VAT_TOLERANCE_MIN_PAISA or abs(diff) * 100 <= abs(expected) * config.VAT_TOLERANCE_PERCENT:
        return []
    return [_flag("V6", "WARNING", "vat_paisa",
                  f"VAT {_npr(draft.vat_paisa)} ≠ {config.VAT_RATE_PERCENT}% of taxable {_npr(taxable)} "
                  f"({_npr(expected)}, diff {_npr(diff)})")]


def _v7_pan(draft: ReceiptDraft) -> list[ValidationFlag]:
    if draft.merchant_pan is None:
        return []
    pan = draft.merchant_pan.strip()
    if len(pan) == 9 and all(c in "0123456789" for c in pan):
        return []
    return [_flag("V7", "WARNING", "merchant_pan", f"PAN {draft.merchant_pan!r} is not exactly 9 digits")]


def _v8_date_plausible(draft: ReceiptDraft, today: date) -> list[ValidationFlag]:
    if draft.date_ad is None:
        return []  # unreadable dates are already N2 + V1
    if draft.date_ad > today:
        return [_flag("V8", "WARNING", "date_ad", f"date {draft.date_ad} is after today ({today})")]
    earliest = _earliest_plausible_ad()
    if draft.date_ad < earliest:
        return [_flag("V8", "WARNING", "date_ad",
                      f"date {draft.date_ad} is before {config.EARLIEST_PLAUSIBLE_DATE_BS} BS ({earliest})")]
    return []


def _v9_category(draft: ReceiptDraft) -> list[ValidationFlag]:
    if isinstance(draft.category, Category):
        return []
    allowed = ", ".join(c.value for c in Category)
    return [_flag("V9", "BLOCKING", "category", f"choose a category (one of: {allowed})")]


def summarize_flags(flags: Iterable[ValidationFlag]) -> tuple[ReceiptStatus, bool, bool]:
    """(status, can_save, needs_override) for any list of flags (V-, N- or EXTRACTION_FAILED).

    status: invalid if any BLOCKING or ERROR; needs_review if only WARNINGs; clean if none.
    can_save: no BLOCKING flags. needs_override: can_save and at least one ERROR (V5).
    """
    severities = {f.severity for f in flags}
    if severities & {"BLOCKING", "ERROR"}:
        status = "invalid"
    elif severities:
        status = "needs_review"
    else:
        status = "clean"
    can_save = "BLOCKING" not in severities
    return status, can_save, can_save and "ERROR" in severities


def may_save(flags: Iterable[ValidationFlag], user_override: bool) -> bool:
    """True if saving is allowed: no BLOCKING flags, and any ERROR needs `user_override` ("Save anyway")."""
    _, can_save, needs_override = summarize_flags(flags)
    return can_save and (user_override or not needs_override)


def validate_draft(
    draft: ReceiptDraft, today: date, existing=None
) -> tuple[list[ValidationFlag], ReceiptStatus, bool, bool]:
    """Run V1-V9 on a draft. Returns (flags, status, can_save, needs_override).

    `today` is passed in, never read from the clock (A§2 P10). The draft is not modified.
    """
    flags = [
        *_v1_required(draft),
        *_v2_positive(draft),
        *_v3_line_arithmetic(draft),
        *_v4_lines_vs_subtotal(draft),
        *_v5_bill_arithmetic(draft),
        *_v6_vat_rate(draft),
        *_v7_pan(draft),
        *_v8_date_plausible(draft, today),
        *_v9_category(draft),
    ]
    return (flags, *summarize_flags(flags))
