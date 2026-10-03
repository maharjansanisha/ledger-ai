"""The ONLY module that talks to the LLM provider (ARCHITECTURE.md §3.4, TASK-013).

- Prompts are versioned files in prompts/ (the version is the file name).
- Every call is temperature 0 with JSON output.
- Responses are cached on disk in data/cache/ (gitignored), one JSON file per key,
  key = sha256(namespace + model + prompt_version + input). The app and eval share
  the cache, so re-running costs zero requests.
- Errors: one automatic retry on 5xx / timeout / network failure; NO automatic
  retry on 429 (free-tier limit), so the quota is never burned in a loop.
- This module returns raw text and never interprets, fixes or computes anything.
- The API key is never printed or logged.

Switching provider (e.g. GitHub Models) means rewriting the body of this file;
the function signatures are the boundary.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from bahikhata import config


class LLMError(Exception):
    """A provider call failed. `kind` is "rate_limit", "unavailable", "request" or "config";
    `user_message` is safe to show in the UI."""

    def __init__(self, kind: str, user_message: str, detail: str = ""):
        super().__init__(f"{kind}: {detail or user_message}")
        self.kind = kind
        self.user_message = user_message


_USER_MESSAGES = {
    "rate_limit": "Free-tier limit reached — wait a minute and retry.",
    "unavailable": "Extraction service unavailable — try again.",
    "request": "The AI service rejected the request — check the model name and API key.",
    "config": "GEMINI_API_KEY is not set — copy .env.example to .env and add your key.",
}


@dataclass
class LLMResponse:
    text: str                # raw model text, unmodified
    model_name: str
    prompt_version: str
    cache_hit: bool
    latency_s: float         # 0.0 on a cache hit


def load_prompt(name: str) -> str:
    """Read prompts/<name>.txt, e.g. load_prompt("extraction_v1")."""
    return (config.PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def cache_key(namespace: str, model_name: str, prompt_version: str, cache_input: str) -> str:
    return hashlib.sha256(
        "\x1f".join((namespace, model_name, prompt_version, cache_input)).encode("utf-8")
    ).hexdigest()


def _cache_read(key: str) -> dict[str, Any] | None:
    path = config.CACHE_DIR / f"{key}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _cache_write(key: str, entry: dict[str, Any]) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (config.CACHE_DIR / f"{key}.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _default_client():
    from google import genai
    from google.genai import types

    try:
        api_key = config.get_gemini_api_key()
    except RuntimeError as exc:
        raise LLMError("config", _USER_MESSAGES["config"]) from exc
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=config.LLM_TIMEOUT_SECONDS * 1000),
    )


def _generate(client, model_name: str, contents: list, response_schema: type[BaseModel] | None) -> str:
    """One provider call with the A§14 retry policy. Returns the raw response text."""
    from google.genai import errors, types

    generation_config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_schema=response_schema,
    )
    for attempt in (1, 2):
        try:
            response = client.models.generate_content(
                model=model_name, contents=contents, config=generation_config
            )
            return response.text or ""
        except errors.APIError as exc:
            detail = f"HTTP {exc.code} {exc.status}"
            if exc.code == 429:
                raise LLMError("rate_limit", _USER_MESSAGES["rate_limit"], detail) from exc
            if exc.code is not None and exc.code < 500:
                raise LLMError("request", _USER_MESSAGES["request"], detail) from exc
            error = LLMError("unavailable", _USER_MESSAGES["unavailable"], detail)
        except Exception as exc:  # timeouts / connection errors from the HTTP layer
            error = LLMError("unavailable", _USER_MESSAGES["unavailable"], type(exc).__name__)
        if attempt == 1:
            time.sleep(config.LLM_RETRY_WAIT_SECONDS)
    raise error


def _call(
    *,
    namespace: str,
    prompt_version: str,
    contents: list,
    cache_input: str,
    response_schema: type[BaseModel] | None,
    client,
) -> LLMResponse:
    model_name = config.MODEL_NAME
    key = cache_key(namespace, model_name, prompt_version, cache_input)
    cached = _cache_read(key)
    if cached is not None:
        return LLMResponse(cached["text"], model_name, prompt_version, cache_hit=True, latency_s=0.0)

    start = time.perf_counter()
    text = _generate(client or _default_client(), model_name, contents, response_schema)
    latency = time.perf_counter() - start
    _cache_write(key, {
        "namespace": namespace,
        "model_name": model_name,
        "prompt_version": prompt_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "text": text,
    })
    return LLMResponse(text, model_name, prompt_version, cache_hit=False, latency_s=latency)


def call_extraction(
    image_bytes: bytes,
    response_schema: type[BaseModel],
    *,
    prompt_version: str | None = None,
    extra_instruction: str = "",
    mime_type: str = "image/jpeg",
    client=None,
) -> LLMResponse:
    """Image + extraction prompt -> raw JSON text. Cached by the processed image's sha256.

    `extra_instruction` (e.g. a schema-error note on the retry) is appended to the
    prompt and is part of the cache key.
    """
    from google.genai import types

    prompt_version = prompt_version or config.EXTRACTION_PROMPT
    prompt = load_prompt(prompt_version) + (f"\n{extra_instruction}" if extra_instruction else "")
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    return _call(
        namespace="extraction",
        prompt_version=prompt_version,
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt],
        cache_input=image_sha256 + extra_instruction,
        response_schema=response_schema,
        client=client,
    )


def call_json(
    prompt_text: str,
    *,
    namespace: str,
    prompt_version: str,
    cache_input: str,
    response_schema: type[BaseModel] | None = None,
    client=None,
) -> LLMResponse:
    """Text prompt -> raw JSON text (used later for SQL planning, A§6).

    The caller fills the prompt and chooses `cache_input` (for SQL: question + today).
    """
    return _call(
        namespace=namespace,
        prompt_version=prompt_version,
        contents=[prompt_text],
        cache_input=cache_input,
        response_schema=response_schema,
        client=client,
    )
