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
import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

from bahikhata import config, db, llm_client, review
from tests.fakes import FakeClient, client_error

PAGE = str(Path(__file__).resolve().parent.parent / "pages" / "capture_and_review" / "page.py")
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


@pytest.fixture
def switched(monkeypatch):
    """Records st.switch_page calls (the page files aren't registered when AppTest runs one page)."""
    calls = []
    monkeypatch.setattr(st, "switch_page", lambda page, query_params=None: calls.append(page))
    return calls


def start(upload=None, query_params=None) -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=30)
    for name, value in (query_params or {}).items():
        at.query_params[name] = value
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
    assert "⛔ BLOCKING: V1: total is missing" in text(at)
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


def test_v5_needs_save_anyway_checkbox(env, monkeypatch, switched):
    env(json.dumps({**GOOD, "total_raw": "1,220"}))
    saved = {}
    monkeypatch.setattr("bahikhata.db.get_connection", lambda: _FakeConn())
    monkeypatch.setattr("bahikhata.db.save_confirmed_receipt", lambda c, r, p, a: saved.setdefault("r", r) and 9)
    at = extract(start(png()))
    assert "❗ ERROR: V5:" in text(at)
    submit(at, "Confirm & Save")                    # without the checkbox
    assert "save anyway" in at.error[0].value and "r" not in saved
    at.checkbox(key="override_1").check()
    submit(at, "Confirm & Save")
    assert switched == ["pages/dashboard/page.py"]
    assert at.session_state["ledger_notice"] == "Saved receipt #9."
    assert saved["r"].user_override is True and saved["r"].status == "invalid"


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_save_success_redirects_to_dashboard(env, monkeypatch, switched):
    env(json.dumps(GOOD))
    monkeypatch.setattr("bahikhata.db.get_connection", lambda: _FakeConn())
    monkeypatch.setattr("bahikhata.db.save_confirmed_receipt", lambda c, r, p, a: 42)
    at = extract(start(png()))
    submit(at, "Confirm & Save")
    assert not at.exception
    assert switched == ["pages/dashboard/page.py"]
    assert at.session_state["ledger_notice"] == "Saved receipt #42."
    assert "phase" not in at.session_state  # the page is cleared for the next receipt
    assert list(config.IMAGES_DIR.glob("*.jpg"))


# --- Edit a saved receipt (?edit=<id>) -----------------------------------------

SAVED_ROW = {
    "id": 7, "merchant_name": "Shree Traders", "merchant_pan": "123456789", "invoice_number": "INV-1",
    "date_ad": date(2026, 9, 30), "date_bs": "2083-06-14", "bs_year": 2083, "bs_month": 6,
    "subtotal_paisa": 100000, "discount_paisa": None, "service_charge_paisa": None, "vat_paisa": 13000,
    "total_paisa": 113000, "category": "Inventory", "status": "clean", "user_override": False,
    "image_path": "missing/receipt.jpg",
    "line_items": [{"line_no": 1, "description": "Rice", "quantity": 2.0,
                    "unit_price_paisa": 50000, "amount_paisa": 100000}],
}


@pytest.fixture
def saved_db(monkeypatch):
    """A fake ledger holding SAVED_ROW; returns the dict update calls are recorded in."""
    updates = {}
    monkeypatch.setattr(db, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(db, "get_receipt", lambda conn, rid: SAVED_ROW if rid == 7 else None)
    monkeypatch.setattr(db, "get_ai_draft_json", lambda conn, rid: None)

    def update(conn, rid, receipt, **audit):
        updates.update(id=rid, receipt=receipt, **audit)
    monkeypatch.setattr(db, "update_confirmed_receipt", update)
    return updates


def test_edit_loads_saved_values_without_calling_the_api(env, saved_db):
    client = env(json.dumps(GOOD))
    at = start(query_params={"edit": "7"})
    assert not at.exception
    assert client.calls == []
    assert at.title[0].value == "Edit receipt #7"
    assert len(at.file_uploader) == 0
    assert at.text_input(key="merchant_name_1").value == "Shree Traders"
    assert at.text_input(key="date_raw_1").value == "2083/06/14"
    assert at.text_input(key="total_1").value == "1130.00"
    assert "2026-09-30" in text(at) and "clean" in text(at)
    assert "could not be found" in at.info[0].value  # image file missing: message, not a crash
    assert not save_button(at).disabled


def test_edit_save_updates_and_redirects(env, saved_db, switched):
    env()
    at = start(query_params={"edit": "7"})
    at.text_input(key="merchant_name_1").set_value("Shree Traders Pvt")
    submit(at, "Confirm & Save")
    assert not at.exception
    assert saved_db["id"] == 7 and saved_db["receipt"].merchant_name == "Shree Traders Pvt"
    assert switched == ["pages/dashboard/page.py"]
    assert at.session_state["ledger_notice"] == "Updated receipt #7."


def test_edit_unknown_receipt_shows_message(env, saved_db):
    at = start(query_params={"edit": "99"})
    assert not at.exception
    assert "#99 was not found" in at.error[0].value


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


def elements_in_order(at: AppTest) -> list:
    """Every element of the page in render order (blocks such as forms and columns flattened)."""
    def walk(node):
        yield node
        for child in getattr(node, "children", {}).values():
            yield from walk(child)
    return list(walk(at.main))


def element_after(at: AppTest, key: str):
    elements = elements_in_order(at)
    index = next(i for i, e in enumerate(elements) if getattr(e, "key", None) == key)
    return elements[index + 1]


def save_hint_text(at: AppTest) -> str | None:
    return next((c.value for c in at.caption if c.value.startswith("Can't save yet")), None)


def test_missing_total_message_is_shown_under_the_total_field(env):
    env(json.dumps({**GOOD, "total_raw": None}))
    at = extract(start(png()))
    below_total = element_after(at, "total_1")
    assert below_total.type == "caption"
    assert below_total.value == "⛔ BLOCKING: V1: total is missing"
    assert at.text_input(key="total_1").label == "Total *"
    assert "total is missing" not in "\n".join(m.value for m in at.markdown)  # not repeated at the top


def test_cant_save_line_appears_then_disappears_after_fixing(env):
    env(json.dumps({**GOOD, "total_raw": None, "date_raw": None}))
    at = extract(start(png()))
    assert save_hint_text(at) == "Can't save yet: date is missing or could not be read; total is missing."

    at.text_input(key="total_1").set_value("1130")
    at.text_input(key="date_raw_1").set_value("2083/06/14")
    submit(at, "Re-validate")
    assert save_hint_text(at) is None
    assert not save_button(at).disabled
    assert element_after(at, "total_1").type != "caption"  # no message under Total any more


def test_required_marks_and_instructions(env):
    env(json.dumps(GOOD))
    at = extract(start(png()))
    labels = {e.key: e.label for e in elements_in_order(at) if hasattr(e, "label") and getattr(e, "key", None)}
    assert labels["merchant_name_1"] == "Merchant *" and labels["date_raw_1"] == "Date (as printed) *"
    assert labels["category_1"] == "Category *" and labels["total_1"] == "Total *"
    assert labels["subtotal_1"] == "Subtotal" and labels["merchant_pan_1"] == "PAN / VAT no."
    captions = [c.value for c in at.caption]
    assert "Edit, then click **Re-validate** to check your changes." in captions
    assert "\\* required" in captions


def test_v5_hint_mentions_the_checkbox(env):
    env(json.dumps({**GOOD, "total_raw": "1,220"}))
    at = extract(start(png()))
    assert any("save anyway" in c.value and c.value.startswith("The amounts don't add up") for c in at.caption)
    assert not save_button(at).disabled
