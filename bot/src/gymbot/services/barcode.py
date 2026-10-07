"""Barcodes on food photos, decoded locally (zxing-cpp; Pillow decodes the JPEG into pixels).

Retail codes only: EAN-13, EAN-8, UPC-A, UPC-E. Any decode problem (not an image, no code) means "no barcode":
the photo then goes to the vision model as usual. Also the JPEG downscale for the vision model.
"""

from __future__ import annotations

import asyncio
import io
import logging

import zxingcpp
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

FORMATS = (
    zxingcpp.BarcodeFormat.EAN13,
    zxingcpp.BarcodeFormat.EAN8,
    zxingcpp.BarcodeFormat.UPCA,
    zxingcpp.BarcodeFormat.UPCE,
)


def decode(image: bytes) -> str | None:
    """The first retail barcode on the image (digits), else None."""
    try:
        with Image.open(io.BytesIO(image)) as im:
            found = zxingcpp.read_barcodes(ImageOps.exif_transpose(im).convert("L"), formats=FORMATS)
    except Exception as e:  # noqa: BLE001 - a broken or odd image is just "no barcode"
        log.info("barcode: no decode (%s)", type(e).__name__)
        return None
    for code in found:
        if code.valid and code.text.isdigit():
            return code.text
    return None


async def decode_async(image: bytes) -> str | None:
    """decode() off the event loop (tens of ms of CPU on a 2560 px photo)."""
    return await asyncio.to_thread(decode, image)


def downscale(image: bytes, max_side: int, quality: int = 85) -> bytes:
    """A JPEG no larger than `max_side` px; the original bytes when already small enough or unreadable."""
    try:
        with Image.open(io.BytesIO(image)) as im:
            if max(im.size) <= max_side:
                return image
            small = ImageOps.exif_transpose(im).convert("RGB")
            small.thumbnail((max_side, max_side))
            out = io.BytesIO()
            small.save(out, "JPEG", quality=quality)
            return out.getvalue()
    except Exception as e:  # noqa: BLE001 - let the vision provider judge the original
        log.info("photo: no downscale (%s)", type(e).__name__)
        return image
