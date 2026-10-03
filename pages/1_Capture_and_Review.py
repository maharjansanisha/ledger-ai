"""Capture & Review page: upload -> extract -> review -> save (TASK-016/017/018, A§3.1, A§11).

UI only. Logic lives in bahikhata/ (images, extraction, review). Streamlit reruns
this script on every interaction, so:
- the API is called ONLY when "Extract" (or "Try again") is clicked;
- the upload, extraction result and current review live in st.session_state;
- form widgets are keyed per extraction ("version"), so edits survive reruns.
"""

import hashlib

import pandas as pd
import streamlit as st

from bahikhata import extraction, review
from bahikhata.images import ImageIntakeError, process_upload
from bahikhata.schemas import Category, ReceiptExtraction

st.set_page_config(page_title="Capture & Review · BahiKhata AI", page_icon="🧾", layout="wide")
S = st.session_state

_SESSION_KEYS = ("upload_sha", "upload_bytes", "processed", "intake_error", "phase", "ai_draft",
                 "ai_extraction", "audit_payload", "fail_message", "header_base", "rows_base",
                 "current", "save_error", "saved_id")
_SEVERITY_ICON = {"BLOCKING": "⛔", "ERROR": "❗", "WARNING": "⚠️"}
_AMOUNT_LABELS = {"subtotal": "Subtotal", "discount": "Discount", "service_charge": "Service charge",
                  "vat": "VAT", "total": "Total"}


def reset() -> None:
    for key in _SESSION_KEYS:
        S.pop(key, None)


def start_review(draft, flags, ai_extraction: ReceiptExtraction | None) -> None:
    header, rows = review.draft_to_form(draft, ai_extraction)
    S.version = S.get("version", 0) + 1
    S.header_base = header
    S.rows_base = pd.DataFrame(rows, columns=review.LINE_COLUMNS, dtype="string")
    S.current = review.Review.from_flags(draft, flags)
    S.phase = "review"


def run_extraction() -> None:
    try:
        with st.spinner("Reading the receipt…"):
            draft, flags, _status, audit = extraction.extract_draft(S.upload_bytes, review.today())
    except ImageIntakeError as exc:
        S.intake_error = str(exc)
        return
    except Exception:  # never show a traceback; extract_draft already turns API errors into flags
        S.phase, S.fail_message = "failed", "Something went wrong while reading the receipt. Try again."
        return
    S.ai_draft, S.audit_payload = draft, audit
    S.ai_extraction = (ReceiptExtraction.model_validate(audit["ai_extraction_json"])
                       if audit["ai_extraction_json"] else None)
    failed = [f for f in flags if f.rule_id == "EXTRACTION_FAILED"]
    if failed:
        S.phase, S.fail_message = "failed", failed[0].message.removeprefix("EXTRACTION_FAILED: ")
    else:
        start_review(draft, flags, S.ai_extraction)


def show_flags(current: review.Review) -> None:
    status_text = {"clean": "✅ clean", "needs_review": "⚠️ needs review", "invalid": "⛔ invalid"}
    st.markdown(f"**Status:** {status_text[current.status]}")
    for flag in current.flags:
        where = f" · `{flag.field}`" if flag.field else ""
        st.markdown(f"{_SEVERITY_ICON[flag.severity]} **{flag.severity}**{where} — {flag.message}")


def field_error(current: review.Review, field_name: str) -> None:
    for flag in current.flags:
        if flag.field == field_name and flag.rule_id in ("N1", "N2", "N3"):
            st.caption(f":red[{flag.message}]")


# --- Upload -------------------------------------------------------------------

st.title("Capture & Review")
uploaded = st.file_uploader("Receipt photo (JPG or PNG, max 10 MB)", type=["jpg", "jpeg", "png"])

if uploaded is not None:
    data = uploaded.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    if S.get("upload_sha") != sha:  # a new file: start over
        reset()
        S.upload_sha, S.upload_bytes = sha, data
        try:
            S.processed = process_upload(data)
        except ImageIntakeError as exc:
            S.intake_error = str(exc)
elif "upload_sha" in S:  # the file was removed from the uploader
    reset()

if S.get("intake_error"):
    st.error(S.intake_error)
    st.stop()
if "processed" not in S:
    st.info("Upload a photo of a receipt to start. Nothing is sent anywhere until you click **Extract**.")
    st.stop()

left, right = st.columns([2, 3])
with left:
    st.image(S.processed.processed_bytes, caption="Receipt (click the image corner for full screen)")

with right:
    if S.get("saved_id") is not None:
        st.success(f"Saved receipt #{S.saved_id}.")
        if st.button("Capture another receipt", key="capture_another"):
            reset()
            st.rerun()
        st.stop()

    phase = S.get("phase")
    if phase is None:
        if st.button("Extract", type="primary", key="extract"):
            run_extraction()
            st.rerun()
        st.stop()

    if phase == "failed":
        st.error(S.fail_message)
        col_retry, col_manual = st.columns(2)
        if col_retry.button("Try again", key="try_again"):
            run_extraction()
            st.rerun()
        if col_manual.button("Enter by hand", key="enter_by_hand"):
            S.ai_extraction = None
            start_review(S.ai_draft, [], None)
            S.current = review.revalidate(S.header_base, [], review.today())
            st.rerun()
        st.stop()

    # --- Review ----------------------------------------------------------------
    current: review.Review = S.current
    show_flags(current)
    if S.get("save_error"):
        st.error(S.save_error)

    v, base = S.version, S.header_base
    with st.form(f"review_{v}"):
        header = {
            "merchant_name": st.text_input("Merchant", base["merchant_name"], key=f"merchant_name_{v}"),
            "merchant_pan": st.text_input("PAN / VAT no.", base["merchant_pan"], key=f"merchant_pan_{v}"),
            "invoice_number": st.text_input("Invoice no.", base["invoice_number"], key=f"invoice_number_{v}"),
        }
        col_date, col_cal = st.columns([3, 1])
        header["date_raw"] = col_date.text_input("Date (as printed)", base["date_raw"], key=f"date_raw_{v}")
        header["calendar"] = col_cal.selectbox(
            "Calendar", review.CALENDARS, index=review.CALENDARS.index(base["calendar"]), key=f"calendar_{v}",
            help="Only used when the year is two digits; a 4-digit year decides by itself.",
        )
        st.caption(f"Read as: AD **{current.draft.date_ad or '—'}** · BS **{current.draft.date_bs or '—'}**")
        field_error(current, "date_ad")

        categories = [None] + [c.value for c in Category]
        header["category"] = st.selectbox(
            "Category", categories, index=categories.index(base["category"]),
            format_func=lambda c: "— choose —" if c is None else c, key=f"category_{v}",
        )

        st.markdown("**Amounts** (NPR, e.g. `1250.50`)")
        for key, label in _AMOUNT_LABELS.items():
            header[key] = st.text_input(label, base[key], key=f"{key}_{v}")
            field_error(current, review.AMOUNT_FIELDS[key][1])

        st.markdown("**Line items**")
        edited_rows = st.data_editor(
            S.rows_base, num_rows="dynamic", key=f"items_{v}", width="stretch",
            column_config={c: st.column_config.TextColumn(c.replace("_", " ").title()) for c in review.LINE_COLUMNS},
        )

        override = False
        if current.needs_override:
            override = st.checkbox("I checked this, save anyway", key=f"override_{v}")

        col_check, col_save = st.columns(2)
        revalidate_clicked = col_check.form_submit_button("Re-validate")
        save_clicked = col_save.form_submit_button(
            "Confirm & Save", type="primary", disabled=not current.can_save,
            help=None if current.can_save else "Fix the ⛔ problems first.",
        )

    if revalidate_clicked or save_clicked:
        rows = edited_rows.to_dict("records")
        S.current = review.revalidate(header, rows, review.today())
        S.save_error = None
        if save_clicked:
            try:
                S.saved_id = review.save_receipt(
                    current=S.current, ai_draft=S.ai_draft, audit_payload=S.audit_payload,
                    image=S.processed, user_override=override,
                )
            except review.SaveError as exc:
                S.save_error = str(exc)
        st.rerun()
