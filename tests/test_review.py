"""TASK-017/018: review helpers (form <-> draft, re-validation, save rules). No network, no DB."""

import io
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from PIL import Image

from bahikhata import config, review
from bahikhata.images import process_upload
from bahikhata.schemas import Category, ReceiptDraft, ReceiptExtraction

TODAY = date(2026, 10, 3)

HEADER = {
    "merchant_name": "Shree Traders", "merchant_pan": "123456789", "invoice_number": "INV-1",
    "date_raw": "2083/06/14", "calendar": "BS", "category": "Inventory",
    "subtotal": "1,000.00", "discount": "", "service_charge": "", "vat": "130", "total": "Rs. 1,130/-",
}
ROWS = [{"description": "Rice", "quantity": "2", "unit_price": "500", "amount": "1000"}]
AUDIT = {
    "model_name": "gemini-3.5-flash-lite", "prompt_version": "extraction_v1", "image_sha256": "a" * 64,
    "raw_response": "{}", "ai_extraction_json": {}, "ai_draft_json": {}, "flags_at_extraction_json": [],
    "extracted_at": None,
}


@pytest.fixture
def image(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    buf = io.BytesIO()
    Image.new("RGB", (30, 20), "white").save(buf, format="PNG")
    return process_upload(buf.getvalue())


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_paisa_to_text():
    assert [review.paisa_to_text(p) for p in (125050, 5, 0, -1250, None)] == ["1250.50", "0.05", "0.00", "-12.50", ""]


def test_revalidate_parses_text_with_the_same_normalizer():
    current = review.revalidate(HEADER, ROWS, TODAY)
    assert current.flags == [] and current.status == "clean" and current.can_save
    d = current.draft
    assert (d.subtotal_paisa, d.vat_paisa, d.total_paisa) == (100000, 13000, 113000)
    assert d.date_bs == "2083-06-14" and d.category is Category.INVENTORY
    assert d.line_items[0].quantity == Decimal("2")


def test_revalidate_reports_unparseable_amount_inline_field():
    current = review.revalidate({**HEADER, "total": "11.30.00"}, ROWS, TODAY)
    assert [(f.rule_id, f.field) for f in current.flags] == [("N1", "total_paisa"), ("V1", "total_paisa")]
    assert not current.can_save


def test_revalidate_does_not_modify_its_inputs():
    header, rows = dict(HEADER), [dict(r) for r in ROWS]
    review.revalidate(header, rows, TODAY)
    assert header == HEADER and rows == ROWS


def test_blank_and_nan_rows_are_dropped():
    rows = ROWS + [{"description": None, "quantity": float("nan"), "unit_price": "", "amount": "  "}]
    assert len(review.form_to_extraction(HEADER, rows).line_items) == 1


def test_draft_to_form_round_trip():
    draft = review.revalidate(HEADER, ROWS, TODAY).draft
    header, rows = review.draft_to_form(draft)
    again = review.revalidate(header, rows, TODAY).draft
    assert again.model_dump(exclude={"date_raw", "date_calendar_hint"}) == draft.model_dump(
        exclude={"date_raw", "date_calendar_hint"})
    assert header["total"] == "1130.00" and rows[0]["amount"] == "1000.00"


def test_draft_to_form_shows_ai_text_for_unparseable_amounts():
    extraction = ReceiptExtraction(total_raw="12.50.00", line_items=[{"amount_raw": "abc"}])
    draft = ReceiptDraft(line_items=[{}])
    header, rows = review.draft_to_form(draft, extraction)
    assert header["total"] == "12.50.00" and rows[0]["amount"] == "abc"
    assert header["category"] is None and header["calendar"] == "unknown"


def test_save_success_writes_image_and_builds_audit(image, monkeypatch):
    captured = {}

    def fake_save(conn, confirmed, image_path, audit):
        captured.update(confirmed=confirmed, image_path=image_path, audit=audit)
        return 42

    ai_draft = review.revalidate({**HEADER, "total": "1,120"}, ROWS, TODAY).draft
    current = review.revalidate(HEADER, ROWS, TODAY)
    monkeypatch.setattr("bahikhata.db.save_confirmed_receipt", fake_save)
    receipt_id = review.save_receipt(current=current, ai_draft=ai_draft, audit_payload=AUDIT,
                                     image=image, user_override=False, connect=FakeConnection)
    assert receipt_id == 42
    assert captured["confirmed"].status == "clean" and captured["confirmed"].user_override is False
    assert captured["audit"].edited_fields_json == ["total_paisa"]
    assert captured["audit"].flags_at_save_json == []
    assert (config.IMAGES_DIR / f"{image.processed_sha256}.jpg").read_bytes() == image.processed_bytes
    assert captured["image_path"].endswith(f"{image.processed_sha256}.jpg")


def test_save_refused_when_blocking(image):
    current = review.revalidate({**HEADER, "total": ""}, ROWS, TODAY)
    with pytest.raises(review.SaveError, match="⛔"):
        review.save_receipt(current=current, ai_draft=ReceiptDraft(), audit_payload=AUDIT, image=image,
                            user_override=True, connect=FakeConnection)
    assert not config.IMAGES_DIR.exists()  # nothing written


def test_v5_needs_override_and_override_is_stored(image, monkeypatch):
    current = review.revalidate({**HEADER, "total": "1,220"}, ROWS, TODAY)
    assert current.needs_override
    with pytest.raises(review.SaveError, match="save anyway"):
        review.save_receipt(current=current, ai_draft=ReceiptDraft(), audit_payload=AUDIT, image=image,
                            user_override=False, connect=FakeConnection)
    saved = {}
    monkeypatch.setattr("bahikhata.db.save_confirmed_receipt", lambda c, r, p, a: saved.setdefault("r", r) and 7)
    review.save_receipt(current=current, ai_draft=ReceiptDraft(), audit_payload=AUDIT, image=image,
                        user_override=True, connect=FakeConnection)
    assert saved["r"].user_override is True and saved["r"].status == "invalid"


def test_override_ignored_when_not_needed(image, monkeypatch):
    saved = {}
    monkeypatch.setattr("bahikhata.db.save_confirmed_receipt", lambda c, r, p, a: saved.setdefault("r", r) and 1)
    review.save_receipt(current=review.revalidate(HEADER, ROWS, TODAY), ai_draft=ReceiptDraft(),
                        audit_payload=AUDIT, image=image, user_override=True, connect=FakeConnection)
    assert saved["r"].user_override is False


def test_missing_database_url_is_a_save_error(image, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(review.SaveError, match="DATABASE_URL"):
        review.save_receipt(current=review.revalidate(HEADER, ROWS, TODAY), ai_draft=ReceiptDraft(),
                            audit_payload=AUDIT, image=image, user_override=False)


def test_db_error_is_a_save_error_without_details(image):
    def broken_connect():
        raise psycopg.OperationalError("connection to server at secret-host.neon.tech failed")

    with pytest.raises(review.SaveError) as info:
        review.save_receipt(current=review.revalidate(HEADER, ROWS, TODAY), ai_draft=ReceiptDraft(),
                            audit_payload=AUDIT, image=image, user_override=False, connect=broken_connect)
    assert "OperationalError" in str(info.value) and "secret-host" not in str(info.value)


@pytest.mark.parametrize("flag_field, form_field", [
    ("total_paisa", "total"), ("vat_paisa", "vat"), ("service_charge_paisa", "service_charge"),
    ("date_ad", "date"), ("merchant_name", "merchant_name"), ("merchant_pan", "merchant_pan"),
    ("category", "category"), ("line_items[2].amount_paisa", "line_items"), (None, None),
])
def test_form_field_for(flag_field, form_field):
    assert review.form_field_for(flag_field) == form_field


def test_save_hint():
    assert review.save_hint(review.revalidate(HEADER, ROWS, TODAY)) is None
    blocked = review.revalidate({**HEADER, "total": "", "category": None}, ROWS, TODAY)
    assert review.save_hint(blocked) == "Can't save yet: total is missing; choose a category."
    v5 = review.revalidate({**HEADER, "total": "1,220"}, ROWS, TODAY)
    assert "save anyway" in review.save_hint(v5)
