"""TASK-013 LLM client (cache, retry policy) and TASK-015 extract_receipt (parse, retry, EXTRACTION_FAILED).

No network: a fake client stands in for google-genai. The one live test is skipped
unless GEMINI_API_KEY is set AND RUN_LIVE=1.
"""

import io
import json
import os
from pathlib import Path
import pytest
from bahikhata import config, llm_client
from bahikhata.extraction import extract_receipt
from bahikhata.schemas import ReceiptExtraction
from tests.fakes import FakeClient, client_error, server_error

IMAGE = b"\xff\xd8 fake processed jpeg bytes"
GOOD = json.dumps({"merchant_name": "Shree Traders", "total_raw": "Rs. 1,250.00", "line_items": []})


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "LLM_RETRY_WAIT_SECONDS", 0)
    return tmp_path / "cache"


# --- llm_client --------------------------------------------------------------

def test_load_prompt_reads_versioned_file():
    prompt = llm_client.load_prompt(config.EXTRACTION_PROMPT)
    for rule in ("return null", "EXACTLY as printed", "Do not calculate", "Do not convert"):
        assert rule in prompt


def test_request_uses_model_temperature_zero_json_schema_and_image():
    client = FakeClient(GOOD)
    llm_client.call_extraction(IMAGE, ReceiptExtraction, client=client)
    call = client.calls[0]
    assert call["model"] == config.MODEL_NAME
    assert call["config"].temperature == 0
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_schema is ReceiptExtraction
    assert call["contents"][0].inline_data.data == IMAGE
    assert call["contents"][1] == llm_client.load_prompt("extraction_v1")


def test_second_identical_call_is_a_cache_hit_without_network(isolated_cache):
    first = llm_client.call_extraction(IMAGE, ReceiptExtraction, client=FakeClient(GOOD))
    no_network = FakeClient()  # would raise IndexError if called
    second = llm_client.call_extraction(IMAGE, ReceiptExtraction, client=no_network)
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert second.text == GOOD and no_network.calls == []
    assert len(list(isolated_cache.glob("*.json"))) == 1


def test_cache_key_changes_with_prompt_version_model_and_input():
    base = llm_client.cache_key("extraction", "m1", "extraction_v1", "abc")
    assert base != llm_client.cache_key("extraction", "m1", "extraction_v2", "abc")
    assert base != llm_client.cache_key("extraction", "m2", "extraction_v1", "abc")
    assert base != llm_client.cache_key("extraction", "m1", "extraction_v1", "abd")
    assert base != llm_client.cache_key("sql", "m1", "extraction_v1", "abc")


def test_changing_prompt_version_is_a_cache_miss(tmp_path, monkeypatch):
    llm_client.call_extraction(IMAGE, ReceiptExtraction, client=FakeClient(GOOD))
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path)
    (tmp_path / "extraction_v2.txt").write_text("v2 prompt")
    client = FakeClient(GOOD)
    response = llm_client.call_extraction(IMAGE, ReceiptExtraction, prompt_version="extraction_v2", client=client)
    assert response.cache_hit is False and len(client.calls) == 1


def test_one_retry_on_503_then_success():
    client = FakeClient(server_error(), GOOD)
    response = llm_client.call_extraction(IMAGE, ReceiptExtraction, client=client)
    assert response.text == GOOD and len(client.calls) == 2


def test_one_retry_on_timeout_then_clear_error():
    client = FakeClient(TimeoutError("read timed out"), TimeoutError("read timed out"))
    with pytest.raises(llm_client.LLMError) as info:
        llm_client.call_extraction(IMAGE, ReceiptExtraction, client=client)
    assert info.value.kind == "unavailable" and len(client.calls) == 2


def test_429_is_not_retried():
    client = FakeClient(client_error(429, "RESOURCE_EXHAUSTED"), GOOD)
    with pytest.raises(llm_client.LLMError) as info:
        llm_client.call_extraction(IMAGE, ReceiptExtraction, client=client)
    assert info.value.kind == "rate_limit" and len(client.calls) == 1
    assert "limit" in info.value.user_message


def test_other_4xx_is_not_retried():
    client = FakeClient(client_error(404, "NOT_FOUND"))
    with pytest.raises(llm_client.LLMError) as info:
        llm_client.call_extraction(IMAGE, ReceiptExtraction, client=client)
    assert info.value.kind == "request" and len(client.calls) == 1


def test_failed_calls_are_not_cached(isolated_cache):
    with pytest.raises(llm_client.LLMError):
        llm_client.call_extraction(IMAGE, ReceiptExtraction, client=FakeClient(client_error(429, "R")))
    assert not isolated_cache.exists() or list(isolated_cache.glob("*.json")) == []


def test_api_key_never_appears_in_errors_or_cache(monkeypatch, isolated_cache):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    llm_client.call_extraction(IMAGE, ReceiptExtraction, client=FakeClient(GOOD))
    with pytest.raises(llm_client.LLMError) as info:
        llm_client.call_extraction(b"other", ReceiptExtraction, client=FakeClient(server_error(), server_error()))
    assert "SECRET-KEY-123" not in str(info.value)
    assert all("SECRET-KEY-123" not in p.read_text() for p in isolated_cache.glob("*.json"))


def test_call_json_caches_by_namespace_and_input():
    response = llm_client.call_json(
        "prompt", namespace="sql", prompt_version="sql_v1", cache_input="q|2026-10-03", client=FakeClient('{"status": "ok"}')
    )
    again = llm_client.call_json(
        "prompt", namespace="sql", prompt_version="sql_v1", cache_input="q|2026-10-03", client=FakeClient()
    )
    assert response.text == again.text and again.cache_hit


# --- extract_receipt ---------------------------------------------------------

def test_extract_receipt_success_keeps_raw_text():
    result = extract_receipt(IMAGE, client=FakeClient(GOOD))
    assert result.extraction.total_raw == "Rs. 1,250.00"  # transcribed text, not parsed
    assert result.raw_response == GOOD
    assert result.flags == [] and result.attempts == 1
    assert result.model_name == config.MODEL_NAME and result.prompt_version == "extraction_v1"
    assert len(result.image_sha256) == 64


def test_extract_receipt_retries_once_with_error_note_then_succeeds():
    client = FakeClient("not json at all", GOOD)
    result = extract_receipt(IMAGE, client=client)
    assert result.extraction is not None and result.attempts == 2
    assert "could not be parsed" in client.calls[1]["contents"][1]


def test_extract_receipt_fails_twice_gives_extraction_failed_and_keeps_raw():
    bad = '{"line_items": "should be a list"}'
    result = extract_receipt(IMAGE, client=FakeClient("not json", bad))
    assert result.extraction is None
    assert result.raw_response == bad
    assert [(f.rule_id, f.severity) for f in result.flags] == [("EXTRACTION_FAILED", "BLOCKING")]


def test_malformed_cache_file_is_dropped_and_api_called_again(isolated_cache):
    """A cached response that no longer matches the schema is discarded: a fresh API call is made."""
    extract_receipt(IMAGE, client=FakeClient(GOOD))
    (cache_file,) = isolated_cache.glob("*.json")
    entry = json.loads(cache_file.read_text())
    entry["text"] = "{broken"
    cache_file.write_text(json.dumps(entry))

    client = FakeClient(GOOD)
    result = extract_receipt(IMAGE, client=client)
    assert len(client.calls) == 1 and result.cache_hit is False
    assert result.extraction is not None and result.attempts == 1


def test_unparseable_responses_are_not_cached_so_try_again_calls_the_api(isolated_cache):
    first = extract_receipt(IMAGE, client=FakeClient("not json", "still not json"))
    assert first.extraction is None and list(isolated_cache.glob("*.json")) == []

    client = FakeClient(GOOD)  # "Try again"
    second = extract_receipt(IMAGE, client=client)
    assert len(client.calls) == 1 and second.extraction is not None and second.cache_hit is False


def test_good_answer_to_the_retry_is_cached_under_its_own_key(isolated_cache):
    extract_receipt(IMAGE, client=FakeClient("not json", GOOD))
    assert len(list(isolated_cache.glob("*.json"))) == 1  # only the retry-with-note response


@pytest.mark.parametrize("error, text", [
    (client_error(429, "RESOURCE_EXHAUSTED"), "limit"),
    (server_error(), "unavailable"),
])
def test_provider_errors_become_extraction_failed_not_exceptions(error, text):
    result = extract_receipt(IMAGE, client=FakeClient(error, error))
    assert result.extraction is None and result.raw_response is None
    assert result.flags[0].rule_id == "EXTRACTION_FAILED"
    assert text in result.flags[0].message


# --- Live (opt-in) -----------------------------------------------------------

LIVE_IMAGE = Path(__file__).resolve().parent.parent / "data" / "eval" / "dev" / "r002.png"  # local only


@pytest.mark.skipif(
    not (os.getenv("RUN_LIVE") == "1" and os.getenv("GEMINI_API_KEY") and LIVE_IMAGE.is_file()),
    reason="live Gemini test: set RUN_LIVE=1 with GEMINI_API_KEY and a local data/eval/dev/r002.png",
)
def test_live_extraction_on_dev_receipt():
    """LIVE: calls the real Gemini API once (uses free-tier quota)."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(LIVE_IMAGE)).convert("RGB")
    img.thumbnail((config.MAX_LONG_EDGE_PX, config.MAX_LONG_EDGE_PX))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)

    result = extract_receipt(buf.getvalue())
    assert result.flags == [], result.flags
    assert result.extraction is not None


def test_missing_api_key_is_a_user_safe_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    result = extract_receipt(IMAGE)  # no fake client: the real client factory runs
    assert result.extraction is None
    assert "GEMINI_API_KEY is not set" in result.flags[0].message
