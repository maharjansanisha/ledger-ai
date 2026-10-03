"""TASK-012: image intake. Only tiny synthetic images made here; no real receipts."""

import io

import pytest
from PIL import Image

from bahikhata import config
from bahikhata.images import (
    MSG_TOO_LARGE,
    MSG_UNREADABLE,
    MSG_WRONG_TYPE,
    ImageIntakeError,
    process_upload,
)


def make_image(fmt="JPEG", size=(40, 20), mode="RGB", color="red", exif_orientation=None) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    kwargs = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation  # Orientation tag
        kwargs["exif"] = exif
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_accepts_jpeg_and_png_and_returns_jpeg(fmt):
    result = process_upload(make_image(fmt))
    assert result.original_format == fmt
    assert result.original_size == (40, 20) == result.processed_size
    with Image.open(io.BytesIO(result.processed_bytes)) as out:
        assert out.format == "JPEG" and out.mode == "RGB"


def test_same_bytes_give_same_hashes():
    data = make_image()
    a, b = process_upload(data), process_upload(data)
    assert a.original_sha256 == b.original_sha256 and len(a.original_sha256) == 64
    assert a.processed_sha256 == b.processed_sha256
    assert a.original_sha256 != a.processed_sha256


def test_text_file_renamed_jpg_is_unreadable():
    with pytest.raises(ImageIntakeError, match=MSG_UNREADABLE):
        process_upload(b"this is a text file, not a receipt\n" * 10)


def test_corrupt_jpeg_is_unreadable():
    data = make_image(size=(200, 200))
    with pytest.raises(ImageIntakeError, match=MSG_UNREADABLE):
        process_upload(data[: len(data) // 3])  # truncated


def test_empty_upload_is_unreadable():
    with pytest.raises(ImageIntakeError, match=MSG_UNREADABLE):
        process_upload(b"")


@pytest.mark.parametrize("fmt", ["WEBP", "GIF", "BMP"])
def test_other_formats_ask_for_jpg_or_png(fmt):
    with pytest.raises(ImageIntakeError, match=MSG_WRONG_TYPE):
        process_upload(make_image(fmt))


def test_heic_asks_for_jpg_or_png():
    fake_heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 100
    with pytest.raises(ImageIntakeError, match=MSG_WRONG_TYPE):
        process_upload(fake_heic)


def test_oversize_file_rejected_before_decoding(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1000)
    with pytest.raises(ImageIntakeError, match=MSG_TOO_LARGE.replace("(", r"\(").replace(")", r"\)")):
        process_upload(b"\xff" * 1001)


def test_exactly_max_size_is_allowed(monkeypatch):
    data = make_image()
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", len(data))
    process_upload(data)


def test_large_dimensions_shrink_to_long_edge_keeping_aspect(monkeypatch):
    monkeypatch.setattr(config, "MAX_LONG_EDGE_PX", 100)
    result = process_upload(make_image(size=(400, 200)))
    assert result.original_size == (400, 200)
    assert result.processed_size == (100, 50)


def test_small_images_are_not_enlarged():
    assert process_upload(make_image(size=(30, 60))).processed_size == (30, 60)


def test_exif_rotation_is_applied():
    # Orientation 6 = rotate 90° clockwise to display: a 40×20 stored image displays as 20×40.
    result = process_upload(make_image(size=(40, 20), exif_orientation=6))
    assert result.processed_size == (20, 40)


def test_transparent_png_is_flattened_onto_white():
    result = process_upload(make_image("PNG", mode="RGBA", color=(0, 0, 0, 0)))
    with Image.open(io.BytesIO(result.processed_bytes)) as out:
        assert all(channel > 240 for channel in out.getpixel((5, 5)))
