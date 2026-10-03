"""TASK-015: extract_draft (intake -> LLM -> normalize -> validate) and diff_fields. No network."""

import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from PIL import Image

from bahikhata import config
from bahikhata.db import AuditRecord
from bahikhata.extraction import diff_fields, extract_draft
from bahikhata.images import ImageIntakeError
from bahikhata.schemas import Category, ConfirmedReceipt, ReceiptDraft
from tests.fakes import FakeClient

TODAY = date(2026, 10, 3)

GOOD = {
    "merchant_name": "Shree Traders",
    "merchant_pan": "123456789",
    "date_raw": "2083/06/14",
    "date_calendar_hint": "BS",
    "subtotal_raw": "Rs. 1,000.00",
    "vat_amount_raw": "130.00",
    "total_raw": "1,130/-",
    "category": "Inventory",
    "line_items": [{"description": "Rice", "quantity_raw": "2", "unit_price_raw": "500", "amount_raw": "1,000"}],
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "LLM_RETRY_WAIT_SECONDS", 0)


def upload(color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 30), color).save(buf, format="PNG")
    return buf.getvalue()


def response(**overrides) -> str:
    return json.dumps({**GOOD, **overrides})


def rules(flags) -> list[str]:
    return [f.rule_id for f in flags]


def test_clean_receipt_end_to_end():
    draft, flags, status, audit = extract_draft(upload(), TODAY, client=FakeClient(response()))
    assert flags == [] and status == "clean"
    assert draft.merchant_name == "Shree Traders"
    assert draft.total_paisa == 113000 and draft.subtotal_paisa == 100000 and draft.vat_paisa == 13000
    assert draft.date_bs == "2083-06-14" and draft.date_ad is not None and draft.bs_month == 6
    assert draft.category is Category.INVENTORY
    assert draft.line_items[0].quantity == Decimal("2") and draft.line_items[0].amount_paisa == 100000


def test_audit_payload_matches_audit_record_columns():
    data = upload()
    _, _, _, audit = extract_draft(data, TODAY, client=FakeClient(response()))
    assert audit["model_name"] == config.MODEL_NAME and audit["prompt_version"] == "extraction_v1"
    assert len(audit["image_sha256"]) == 64
    assert audit["raw_response"] == response()
    assert audit["ai_extraction_json"]["total_raw"] == "1,130/-"       # what the AI said
    assert audit["ai_draft_json"]["total_paisa"] == 113000              # after normalization
    assert audit["flags_at_extraction_json"] == []
    # TASK-018 adds the save-time columns and gets a valid AuditRecord.
    AuditRecord(**audit, flags_at_save_json=[], edited_fields_json=[], confirmed_at=datetime.now(timezone.utc))
    json.dumps(audit["ai_draft_json"])  # JSON-serialisable for the JSONB column


def test_normalizer_and_validator_flags_are_combined():
    draft, flags, status, _ = extract_draft(
        upload(), TODAY, client=FakeClient(response(total_raw="12.50.00", date_raw="03/04/2026"))
    )
    assert draft.total_paisa is None
    assert rules(flags) == ["N1", "N3", "V1"]   # unreadable total -> N1 then V1 blocks
    assert status == "invalid"


def test_v5_mismatch_gives_overridable_error():
    _, flags, status, _ = extract_draft(upload(), TODAY, client=FakeClient(response(total_raw="1,220.00")))
    assert [(f.rule_id, f.severity) for f in flags] == [("V5", "ERROR")]
    assert status == "invalid"


def test_category_outside_enum_becomes_none_and_v9():
    draft, flags, _, audit = extract_draft(upload(), TODAY, client=FakeClient(response(category="Groceries")))
    assert draft.category is None and rules(flags) == ["V9"]
    assert audit["ai_extraction_json"]["category"] == "Groceries"  # kept for the audit


def test_extraction_failure_gives_empty_draft_and_keeps_raw_text():
    draft, flags, status, audit = extract_draft(upload(), TODAY, client=FakeClient("nope", "still nope"))
    assert draft == ReceiptDraft()
    assert rules(flags)[0] == "EXTRACTION_FAILED"
    assert set(rules(flags)[1:]) == {"V1", "V9"}
    assert status == "invalid"
    assert audit["raw_response"] == "still nope"
    assert audit["ai_extraction_json"] is None and audit["ai_draft_json"] is None


def test_bad_upload_raises_intake_error_before_any_api_call():
    client = FakeClient()
    with pytest.raises(ImageIntakeError):
        extract_draft(b"not an image", TODAY, client=client)
    assert client.calls == []


def test_second_extraction_of_same_upload_uses_cache():
    data = upload()
    extract_draft(data, TODAY, client=FakeClient(response()))
    no_network = FakeClient()
    draft, _, _, _ = extract_draft(data, TODAY, client=no_network)
    assert no_network.calls == [] and draft.total_paisa == 113000


# --- diff_fields -------------------------------------------------------------

def ai_draft() -> ReceiptDraft:
    return extract_draft(upload(), TODAY, client=FakeClient(response()))[0]


def confirm(draft: ReceiptDraft, **changes) -> ConfirmedReceipt:
    data = {**draft.model_dump(exclude={"date_raw", "date_calendar_hint"}), "status": "clean", **changes}
    return ConfirmedReceipt.model_validate(data)


def test_diff_no_edits():
    d = ai_draft()
    assert diff_fields(d, confirm(d)) == []


def test_diff_header_and_line_items():
    d = ai_draft()
    final = confirm(d, total_paisa=114000, merchant_pan=None,
                    line_items=[{"description": "Rice", "quantity": "2", "unit_price_paisa": 50000, "amount_paisa": 100100}])
    assert diff_fields(d, final) == ["merchant_pan", "total_paisa", "line_items"]


def test_diff_ignores_whitespace_and_equal_decimals():
    d = ai_draft().model_copy(update={"merchant_name": "Shree Traders  "})
    final = confirm(d, merchant_name="Shree Traders",
                    line_items=[{"description": " Rice ", "quantity": "2.0", "unit_price_paisa": 50000, "amount_paisa": 100000}])
    assert diff_fields(d, final) == []


def test_diff_removed_line_item_and_filled_category():
    d = ai_draft().model_copy(update={"category": None})
    assert diff_fields(d, confirm(d, category="Food", line_items=[])) == ["category", "line_items"]


def test_diff_after_failed_extraction_lists_everything_the_user_typed():
    final = ConfirmedReceipt.model_validate({
        "merchant_name": "Typed", "date_ad": "2026-09-30", "date_bs": "2083-06-14", "bs_year": 2083,
        "bs_month": 6, "total_paisa": 500, "category": "Food", "status": "clean",
    })
    assert diff_fields(ReceiptDraft(), final) == [
        "merchant_name", "date_ad", "date_bs", "bs_year", "bs_month", "total_paisa", "category",
    ]
