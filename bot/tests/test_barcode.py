"""Barcodes on photos (services/barcode.py): decoding retail codes, "no barcode" on anything else, the downscale."""

import io
import random

import pytest
import zxingcpp
from PIL import Image

from gymbot.services import barcode

CANVAS = (2560, 1920)


def jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


def code_image(text: str, fmt: zxingcpp.BarcodeFormat, scale: int = 3) -> Image.Image:
    """The code rendered by zxing-cpp itself (no fixture files), as a grayscale PIL image."""
    mv = memoryview(zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, fmt), scale=scale))
    height, width = mv.shape[:2]
    return Image.frombytes("L", (width, height), bytes(mv))


def photo_with(code: Image.Image, canvas: tuple[int, int] = CANVAS) -> bytes:
    """A big white 'photo' with the code somewhere off-center, as JPEG bytes."""
    page = Image.new("L", canvas, 255)
    page.paste(code, (canvas[0] // 3, canvas[1] // 3))
    return jpeg(page)


def noise_photo(size: tuple[int, int] = CANVAS) -> bytes:
    """A 'plate': smooth gradients and blobs, no code."""
    rng = random.Random(7)
    im = Image.new("RGB", size, (200, 170, 120))
    for _ in range(40):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        r = rng.randrange(40, 200)
        im.paste((rng.randrange(256), rng.randrange(256), rng.randrange(256)), (x, y, x + r, y + r))
    return jpeg(im)


# ---- decode ----


@pytest.mark.parametrize(
    ("digits", "fmt"),
    [
        ("5000159407236", zxingcpp.BarcodeFormat.EAN13),
        ("96385074", zxingcpp.BarcodeFormat.EAN8),
    ],
    ids=["ean13", "ean8"],
)
def test_decode_finds_retail_codes(digits, fmt):
    assert barcode.decode(photo_with(code_image(digits, fmt))) == digits


def test_decode_upca_is_returned_as_thirteen_digits():
    # UPC-A is an EAN-13 with a leading zero: that is what Open Food Facts keys it by.
    found = barcode.decode(photo_with(code_image("036000291452", zxingcpp.BarcodeFormat.UPCA)))
    assert found == "0036000291452"


def test_decode_code_filling_the_whole_image():
    assert barcode.decode(jpeg(code_image("5000159407236", zxingcpp.BarcodeFormat.EAN13, scale=4))) == "5000159407236"


def test_decode_ignores_a_qr_code():
    assert barcode.decode(photo_with(code_image("https://example.com", zxingcpp.BarcodeFormat.QRCode, scale=6))) is None


def test_decode_plain_photo_is_none():
    assert barcode.decode(noise_photo()) is None


@pytest.mark.parametrize("junk", [b"", b"not an image at all", b"\xff\xd8\xff\xe0fake-jpeg-bytes\x00\x01"])
def test_decode_junk_is_none_not_an_exception(junk):
    assert barcode.decode(junk) is None


async def test_decode_async_matches_decode():
    data = photo_with(code_image("96385074", zxingcpp.BarcodeFormat.EAN8))
    assert await barcode.decode_async(data) == "96385074"
    assert await barcode.decode_async(b"junk") is None


# ---- downscale ----


def test_downscale_big_photo_gets_max_side_1280():
    out = barcode.downscale(noise_photo(), 1280)
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "JPEG"
        assert max(im.size) == 1280
        assert im.size == (1280, 960)  # the aspect ratio is kept


def test_downscale_portrait_photo_limits_the_height():
    out = barcode.downscale(noise_photo((1500, 3000)), 1280)
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (640, 1280)


def test_downscale_small_image_is_returned_unchanged():
    small = jpeg(Image.new("RGB", (1280, 720), "white"))
    assert barcode.downscale(small, 1280) is small
    tiny = jpeg(Image.new("RGB", (200, 100), "white"))
    assert barcode.downscale(tiny, 1280) == tiny


@pytest.mark.parametrize("junk", [b"", b"junk bytes", b"\xff\xd8\xff\xe0fake-jpeg-bytes\x00\x01"])
def test_downscale_junk_is_returned_unchanged(junk):
    assert barcode.downscale(junk, 1280) == junk


def test_a_big_enough_code_survives_the_downscale():
    """The code is read from the original, but a large one stays readable in the copy for the vision model too."""
    big = photo_with(code_image("5000159407236", zxingcpp.BarcodeFormat.EAN13, scale=8))
    assert barcode.decode(big) == "5000159407236"
    assert barcode.decode(barcode.downscale(big, 1280)) == "5000159407236"


# ---- rotation, size, orientation tag, checksum ----


@pytest.mark.parametrize("angle", [90, 180, 270, 12], ids=["r90", "r180", "r270", "tilt12"])
def test_decode_rotated_code(angle):
    code = code_image("5000159407236", zxingcpp.BarcodeFormat.EAN13).rotate(angle, expand=True, fillcolor=255)
    assert barcode.decode(photo_with(code, (1200, 900))) == "5000159407236"


@pytest.mark.parametrize("scale", [1, 2], ids=["narrowest-bars-1px", "bars-2px"])
def test_decode_small_code_on_a_big_photo(scale):
    # 113 px wide on a 2560 px frame: the smallest rendering of a code that is still crisp.
    code = code_image("96385074", zxingcpp.BarcodeFormat.EAN8, scale)
    assert barcode.decode(photo_with(code)) == "96385074"


def test_decode_small_photo():
    assert barcode.decode(photo_with(code_image("5000159407236", zxingcpp.BarcodeFormat.EAN13), (800, 400))) == (
        "5000159407236"
    )


def test_decode_picks_the_retail_code_when_a_qr_is_next_to_it():
    page = Image.new("L", CANVAS, 255)
    page.paste(code_image("https://example.com", zxingcpp.BarcodeFormat.QRCode, scale=6), (100, 100))
    page.paste(code_image("96385074", zxingcpp.BarcodeFormat.EAN8), (1500, 1200))
    assert barcode.decode(jpeg(page)) == "96385074"


def test_decode_ignores_other_linear_formats():
    # Code 128 is no retail code: a shelf label or a logistics sticker must not be taken for a product.
    assert barcode.decode(photo_with(code_image("12345678", zxingcpp.BarcodeFormat.Code128))) is None


def test_decode_png_works_too():
    out = io.BytesIO()
    code_image("5000159407236", zxingcpp.BarcodeFormat.EAN13).save(out, "PNG")
    assert barcode.decode(out.getvalue()) == "5000159407236"


def exif_jpeg(image: Image.Image, orientation: int) -> bytes:
    exif = image.getexif()
    exif[0x0112] = orientation
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=92, exif=exif)
    return out.getvalue()


def test_decode_photo_with_an_orientation_tag():
    page = Image.new("L", CANVAS, 255)
    page.paste(code_image("5000159407236", zxingcpp.BarcodeFormat.EAN13), (800, 600))
    for orientation in (3, 6, 8):
        assert barcode.decode(exif_jpeg(page, orientation)) == "5000159407236"


def test_downscale_applies_the_orientation_tag():
    # A phone saves landscape pixels with "rotate 90": the vision model must get the upright picture.
    out = barcode.downscale(exif_jpeg(Image.new("RGB", (2560, 1920), "gray"), 6), 1280)
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (960, 1280)


EAN13_L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"]
EAN13_G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"]
EAN13_R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"]
EAN13_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def check_digit(first12: str) -> int:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return (10 - total % 10) % 10


def drawn_ean13(digits: str, module: int = 4, height: int = 200) -> Image.Image:
    """An EAN-13 drawn by hand from the standard's tables, so the check digit can be wrong on purpose."""
    parity = EAN13_PARITY[int(digits[0])]
    bits = "101"
    for d, p in zip(digits[1:7], parity, strict=True):
        bits += (EAN13_L if p == "L" else EAN13_G)[int(d)]
    bits += "01010"
    for d in digits[7:]:
        bits += EAN13_R[int(d)]
    bits += "101"
    quiet = 10 * module
    im = Image.new("L", (len(bits) * module + 2 * quiet, height), 255)
    for i, bit in enumerate(bits):
        if bit == "1":
            im.paste(0, (quiet + i * module, 10, quiet + (i + 1) * module, height - 10))
    return im


def test_check_digit_helper_matches_zxing():
    assert check_digit("500015940723") == 6
    assert check_digit("036000291452") == 2


def test_hand_drawn_ean13_with_a_good_check_digit_is_read():
    # Guards the test encoder itself: otherwise the next test would pass for the wrong reason.
    assert barcode.decode(photo_with(drawn_ean13("5000159407236"))) == "5000159407236"


@pytest.mark.parametrize("wrong", [0, 1, 7, 9])
def test_decode_rejects_a_wrong_check_digit(wrong):
    assert wrong != 6
    assert barcode.decode(photo_with(drawn_ean13(f"500015940723{wrong}"))) is None
