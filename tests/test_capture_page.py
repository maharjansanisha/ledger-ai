"""TASK-016/017/018: the Capture & Review page, driven by Streamlit's AppTest.

Real pipeline (intake -> extract -> normalize -> validate) with a FakeClient instead
of Gemini, a temp cache and image dir, and a fake DB connection. No network, no DB.
Not covered here: editing st.data_editor cells (AppTest cannot drive data_editor);
line-item edits are covered by tests/test_review.py instead.
"""

import io
import json
from datetime import date
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from bahikhata import config, llm_client, review
from tests.fakes import FakeClient, client_error

PAGE = str(Path(__file__).resolve().parent.parent / "pages" / "1_Capture_and_Review.py")
GOOD = {
    "merchant_name": "Shree Traders", "merchant_pan": "123456789", "date_raw": "2083/06/14",
    "date_calendar_hint": "BS", "subtotal_raw": "Rs. 1,000.00", "vat_amount_raw": "130.00",
    "total_raw": "1,130/-", "category": "Inventory",
    "line_items": [{"description": "Rice", "quantity_raw": "2", "unit_price_raw": "500", "amount_raw": "1,000"}],
}


def png(color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 30), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated cache/images, fixed today; returns a setter for the fake Gemini responses."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "LLM_RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(review, "today", lambda: date(2026, 10, 3))
    clients = []

    def respond(*outcomes):
        client = FakeClient(*outcomes)
        clients.append(client)
        monkeypatch.setattr(llm_client, "_default_client", lambda: client)
        return client

    return respond


def start(upload=None) -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.run()
    if upload is not None:
        at.file_uploader[0].set_value(("receipt.png", upload, "image/png"))
        at.run()
    return at


def extract(at: AppTest) -> AppTest:
    at.button(key="extract").click()
    at.run()
    return at


def save_button(at: AppTest):
    return next(b for b in at.button if b.label == "Confirm & Save")


def submit(at: AppTest, label: str) -> AppTest:
    next(b for b in at.button if b.label == label).click()
    at.run()
    return at


def text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown) + "\n" + "\n".join(c.value for c in at.caption)


def test_page_loads_without_upload(env):
    at = start()
    assert not at.exception
    assert "Upload a photo" in at.info[0].value


def test_upload_does_not_call_the_api_until_extract(env):
    client = env(json.dumps(GOOD))
    at = start(png())
    assert not at.exception and client.calls == []
    assert at.button(key="extract").label == "Extract"
    at.run()  # an unrelated rerun
    assert client.calls == []


def test_extract_populates_the_form(env):
    client = env(json.dumps(GOOD))
    at = extract(start(png()))
    assert not at.exception
    assert len(client.calls) == 1
    assert at.text_input(key="merchant_name_1").value == "Shree Traders"
    assert at.text_input(key="total_1").value == "1130.00"
    assert at.selectbox(key="category_1").value == "Inventory"
    assert "clean" in text(at) and "2083-06-14" in text(at)
    assert not save_button(at).disabled
    at.run()  # rerun after extraction: no second API call
    assert len(client.calls) == 1


def test_blocking_flag_disables_save(env):
    env(json.dumps({**GOOD, "total_raw": None}))
    at = extract(start(png()))
    assert "⛔ **BLOCKING**" in text(at) and "total is missing" in text(at)
    assert save_button(at).disabled


def test_revalidate_reparses_edits(env):
    env(json.dumps({**GOOD, "total_raw": None}))
    at = extract(start(png()))
    at.text_input(key="total_1").set_value("Rs. 1,130/-")
    at.text_input(key="date_raw_1").set_value("03/04/2026")
    submit(at, "Re-validate")
    assert not at.exception
    assert "total is missing" not in text(at)
    assert "2026-04-03" in text(at) and "N3" in text(at)          # date re-parsed, DD/MM warning
    assert not save_button(at).disabled
    assert at.text_input(key="total_1").value == "Rs. 1,130/-"    # the user's text is kept


def test_unparseable_amount_shows_inline_error(env):
    env(json.dumps(GOOD))
    at = extract(start(png()))
    at.text_input(key="vat_1").set_value("1.30.00")
    submit(at, "Re-validate")
    assert any("N1" in c.value and "1.30.00" in c.value for c in at.caption)


def test_v5_needs_save_anyway_checkbox(env, monkeypatch):
    env(json.dumps({**GOOD, "total_raw": "1,220"}))
    saved = {}
    monkeypatch.setattr("bahikhata.db.get_connection", lambda: _FakeConn())
    monkeypatch.setattr("bahikhata.db.save_confirmed_receipt", lambda c, r, p, a: saved.setdefault("r", r) and 9)
    at = extract(start(png()))
    assert "❗ **ERROR**" in text(at)
    submit(at, "Confirm & Save")                    # without the checkbox
    assert "save anyway" in at.error[0].value and "r" not in saved
    at.checkbox(key="override_1").check()
    submit(at, "Confirm & Save")
    assert "Saved receipt #9" in at.success[0].value
    assert saved["r"].user_override is True and saved["r"].status == "invalid"


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_save_success_shows_id(env, monkeypatch):
    env(json.dumps(GOOD))
    monkeypatch.setattr("bahikhata.db.get_connection", lambda: _FakeConn())
    monkeypatch.setattr("bahikhata.db.save_confirmed_receipt", lambda c, r, p, a: 42)
    at = extract(start(png()))
    submit(at, "Confirm & Save")
    assert not at.exception
    assert "Saved receipt #42" in at.success[0].value
    assert list(config.IMAGES_DIR.glob("*.jpg"))


def test_missing_database_url_keeps_edits(env, monkeypatch):
    env(json.dumps(GOOD))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    at = extract(start(png()))
    at.text_input(key="merchant_name_1").set_value("Edited Traders")
    submit(at, "Confirm & Save")
    assert not at.exception
    assert "DATABASE_URL" in at.error[0].value
    assert at.text_input(key="merchant_name_1").value == "Edited Traders"


@pytest.mark.parametrize("outcomes, message", [
    (("not json", "still not json"), "could not be read"),
    ((client_error(429, "RESOURCE_EXHAUSTED"),), "limit"),
])
def test_extraction_failure_offers_retry_and_manual_entry(env, outcomes, message):
    env(*outcomes)
    at = extract(start(png()))
    assert not at.exception
    assert message in at.error[0].value
    assert at.image  # the receipt stays on screen
    at.button(key="enter_by_hand").click()
    at.run()
    assert at.text_input(key="merchant_name_1").value == ""
    assert save_button(at).disabled                 # V1 / V9 block an empty draft


def test_try_again_calls_the_api_again(env):
    env(client_error(429, "RESOURCE_EXHAUSTED"))
    at = extract(start(png()))
    env(json.dumps(GOOD))
    at.button(key="try_again").click()
    at.run()
    assert at.text_input(key="merchant_name_1").value == "Shree Traders"


def test_bad_upload_shows_message_not_traceback(env):
    at = start(b"this is not an image")
    assert not at.exception
    assert at.error[0].value == "This file isn't a readable image."
