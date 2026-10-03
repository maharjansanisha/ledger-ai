"""Receipt extraction orchestration (ARCHITECTURE.md §4, TASK-015, first part).

extract_receipt(): processed image bytes -> ReceiptExtraction via the LLM client,
with the A§4 failure path: if the response is not valid JSON for the schema, retry
once with the error appended; if it fails again, return no extraction plus a
BLOCKING EXTRACTION_FAILED flag, keeping the raw text for the audit. Provider
errors (429, 5xx, network) also become EXTRACTION_FAILED with a user-safe message,
so the UI never crashes and the user can type the receipt in by hand.

Still to come in TASK-015, once the validator (TASK-010) and image intake
(TASK-012) exist: extract_draft() = intake -> extract_receipt -> normalize ->
validate, and diff_fields().
"""

import hashlib
from dataclasses import dataclass, field

from pydantic import ValidationError

from bahikhata import config, llm_client
from bahikhata.schemas import ReceiptExtraction, ValidationFlag

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
