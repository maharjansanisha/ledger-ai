"""Rendering for Capture & Review. Reads session state, never changes it.

Each render_* function draws one part of the page and returns what the user did
(a clicked button, or the submitted form), leaving the page script to act on it
through `state`.
"""

from dataclasses import dataclass

import pandas as pd
import streamlit as st

from bahikhata import review
from bahikhata.schemas import Category

_SEVERITY_ICON = {"BLOCKING": "⛔", "ERROR": "❗", "WARNING": "⚠️"}
_STATUS_TEXT = {"clean": "✅ clean", "needs_review": "⚠️ needs review", "invalid": "⛔ invalid"}
_AMOUNT_LABELS = {"subtotal": "Subtotal", "discount": "Discount", "service_charge": "Service charge",
                  "vat": "VAT", "total": "Total"}


@dataclass
class FormSubmission:
    header: dict
    rows: list[dict]
    save: bool
    override: bool


def label(text: str, form_field: str) -> str:
    """Mark fields that block saving when empty with "*"."""
    return f"{text} *" if form_field in review.REQUIRED_FORM_FIELDS else text


def field_messages(grouped, form_field: str) -> None:
    for flag in grouped.get(form_field, []):
        st.caption(f"{_SEVERITY_ICON[flag.severity]} {flag.severity}: {flag.message}")


def render_header() -> None:
    st.title("Capture & Review")
    st.caption("Upload a receipt, check what was read, and save it to your ledger.")


def render_edit_header(receipt_id: int) -> bool:
    """Header in edit mode; returns True when "Back to dashboard" is clicked."""
    st.title(f"Edit receipt #{receipt_id}")
    st.caption("Change what's wrong, then save. The receipt is not read again.")
    return st.button("Back to dashboard", icon=":material/arrow_back:", key="back_to_dashboard")


def render_uploader() -> bytes | None:
    with st.container(border=True, key="card_upload"):
        uploaded = st.file_uploader("Receipt photo (JPG or PNG, max 10 MB)", type=["jpg", "jpeg", "png"])
    return None if uploaded is None else uploaded.getvalue()


def render_intake_error(message: str) -> None:
    st.error(message)


def render_empty_state() -> None:
    st.info("Upload a photo of a receipt to start. Nothing is sent anywhere until you click **Extract**.")


def render_image(container, image_bytes: bytes | None) -> None:
    with container.container(border=True, key="card_image"):
        if image_bytes is None:
            st.info("The receipt image could not be found on this computer.")
            return
        st.image(image_bytes, caption="Receipt (click the image corner for full screen)")


def render_extract_button() -> bool:
    return st.button("Extract", type="primary", key="extract")


def render_failed(message: str) -> tuple[bool, bool]:
    """Returns (try_again, enter_by_hand) clicks."""
    st.error(message)
    col_retry, col_manual = st.columns(2)
    return col_retry.button("Try again", key="try_again"), col_manual.button("Enter by hand", key="enter_by_hand")


def render_status(current: review.Review, grouped) -> None:
    """Status, plus the flags that belong to no field (the rest are shown next to their fields)."""
    inline = sum(len(flags) for field, flags in grouped.items() if field is not None)
    note = f" · {inline} message(s) shown next to the fields below" if inline else ""
    st.markdown(f"**Status:** {_STATUS_TEXT[current.status]}{note}")
    for flag in grouped.get(None, []):
        st.markdown(f"{_SEVERITY_ICON[flag.severity]} **{flag.severity}** — {flag.message}")


def _merchant_fields(header: dict, base: dict, grouped, v: int) -> None:
    header["merchant_name"] = st.text_input(
        label("Merchant", "merchant_name"), base["merchant_name"], key=f"merchant_name_{v}")
    field_messages(grouped, "merchant_name")
    header["merchant_pan"] = st.text_input("PAN / VAT no.", base["merchant_pan"], key=f"merchant_pan_{v}")
    field_messages(grouped, "merchant_pan")
    header["invoice_number"] = st.text_input("Invoice no.", base["invoice_number"], key=f"invoice_number_{v}")
    field_messages(grouped, "invoice_number")


def _date_fields(header: dict, base: dict, grouped, v: int, current: review.Review) -> None:
    col_date, col_cal = st.columns([3, 1])
    header["date_raw"] = col_date.text_input(
        label("Date (as printed)", "date"), base["date_raw"], key=f"date_raw_{v}")
    header["calendar"] = col_cal.selectbox(
        "Calendar", review.CALENDARS, index=review.CALENDARS.index(base["calendar"]), key=f"calendar_{v}",
        help="Only used when the year is two digits; a 4-digit year decides by itself.",
    )
    st.caption(f"Read as: AD **{current.draft.date_ad or '—'}** · BS **{current.draft.date_bs or '—'}**")
    field_messages(grouped, "date")


def _category_field(header: dict, base: dict, grouped, v: int) -> None:
    categories = [None] + [c.value for c in Category]
    header["category"] = st.selectbox(
        label("Category", "category"), categories, index=categories.index(base["category"]),
        format_func=lambda c: "— choose —" if c is None else c, key=f"category_{v}",
    )
    field_messages(grouped, "category")


def _amount_fields(header: dict, base: dict, grouped, v: int) -> None:
    st.markdown("**Amounts** (NPR, e.g. `1250.50`)")
    for key, text in _AMOUNT_LABELS.items():
        header[key] = st.text_input(label(text, key), base[key], key=f"{key}_{v}")
        field_messages(grouped, key)


def _line_items(rows_base: pd.DataFrame, grouped, v: int) -> pd.DataFrame:
    st.markdown("**Line items**")
    edited = st.data_editor(
        rows_base, num_rows="dynamic", key=f"items_{v}", width="stretch",
        column_config={c: st.column_config.TextColumn(c.replace("_", " ").title()) for c in review.LINE_COLUMNS},
    )
    field_messages(grouped, "line_items")
    return edited


def render_review_form(current: review.Review, base: dict, rows_base: pd.DataFrame, v: int,
                       save_error: str | None) -> FormSubmission | None:
    """The editable review form; returns the submission when Re-validate or Confirm & Save is clicked."""
    grouped = review.flags_by_form_field(current.flags)
    render_status(current, grouped)
    if save_error:
        st.error(save_error)

    st.subheader("Review")
    st.caption("Edit, then click **Re-validate** to check your changes.")
    st.caption("\\* required")
    with st.form(f"review_{v}"):
        header: dict = {}
        _merchant_fields(header, base, grouped, v)
        _date_fields(header, base, grouped, v, current)
        _category_field(header, base, grouped, v)
        _amount_fields(header, base, grouped, v)
        edited_rows = _line_items(rows_base, grouped, v)

        override = False
        if current.needs_override:
            override = st.checkbox("I checked this, save anyway", key=f"override_{v}")

        col_check, col_save = st.columns(2)
        revalidate_clicked = col_check.form_submit_button("Re-validate")
        save_clicked = col_save.form_submit_button(
            "Confirm & Save", type="primary", disabled=not current.can_save,
            help=None if current.can_save else "Fix the ⛔ problems first.",
        )
        hint = review.save_hint(current)
        if hint:
            st.caption(hint)

    if not (revalidate_clicked or save_clicked):
        return None
    return FormSubmission(header, edited_rows.to_dict("records"), save_clicked, override)
