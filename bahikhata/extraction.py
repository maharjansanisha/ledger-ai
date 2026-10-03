"""Receipt extraction orchestration (ARCHITECTURE.md §4, §11, TASK-015).

extract_draft(): the one function the UI and the eval both call:
upload bytes -> intake -> extract_receipt -> normalize -> validate.

extract_receipt(): processed image bytes -> ReceiptExtraction via the LLM client,
with the A§4 failure path: if the response is not valid JSON for the schema, retry
once with the error appended; if it fails again, return no extraction plus a
BLOCKING EXTRACTION_FAILED flag, keeping the raw text for the audit. Provider
errors (429, 5xx, network) also become EXTRACTION_FAILED with a user-safe message,
so the UI never crashes and the user can type the receipt in by hand.

diff_fields(): which fields the human changed, for receipt_audit.edited_fields_json.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError

from bahikhata import config, images, llm_client
from bahikhata.normalize import normalize_extraction
from bahikhata.schemas import ConfirmedReceipt, ReceiptDraft, ReceiptExtraction, ReceiptStatus, ValidationFlag
from bahikhata.validate import summarize_flags, validate_draft

_RETRY_NOTE = (
    "Your previous answer could not be parsed: {error}\n"
    "Return only a JSON object matching the schema. Use null for anything unreadable."
)


@dataclass
class ExtractionResult:
    extraction: ReceiptExtraction | None   # None if extraction failed
    raw_response: str | None               # last raw model text (None if no response at all)
    flags: list[ValidationFlag] = field(default_factory=list)
    model_name: str = ""
    prompt_version: str = ""
    image_sha256: str = ""
    cache_hit: bool = False                # True only if every call came from the cache
    attempts: int = 0


def _failed(message: str) -> ValidationFlag:
    return ValidationFlag(rule_id="EXTRACTION_FAILED", severity="BLOCKING", field=None, message=message)


def _short(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'response'}: {e['msg']}" for e in error.errors()[:5]
    )


def extract_receipt(image_bytes: bytes, *, client=None) -> ExtractionResult:
    """Processed (EXIF-rotated, resized) JPEG bytes -> ExtractionResult. Never raises for
    provider or parse failures; those come back as an EXTRACTION_FAILED flag."""
    result = ExtractionResult(
        extraction=None,
        raw_response=None,
        model_name=config.MODEL_NAME,
        prompt_version=config.EXTRACTION_PROMPT,
        image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        cache_hit=True,
    )
    extra_instruction = ""
    for attempt in (1, 2):
        result.attempts = attempt
        try:
            response = llm_client.call_extraction(
                image_bytes, ReceiptExtraction, extra_instruction=extra_instruction, client=client
            )
        except llm_client.LLMError as exc:
            result.cache_hit = False
            result.flags = [_failed(f"EXTRACTION_FAILED: {exc.user_message}")]
            return result

        result.raw_response = response.text
        result.cache_hit = result.cache_hit and response.cache_hit
        try:
            result.extraction = ReceiptExtraction.model_validate_json(response.text)
            return result
        except ValidationError as exc:
            extra_instruction = _RETRY_NOTE.format(error=_short(exc))

    result.flags = [_failed(
        "EXTRACTION_FAILED: the AI response could not be read twice; enter the receipt by hand"
    )]
    return result


def extract_draft(
    image_bytes: bytes, today: date, *, client=None
) -> tuple[ReceiptDraft, list[ValidationFlag], ReceiptStatus, dict[str, Any]]:
    """Upload bytes -> (draft, flags, status, audit_payload).

    Raises images.ImageIntakeError (user-safe message) for an unusable file, before
    any API call. Every other failure comes back as flags: on EXTRACTION_FAILED the
    draft is empty so the user can type the receipt in.

    flags = EXTRACTION_FAILED (if any) + normalizer N-flags + validator V-flags.
    audit_payload holds the receipt_audit columns known at extraction time (keys
    match db.AuditRecord); flags_at_save_json, edited_fields_json and confirmed_at
    are added at save time (TASK-018). `today` is passed in for V8 (A§2 P10).
    """
    image = images.process_upload(image_bytes)
    result = extract_receipt(image.processed_bytes, client=client)
    if result.extraction is None:
        draft, n_flags = ReceiptDraft(), []
    else:
        draft, n_flags = normalize_extraction(result.extraction)
    v_flags, *_ = validate_draft(draft, today)
    flags = [*result.flags, *n_flags, *v_flags]
    status, _, _ = summarize_flags(flags)

    audit_payload = {
        "model_name": result.model_name,
        "prompt_version": result.prompt_version,
        "image_sha256": image.original_sha256,
        "raw_response": result.raw_response,
        "ai_extraction_json": result.extraction.model_dump(mode="json") if result.extraction else None,
        "ai_draft_json": draft.model_dump(mode="json") if result.extraction else None,
        "flags_at_extraction_json": [f.model_dump() for f in flags],
        "extracted_at": datetime.now(timezone.utc),
    }
    return draft, flags, status, audit_payload


_HEADER_FIELDS = [f for f in ConfirmedReceipt.model_fields if f in ReceiptDraft.model_fields and f != "line_items"]


def _comparable(value):
    if isinstance(value, str):
        return value.strip() or None  # whitespace-only differences are not edits
    return value


def _line_tuple(item) -> tuple:
    return (_comparable(item.description), item.quantity, item.unit_price_paisa, item.amount_paisa)


def diff_fields(ai_draft: ReceiptDraft, final: ConfirmedReceipt | ReceiptDraft) -> list[str]:
    """Names of header fields whose value changed, plus "line_items" if any line differs (A§11).

    Pass ReceiptDraft() as ai_draft when extraction failed: every field the user filled counts.
    """
    edited = [
        name for name in _HEADER_FIELDS
        if _comparable(getattr(ai_draft, name)) != _comparable(getattr(final, name))
    ]
    if [_line_tuple(i) for i in ai_draft.line_items] != [_line_tuple(i) for i in final.line_items]:
        edited.append("line_items")
    return edited
