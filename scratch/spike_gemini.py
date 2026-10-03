"""TASK-002 spike: can Gemini read a receipt image and return schema-shaped JSON?

THROWAWAY script. Not part of the app (ARCHITECTURE.md keeps all real logic in
bahikhata/). It answers four questions for the Spike Log in TASKS.md:
  1. Does the API key work, and which model id do we use?
  2. Does the image reach the model?
  3. Does structured JSON output work with nullable (str | None) fields?
  4. Does our own Pydantic validation accept what Gemini returns?

Usage (run on your Mac from the project root):
  uv run python scratch/spike_gemini.py --list-models
  uv run python scratch/spike_gemini.py --model <model-id> path/to/receipt1.jpg [receipt2.jpg]
  uv run python scratch/spike_gemini.py --model <model-id> --no-schema receipt.jpg   # fallback mode
  uv run python scratch/spike_gemini.py --dry-run receipt.jpg   # no network: checks image prep + schema
"""

import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ValidationError

# Make `bahikhata` importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bahikhata import config  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"  # gitignored


# --- Minimal extraction schema (copied from ARCHITECTURE.md §9) --------------
# Lenient on purpose: every field is a string or null. The LLM TRANSCRIBES;
# deterministic code parses amounts/dates later (decision D3).
# The real models will live in bahikhata/schemas.py (TASK-006).

class ExtractedLineItem(BaseModel):
    description: str | None = None
    quantity_raw: str | None = None
    unit_price_raw: str | None = None
    amount_raw: str | None = None


class ReceiptExtraction(BaseModel):
    merchant_name: str | None = None
    merchant_pan: str | None = None
    invoice_number: str | None = None
    date_raw: str | None = Field(None, description="Date exactly as printed")
    date_calendar_hint: Literal["BS", "AD", "unknown"] | None = None
    line_items: list[ExtractedLineItem] = Field(default_factory=list)
    subtotal_raw: str | None = None
    discount_raw: str | None = None
    service_charge_raw: str | None = None
    vat_amount_raw: str | None = None
    total_raw: str | None = None
    category: str | None = None
    currency_raw: str | None = None


# Spike-only prompt. The real, versioned prompt is prompts/extraction_v1.txt (TASK-014).
PROMPT = """You are reading a purchase receipt or VAT bill from a small business in Nepal.
Transcribe the requested fields into JSON.

Rules:
- If a field is not printed or you cannot read it, return null. Never guess.
- Copy amounts EXACTLY as printed (keep commas, "Rs.", "/-"). Do not calculate or correct anything.
- Copy the date EXACTLY as printed in date_raw. Do not convert between Bikram Sambat (BS) and AD.
  Set date_calendar_hint to "BS", "AD" or "unknown" based only on what is printed.
- merchant_pan is the seller's PAN/VAT number as printed.
- category: choose one of Inventory, Office Supplies, Transportation, Utilities, Rent,
  Equipment, Marketing, Food, Other.
"""


def prepare_image(path: Path) -> tuple[bytes, dict]:
    """Open, EXIF-rotate, downscale and re-encode as JPEG (mirrors future images.py)."""
    raw = path.read_bytes()
    img = Image.open(io.BytesIO(raw))
    info = {"file": str(path), "original_format": img.format,
            "original_size": img.size, "original_bytes": len(raw)}
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((config.MAX_LONG_EDGE_PX, config.MAX_LONG_EDGE_PX))  # keeps aspect ratio
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    data = buf.getvalue()
    info.update({"sent_size": img.size, "sent_bytes": len(data)})
    return data, info


def build_config(use_schema: bool):
    from google.genai import types

    if use_schema:
        # Structured output: the SDK turns the Pydantic model into a response schema.
        return types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ReceiptExtraction,
        )
    # Fallback (ARCHITECTURE.md §9 note): JSON mode, schema described in the prompt.
    return types.GenerateContentConfig(temperature=0, response_mime_type="application/json")


def list_models(client) -> None:
    print("Models that support generateContent (look for a 'flash' model):")
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if "generateContent" in actions:
            print("  ", m.name)


def run_one(client, model: str, path: Path, use_schema: bool) -> bool:
    from google.genai import errors, types

    image_bytes, info = prepare_image(path)
    print("=" * 78)
    print(f"IMAGE: {info}")

    prompt = PROMPT
    if not use_schema:
        prompt += "\nReturn JSON with exactly these keys:\n" + json.dumps(
            ReceiptExtraction.model_json_schema(), indent=1)

    start = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
            config=build_config(use_schema),
        )
    except errors.APIError as exc:
        # 429 = rate limit (do not retry in a loop), 4xx = our request, 5xx = server.
        print(f"API ERROR code={exc.code} status={exc.status}\n{exc.message}")
        return False
    latency = time.perf_counter() - start

    candidate = response.candidates[0] if response.candidates else None
    print(f"MODEL: {model} | latency: {latency:.1f}s | "
          f"finish_reason: {candidate.finish_reason if candidate else None}")
    print(f"USAGE: {response.usage_metadata}")

    raw_text = response.text
    print("\n--- RAW RESPONSE TEXT (unmodified) ---")
    print(raw_text)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_file = OUTPUT_DIR / f"{path.stem}__{model.replace('/', '_')}__{'schema' if use_schema else 'noschema'}.json"
    out_file.write_text(raw_text or "")
    print(f"(raw response saved to {out_file.relative_to(Path.cwd()) if out_file.is_relative_to(Path.cwd()) else out_file})")

    # Our own validation: never trust the SDK/provider alone.
    print("\n--- OUR PYDANTIC VALIDATION ---")
    try:
        parsed = ReceiptExtraction.model_validate_json(raw_text or "")
    except ValidationError as exc:
        print("FAILED:\n", exc)
        return False
    print("OK")
    print(parsed.model_dump_json(indent=2))
    nulls = [k for k, v in parsed.model_dump().items() if v in (None, [])]
    print(f"\nFields returned as null/empty: {nulls}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-002 Gemini receipt spike")
    parser.add_argument("images", nargs="*", type=Path, help="1-2 receipt images (JPG/PNG)")
    parser.add_argument("--model", help="Gemini model id, e.g. from --list-models")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--no-schema", action="store_true",
                        help="fallback: JSON mode with schema described in the prompt")
    parser.add_argument("--dry-run", action="store_true",
                        help="no network: prepare images and build the request config only")
    args = parser.parse_args()

    if args.dry_run:
        missing = [str(p) for p in args.images if not p.is_file()]
        if missing:
            parser.error(f"image file(s) not found: {missing}")
        for path in args.images:
            _, info = prepare_image(path)
            print("IMAGE OK:", info)
        build_config(use_schema=not args.no_schema)
        print("Request config built OK (schema mode)" if not args.no_schema
              else "Request config built OK (no-schema mode)")
        return 0

    from google import genai

    client = genai.Client(api_key=config.get_gemini_api_key())  # key from .env, never printed

    if args.list_models:
        list_models(client)
        return 0
    if not args.model or not args.images:
        parser.error("give --model and 1-2 image paths (run --list-models first)")
    if len(args.images) > 2:
        parser.error("spike uses at most 2 receipts")
    missing = [str(p) for p in args.images if not p.is_file()]
    if missing:
        parser.error(f"image file(s) not found: {missing}. Put masked receipts in data/eval/dev/ first.")

    results = [run_one(client, args.model, p, use_schema=not args.no_schema) for p in args.images]
    print("=" * 78)
    print("SUMMARY:", {str(p): ("OK" if ok else "FAILED") for p, ok in zip(args.images, results)})
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
