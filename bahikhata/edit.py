"""Chat edits to saved line items: proposed in the chat, applied only on Approve.

Two phases, enforced by code -- the prompt is not the safety boundary:

1. propose_edits(question, session_id) -- for an Ask Your Ledger message. One LLM
   call (prompts/edit_v1.txt) reads the request into an EditPlan; the LLM never sees
   the ledger. Python then checks that every value the LLM returned is actually in
   the request (so it cannot widen the scope or invent a target), resolves each edit
   to exactly ONE line item with fixed, parameterised reads, and records a 'pending'
   row in line_item_edits. Nothing here writes to receipts, line_items or
   receipt_audit. Unclear, ambiguous and bulk requests get a question, not a guess;
   if any edit in a request can't be resolved, none of them is proposed.

2. apply_edit / cancel_edit(proposal_id, session_id) -- called ONLY from the page's
   Approve / Cancel buttons, never on the LLM path. They take nothing but the proposal
   id (and the server-side session id) and read the target and both values back from
   the stored proposal, so a tampered request cannot point it anywhere else. In one
   transaction apply_edit locks the proposal (so a double click waits, then sees it
   already applied), checks owner, status and expiry, compare-and-swaps the single
   cell against the whole line as it was when proposed (stale -> nothing changes),
   re-derives receipts.status and the audit flags with the same validator the review
   form uses, and marks the proposal applied. A proposal is applied at most once.

Derived values follow the existing rules: the validator detects and never corrects
(validate.py), so changing a quantity does not change the amount -- a mismatch shows
up as a V3 warning and receipts.status is re-derived from the flags.

There are no user accounts in this MVP (single user): a proposal belongs to the chat
session that created it (Streamlit session state, held server-side).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import psycopg
from pydantic import ValidationError

from bahikhata import config, db, llm_client
from bahikhata.extraction import diff_fields
from bahikhata.normalize import format_npr, parse_amount_to_paisa, parse_quantity
from bahikhata.review import receipt_to_draft
from bahikhata.schemas import EditField, EditPlan, EditProposal, LineItemEditRequest, QueryResult, ReceiptDraft
from bahikhata.validate import validate_draft

FIELD_LABELS: dict[EditField, str] = {
    "description": "Description", "quantity": "Quantity", "unit_price": "Unit price", "amount": "Amount",
}
_STATUS_TEXT = {"clean": "clean", "needs_review": "needs review", "invalid": "invalid"}

_EXAMPLE = "e.g. “Change the quantity of rice on receipt #12 from 2 to 5”"
_COULD_NOT_READ = f"I couldn't work out that change — try rephrasing it, {_EXAMPLE}."
_GENERIC_CLARIFICATION = f"Which line, which field and what new value? For example: {_EXAMPLE}."
_UNSUPPORTED = (
    "I can only change line items you name one at a time: their description, quantity, unit price "
    "or amount. Bulk changes, adding or deleting lines, and bill-level fields (merchant, date, totals, "
    "VAT, category) aren't supported here — use Edit on the Dashboard for those."
)
_NOT_IN_REQUEST = (
    "I couldn't match that to the exact item and values you wrote, so I haven't proposed anything. "
    f"Please name the item, the field and the new value, {_EXAMPLE}."
)
_NEEDS_TARGET = f"Which item should I change? Name it or give its line number, {_EXAMPLE}."
_DB_UNREACHABLE = "I couldn't reach your ledger database right now — nothing was changed. Please try again."

# --- Request gate ---------------------------------------------------------------

_EDIT_WORDS = re.compile(r"\b(change|update|set|edit|correct|fix|modify|replace|rename)\b", re.IGNORECASE)


def looks_like_edit(question: str) -> bool:
    """Cheap word check before spending an LLM call on the edit planner. A false positive
    only costs that call: the planner answers "not_edit" and the question is answered as usual."""
    return bool(_EDIT_WORDS.search(question))


# --- Phase 1: propose -----------------------------------------------------------

def _get_plan(question: str, *, client=None) -> tuple[EditPlan | None, str | None]:
    """One LLM call -> (plan, None), or (None, user-safe message). Cached by the question."""
    prompt = llm_client.load_prompt(config.EDIT_PROMPT).replace("{question}", question)
    try:
        response = llm_client.call_json(
            prompt, namespace="edit", prompt_version=config.EDIT_PROMPT,
            cache_input=question, response_schema=EditPlan, client=client,
        )
    except llm_client.LLMError as exc:
        return None, exc.user_message
    try:
        return EditPlan.model_validate_json(response.text), None
    except ValidationError:
        return None, _COULD_NOT_READ


def _blank_to_none(text: str | None) -> str | None:
    return text.strip() if text is not None and text.strip() else None


def _clean(edit: LineItemEditRequest) -> LineItemEditRequest:
    return edit.model_copy(update={
        "merchant": _blank_to_none(edit.merchant), "invoice_number": _blank_to_none(edit.invoice_number),
        "item": _blank_to_none(edit.item), "current_value": _blank_to_none(edit.current_value),
        "new_value": edit.new_value.strip(),
    })


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def _mentioned(text: str, request: str) -> bool:
    return _folded(text) in _folded(request)


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> set[str]:
    """Numeric tokens, grouping commas stripped (same idea as ask.check_explanation_numbers)."""
    return {match.replace(",", "") for match in _NUMBER_RE.findall(text)}


def outside_request(edit: LineItemEditRequest, request: str) -> bool:
    """True if the LLM returned a target or value the request doesn't contain. Pure function.

    Text (merchant, invoice, item, a description) must appear in the request; ids and
    numeric values must be numbers the user typed. This is what stops a hallucinated or
    over-eager plan from proposing a change to a line or value nobody asked about.
    """
    texts = [edit.merchant, edit.invoice_number, edit.item]
    if edit.field == "description":
        texts += [edit.new_value, edit.current_value]
    if any(text is not None and not _mentioned(text, request) for text in texts):
        return True
    numbers = _numbers_in(request)
    if any(n is not None and str(n) not in numbers for n in (edit.receipt_id, edit.line_no)):
        return True
    if edit.field != "description":
        for value in (edit.new_value, edit.current_value):
            if value is not None and not (_numbers_in(value) and _numbers_in(value) <= numbers):
                return True
    return False


def parse_value(field: EditField, text: str) -> Any:
    """The user's text for `field` -> the typed cell value, or None if it isn't a valid one.
    Same parsers as the review form: quantity Decimal (> 0), money integer paisa (>= 0)."""
    if field == "quantity":
        quantity = parse_quantity(text)
        return quantity if quantity is not None and quantity > 0 else None
    if field in ("unit_price", "amount"):
        return parse_amount_to_paisa(text)
    description = " ".join(text.split())
    return description if 0 < len(description) <= config.MAX_DESCRIPTION_CHARS else None


def _cell(row: dict[str, Any], field: EditField) -> Any:
    """The line item's current value for `field`, typed like parse_value's result."""
    value = row[db.LINE_ITEM_EDIT_COLUMNS[field]]
    if field == "quantity" and value is not None:
        return Decimal(f"{value:.7g}")  # REAL is float4: ~7 significant digits, so 0.1 stays 0.1
    return value


def _same(field: EditField, a: Any, b: Any) -> bool:
    if field == "description" and a is not None and b is not None:
        return _folded(a) == _folded(b)
    return a == b


def display_value(field: EditField, value: Any) -> str:
    """A cell value as shown to the user. Money is formatted by code, never by the LLM."""
    if value is None:
        return "(empty)"
    if field == "quantity":
        return format(Decimal(value).normalize(), "f")
    if field in ("unit_price", "amount"):
        return format_npr(value)
    return f"“{value}”"


def _to_json(field: EditField, value: Any) -> Any:
    return str(value) if field == "quantity" and value is not None else value


def _from_json(field: EditField, value: Any) -> Any:
    return Decimal(value) if field == "quantity" and value is not None else value


def _line_summary(row: dict[str, Any]) -> str:
    parts = [row["description"] or "(no description)"]
    for field in ("quantity", "unit_price", "amount"):
        parts.append(f"{FIELD_LABELS[field].lower()} {display_value(field, _cell(row, field))}")
    return ", ".join(parts)


def _candidate(row: dict[str, Any]) -> str:
    return f"receipt #{row['receipt_id']} ({row['merchant_name']}) line {row['line_no']}: {_line_summary(row)}"


def _target(edit: LineItemEditRequest) -> str:
    what = f"“{edit.item}”" if edit.item else f"line {edit.line_no}"
    if edit.item and edit.line_no is not None:
        what = f"line {edit.line_no} (“{edit.item}”)"
    where = [f"receipt #{edit.receipt_id}" if edit.receipt_id is not None else None,
             f"the {edit.merchant} bill" if edit.merchant else None,
             f"invoice {edit.invoice_number}" if edit.invoice_number else None]
    where = [w for w in where if w]
    return what + (" on " + ", ".join(where) if where else "")


def _resolve(conn: psycopg.Connection, edit: LineItemEditRequest) -> tuple[dict[str, Any] | None, str | None]:
    """Exactly one line item for `edit` -> (row, None); otherwise (None, a question for the user).
    Never picks one of several matches."""
    rows = db.find_line_items(
        conn, receipt_id=edit.receipt_id, merchant=edit.merchant, invoice_number=edit.invoice_number,
        item=edit.item, line_no=edit.line_no, limit=config.MAX_EDIT_CANDIDATES,
    )
    target = _target(edit)
    if not rows:
        return None, f"I couldn't find {target} in your saved receipts, so nothing was proposed."
    label = FIELD_LABELS[edit.field].lower()
    if edit.current_value is not None:
        current = parse_value(edit.field, edit.current_value)
        matching = [r for r in rows if current is not None and _same(edit.field, _cell(r, edit.field), current)]
        if not matching:
            found = "; ".join(_candidate(r) for r in rows[:5])
            return None, (f"No line matching {target} has {label} {edit.current_value} right now. "
                          f"I found: {found}. Nothing was proposed — check the value and ask again.")
        rows = matching
    if len(rows) > 1:
        shown = "; ".join(_candidate(r) for r in rows[:5])
        more = " (and more)" if len(rows) > 5 else ""
        return None, (f"{target[:1].upper()}{target[1:]} matches more than one line: {shown}{more}. "
                      "Which one should I change? Say the receipt number and line, e.g. “receipt #12 line 1”.")
    return rows[0], None


def propose_edits(
    question: str, session_id: str, *, now: datetime | None = None, client=None,
    connect: Callable[[], psycopg.Connection] | None = None,
) -> QueryResult | None:
    """An edit request -> a QueryResult carrying pending EditProposals (or a question / refusal).

    Returns None when the message isn't an edit request, so the caller answers it as a
    question instead. Never changes a bill: the only write is the 'pending' proposal rows.
    """
    base = {"question": question, "prompt_version": config.EDIT_PROMPT, "model_name": config.MODEL_NAME}
    stripped = question.strip()
    if not stripped or len(question) > config.MAX_QUESTION_CHARS:
        return None  # ask.answer_question owns the input-check messages

    plan, error = _get_plan(stripped, client=client)
    if plan is None:
        return QueryResult(**base, message=error)
    if plan.status == "not_edit":
        return None
    if plan.status == "unsupported":
        return QueryResult(**base, message=_UNSUPPORTED)
    if plan.status == "ambiguous" or not plan.edits:
        return QueryResult(**base, message=_blank_to_none(plan.clarification) or _GENERIC_CLARIFICATION)

    edits = [_clean(e) for e in plan.edits]
    if len(edits) > config.MAX_EDITS_PER_REQUEST:
        return QueryResult(**base, message=f"Please ask for at most {config.MAX_EDITS_PER_REQUEST} changes at a time.")
    if any(e.item is None and e.line_no is None for e in edits):
        return QueryResult(**base, message=_NEEDS_TARGET)
    if any(outside_request(e, stripped) for e in edits):
        return QueryResult(**base, message=_NOT_IN_REQUEST)
    new_values = [parse_value(e.field, e.new_value) for e in edits]
    for edit, value in zip(edits, new_values):
        if value is None:
            return QueryResult(**base, message=(
                f"“{edit.new_value}” isn't a valid {FIELD_LABELS[edit.field].lower()}, so nothing was proposed."))

    now = now or datetime.now(timezone.utc)
    try:
        with (connect or db.get_connection)() as conn:
            resolved = []
            for edit, value in zip(edits, new_values):
                row, problem = _resolve(conn, edit)
                if problem:
                    return QueryResult(**base, message=problem)
                if _same(edit.field, _cell(row, edit.field), value):
                    return QueryResult(**base, message=(
                        f"The {FIELD_LABELS[edit.field].lower()} of receipt #{row['receipt_id']} line "
                        f"{row['line_no']} is already {display_value(edit.field, value)}, so there's nothing to change."))
                resolved.append((edit.field, row, value))
            if len({(row["line_item_id"], field) for field, row, _ in resolved}) < len(resolved):
                return QueryResult(**base, message="That asks to change the same value more than once — "
                                                   "please ask for one new value per field.")

            expires_at = now + timedelta(minutes=config.EDIT_PROPOSAL_TTL_MINUTES)
            records, proposals = [], []
            for field, row, value in resolved:
                proposal_id = uuid.uuid4()
                current = _cell(row, field)
                records.append({
                    "id": proposal_id, "session_id": session_id, "receipt_id": row["receipt_id"],
                    "line_item_id": row["line_item_id"], "line_no": row["line_no"], "field": field,
                    "row_snapshot": {column: row[column] for column in db.SNAPSHOT_COLUMNS},
                    "old_value": _to_json(field, current), "new_value": _to_json(field, value),
                    "request_text": stripped, "expires_at": expires_at,
                })
                proposals.append(EditProposal(
                    proposal_id=str(proposal_id), receipt_id=row["receipt_id"], merchant_name=row["merchant_name"],
                    invoice_number=row["invoice_number"], date_ad=row["date_ad"], line_no=row["line_no"],
                    item=row["description"], field=field,
                    current_text=display_value(field, current), new_text=display_value(field, value),
                ))
            db.insert_edit_proposals(conn, records)
    except (psycopg.Error, RuntimeError):  # unreachable DB / DATABASE_URL missing
        return QueryResult(**base, message=_DB_UNREACHABLE)
    return QueryResult(**base, edits=proposals)


# --- Phase 2: approve / cancel (page buttons only) ---------------------------------

@dataclass(frozen=True)
class EditOutcome:
    """What happened when the user decided on a proposal. `message` is safe to show."""

    status: str  # "applied" | "cancelled" | "already_decided" | "expired" | "stale" | "unavailable" | "failed"
    message: str

    @property
    def applied(self) -> bool:
        return self.status == "applied"


_UNAVAILABLE = EditOutcome("unavailable", "This change request isn't available any more. Nothing was changed.")
_EXPIRED = EditOutcome("expired", "This change request expired, so nothing was changed. "
                                  "Ask again to review the latest values.")
_FAILED = EditOutcome("failed", "The update was not completed — nothing was changed. Please try again.")
_ALREADY = {
    "applied": EditOutcome("already_decided", "This change has already been applied."),
    "cancelled": EditOutcome("already_decided", "This change was cancelled, so nothing was changed."),
    "stale": EditOutcome("already_decided", "This change wasn't applied because the line had changed. "
                                            "Ask again to review the latest values."),
    "expired": _EXPIRED,
}


def _proposal_uuid(proposal_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(proposal_id)
    except (ValueError, TypeError, AttributeError):
        return None


def _rederive(conn: psycopg.Connection, receipt_id: int, today: date) -> tuple[str, str]:
    """Re-run the validator on the edited receipt; store the derived status and audit flags.
    Same draft-from-saved-receipt path as the Dashboard's Edit. Returns (old, new) status."""
    receipt = db.get_receipt(conn, receipt_id)
    draft = receipt_to_draft(receipt)
    flags, status, _, _ = validate_draft(draft, today)
    ai_draft_json = db.get_ai_draft_json(conn, receipt_id)
    ai_draft = ReceiptDraft.model_validate(ai_draft_json) if ai_draft_json else ReceiptDraft()
    db.set_receipt_status(conn, receipt_id, status)
    db.refresh_audit_flags(conn, receipt_id, flags_at_save=[f.model_dump() for f in flags],
                           edited_fields=diff_fields(ai_draft, draft))
    return receipt["status"], status


def _stale(proposal: dict[str, Any], now_row: dict[str, Any] | None) -> EditOutcome:
    where = f"line {proposal['line_no']} on receipt #{proposal['receipt_id']}"
    if now_row is None:
        return EditOutcome("stale", f"Nothing was changed: {where} no longer exists (the receipt may have been "
                                    "edited or deleted since). Ask again to review the latest values.")
    return EditOutcome("stale", f"Nothing was changed: {where} was modified after this was proposed. "
                                f"It now reads: {_line_summary(now_row)}. Ask again if you still want the change.")


def apply_edit(
    proposal_id: str, session_id: str, *, now: datetime | None = None, today: date | None = None,
    connect: Callable[[], psycopg.Connection] | None = None,
) -> EditOutcome:
    """Apply one approved proposal, exactly as stored; at most once. Never raises."""
    pid = _proposal_uuid(proposal_id)
    if pid is None:
        return _UNAVAILABLE
    now = now or datetime.now(timezone.utc)
    try:
        with (connect or db.get_connection)() as conn, conn.transaction():
            proposal = db.lock_edit_proposal(conn, pid, session_id)
            if proposal is None:
                return _UNAVAILABLE
            if proposal["status"] != "pending":
                return _ALREADY[proposal["status"]]
            if proposal["expires_at"] <= now:
                db.set_edit_proposal_status(conn, pid, "expired", now)
                return _EXPIRED
            field = proposal["field"]
            new_value = _from_json(field, proposal["new_value"])
            changed = db.update_line_item_field(
                conn, receipt_id=proposal["receipt_id"], line_item_id=proposal["line_item_id"], field=field,
                new_value=float(new_value) if field == "quantity" else new_value,  # REAL column
                snapshot=proposal["row_snapshot"],
            )
            if not changed:
                db.set_edit_proposal_status(conn, pid, "stale", now)
                return _stale(proposal, db.get_line_item(conn, proposal["receipt_id"], proposal["line_item_id"]))
            old_status, new_status = _rederive(conn, proposal["receipt_id"], today or date.today())
            db.set_edit_proposal_status(conn, pid, "applied", now)
    except (psycopg.Error, RuntimeError, LookupError):
        return _FAILED
    item = proposal["row_snapshot"]["description"]
    message = (f"Updated receipt #{proposal['receipt_id']}, line {proposal['line_no']}"
               f"{f' ({item})' if item else ''}: {FIELD_LABELS[field].lower()} "
               f"{display_value(field, _from_json(field, proposal['old_value']))} → {display_value(field, new_value)}.")
    if new_status != old_status:
        message += f" The receipt's check status is now “{_STATUS_TEXT[new_status]}”."
    return EditOutcome("applied", message)


def cancel_edit(
    proposal_id: str, session_id: str, *, now: datetime | None = None,
    connect: Callable[[], psycopg.Connection] | None = None,
) -> EditOutcome:
    """Decline a proposal: nothing on the bill changes, and it can't be applied later. Never raises."""
    pid = _proposal_uuid(proposal_id)
    if pid is None:
        return _UNAVAILABLE
    try:
        with (connect or db.get_connection)() as conn, conn.transaction():
            proposal = db.lock_edit_proposal(conn, pid, session_id)
            if proposal is None:
                return _UNAVAILABLE
            if proposal["status"] != "pending":
                return _ALREADY[proposal["status"]]
            db.set_edit_proposal_status(conn, pid, "cancelled", now or datetime.now(timezone.utc))
    except (psycopg.Error, RuntimeError, LookupError):
        return EditOutcome("failed", "Couldn't record the cancellation right now. Nothing was changed.")
    return EditOutcome("cancelled", "Cancelled — nothing was changed.")
