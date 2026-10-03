"""Image intake and basic handling (ARCHITECTURE.md §3.2-3.3, TASK-012).

Intake decides whether an upload is a usable JPEG/PNG receipt image; handling
EXIF-rotates it, shrinks the long edge to config.MAX_LONG_EDGE_PX and re-encodes
it as RGB JPEG for the API. No deskew/crop/"enhance" (out of scope).

Bad input raises ImageIntakeError with a message that is safe to show in the UI
(A§14 wording), before any API call. Never a traceback.
"""

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from bahikhata import config

MSG_TOO_LARGE = "File too large (max 10 MB)."
MSG_WRONG_TYPE = "Please upload JPG or PNG."
MSG_UNREADABLE = "This file isn't a readable image."

# HEIC/HEIF (iPhone photos): Pillow cannot open these, so recognise them by their
# ISO-BMFF "ftyp" brand to give the "JPG or PNG" message instead of "unreadable".
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif")


class ImageIntakeError(ValueError):
    """The upload cannot be used. str(error) is the user-facing message."""


@dataclass(frozen=True)
class ProcessedImage:
    original_sha256: str        # hash of the uploaded bytes (receipt_audit.image_sha256)
    processed_bytes: bytes      # RGB JPEG sent to the API
    processed_sha256: str       # hash of processed_bytes (LLM cache input)
    original_format: str        # "JPEG" or "PNG"
    original_size: tuple[int, int]
    processed_size: tuple[int, int]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_heif(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS


def process_upload(data: bytes) -> ProcessedImage:
    """Check an uploaded file and prepare it for extraction, or raise ImageIntakeError.

    The file's content decides its type, not its name (a .txt renamed .jpg is rejected).
    """
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise ImageIntakeError(MSG_TOO_LARGE)
    if _is_heif(data):
        raise ImageIntakeError(MSG_WRONG_TYPE)

    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = probe.format
            probe.verify()  # structural check; the image must be reopened afterwards
        if image_format not in config.ALLOWED_IMAGE_FORMATS:
            raise ImageIntakeError(MSG_WRONG_TYPE)
        with Image.open(io.BytesIO(data)) as img:
            img.load()  # catches truncated pixel data that verify() can miss
            original_size = img.size
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")  # transparent PNG: flatten onto white, not black
                img = Image.new("RGB", rgba.size, "white")
                img.paste(rgba, mask=rgba.getchannel("A"))
            else:
                img = img.convert("RGB")
            img.thumbnail((config.MAX_LONG_EDGE_PX, config.MAX_LONG_EDGE_PX))  # keeps aspect ratio
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
    except ImageIntakeError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageIntakeError(MSG_UNREADABLE) from exc

    processed = buf.getvalue()
    return ProcessedImage(
        original_sha256=_sha256(data),
        processed_bytes=processed,
        processed_sha256=_sha256(processed),
        original_format=image_format,
        original_size=original_size,
        processed_size=img.size,
    )
