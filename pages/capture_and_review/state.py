"""Session-state flow for Capture & Review: upload -> extract -> review -> save.

No rendering here. Domain logic lives in bahikhata/ (images, extraction, review);
this module only moves the page between phases and keeps what each phase needs in
st.session_state, because Streamlit reruns the page script on every interaction:
- the API is called ONLY from run_extraction() ("Extract" / "Try again");
- the upload, extraction result and current review survive reruns in session state;
- each new review bumps S.version, which the form keys its widgets with.

Phases (S.phase): None (uploaded, not extracted) -> "failed" | "review"; a
successful save sets S.saved_id, and the page then hands over to the Dashboard.

Edit mode (S.edit_id): a receipt already in the ledger is loaded straight into
"review" from the database -- no upload and no API call -- and saving updates it.
"""

import hashlib

import pandas as pd
import streamlit as st

from bahikhata import extraction, review
from bahikhata.images import ImageIntakeError, process_upload
from bahikhata.schemas import ReceiptExtraction

S = st.session_state

_SESSION_KEYS = ("upload_sha", "upload_bytes", "processed", "intake_error", "phase", "ai_draft",
                 "ai_extraction", "audit_payload", "fail_message", "header_base", "rows_base",
                 "current", "save_error", "saved_id", "edit_id", "edit_image")
# Read (and cleared) by the Dashboard after a save redirects there; survives reset().
NOTICE_KEY = "ledger_notice"


def reset() -> None:
    for key in _SESSION_KEYS:
        S.pop(key, None)


def handle_upload(data: bytes | None) -> None:
    """Start over when a different file is uploaded, or when the file is removed."""
    if data is None:
        if "upload_sha" in S:
            reset()
        return
    sha = hashlib.sha256(data).hexdigest()
    if S.get("upload_sha") == sha:
        return
    reset()
    S.upload_sha, S.upload_bytes = sha, data
    try:
        S.processed = process_upload(data)
    except ImageIntakeError as exc:
        S.intake_error = str(exc)


def start_review(draft, flags, ai_extraction: ReceiptExtraction | None) -> None:
    header, rows = review.draft_to_form(draft, ai_extraction)
    S.version = S.get("version", 0) + 1
    S.header_base = header
    S.rows_base = pd.DataFrame(rows, columns=review.LINE_COLUMNS, dtype="string")
    S.current = review.Review.from_flags(draft, flags)
    S.phase = "review"


def image_bytes() -> bytes | None:
    """The receipt image to show: the processed upload, or the saved image in edit mode."""
    if "processed" in S:
        return S.processed.processed_bytes
    return S.get("edit_image")


def requested_edit_id() -> int | None:
    """The receipt id in the URL (?edit=<id>, set by the Dashboard's Edit button), if any."""
    value = st.query_params.get("edit")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def start_edit(receipt_id: int) -> None:
    """Load a saved receipt into the review form. No API call: it was already reviewed."""
    reset()
    S.edit_id = receipt_id
    try:
        saved = review.load_saved_receipt(receipt_id)
    except review.LoadError as exc:
        S.intake_error = str(exc)
        return
    S.edit_image, S.ai_draft, S.audit_payload = saved.image_bytes, saved.ai_draft, None
    start_review(saved.draft, [], None)
    S.current = review.revalidate(S.header_base, S.rows_base.to_dict("records"), review.today())


def finish_save() -> None:
    """Leave a notice for the Dashboard and clear the page for the next receipt."""
    verb = "Updated" if S.get("edit_id") is not None else "Saved"
    S[NOTICE_KEY] = f"{verb} receipt #{S.saved_id}."
    reset()


def run_extraction() -> None:
    try:
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


def enter_by_hand() -> None:
    """Skip the failed extraction: an empty form, validated so the user sees what's required."""
    S.ai_extraction = None
    start_review(S.ai_draft, [], None)
    S.current = review.revalidate(S.header_base, [], review.today())


def submit_review(header: dict, rows: list[dict], save: bool, override: bool) -> None:
    """Re-validate the edited form and, if asked, save it; a save failure lands in S.save_error."""
    S.current = review.revalidate(header, rows, review.today())
    S.save_error = None
    if not save:
        return
    try:
        if S.get("edit_id") is not None:
            S.saved_id = review.update_receipt(
                receipt_id=S.edit_id, current=S.current, ai_draft=S.ai_draft, user_override=override,
            )
        else:
            S.saved_id = review.save_receipt(
                current=S.current, ai_draft=S.ai_draft, audit_payload=S.audit_payload,
                image=S.processed, user_override=override,
            )
    except review.SaveError as exc:
        S.save_error = str(exc)
