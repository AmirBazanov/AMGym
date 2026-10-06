"""Speech-to-text client: request shape, error mapping, the key never leaks into errors."""

import httpx
import pytest

from gymbot.stt import STTError, STTRateLimited, transcribe

SENTINEL = "gsk_SECRET_SENTINEL_123"
AUDIO = b"OggS\x00\x01fake-audio-bytes\xff\xfe"


@pytest.fixture
def stt_settings(settings):
    return settings.model_copy(update={"stt_api_key": SENTINEL})


async def run(handler, settings, audio: bytes = AUDIO, filename: str = "voice.ogg") -> str:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await transcribe(audio, filename, settings, http)


def ok(text="привет"):
    return lambda req: httpx.Response(200, json={"text": text})


@pytest.mark.parametrize("base", ["https://stt.example/v1", "https://stt.example/v1/"])
async def test_request_shape(stt_settings, base):
    settings = stt_settings.model_copy(update={"stt_base_url": base})
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"text": "ok"})

    await run(handler, settings)

    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://stt.example/v1/audio/transcriptions"
    assert req.headers["Authorization"] == f"Bearer {SENTINEL}"
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="model"' in body
    assert settings.stt_model.encode() in body
    assert b'name="language"' in body
    assert b"ru" in body
    assert b'name="file"' in body
    assert b'filename="voice.ogg"' in body
    assert AUDIO in body


async def test_filename_is_passed_through(stt_settings):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"text": "ok"})

    await run(handler, stt_settings, filename="voice.wav")
    assert b'filename="voice.wav"' in seen[0].content


async def test_text_is_stripped(stt_settings):
    assert await run(ok("  жим лёжа  "), stt_settings) == "жим лёжа"


async def test_rate_limited(stt_settings):
    with pytest.raises(STTRateLimited) as ei:
        await run(lambda req: httpx.Response(429, json={"error": "slow down"}), stt_settings)
    assert isinstance(ei.value, STTError)


@pytest.mark.parametrize("status", [500, 503])
async def test_server_errors(stt_settings, status):
    with pytest.raises(STTError) as ei:
        await run(lambda req: httpx.Response(status, text="boom"), stt_settings)
    assert not isinstance(ei.value, STTRateLimited)
    assert str(status) in str(ei.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json at all"),
        httpx.Response(200, json={"result": "no text key"}),
        httpx.Response(200, json=["text"]),
        httpx.Response(200, json={"text": None}),
        httpx.Response(200, json={"text": 123}),
    ],
    ids=["non-json", "no-text-key", "json-list", "text-null", "text-not-str"],
)
async def test_unexpected_200_body(stt_settings, response):
    with pytest.raises(STTError):
        await run(lambda req: response, stt_settings)


async def test_network_error(stt_settings):
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(STTError) as ei:
        await run(handler, stt_settings)
    assert not isinstance(ei.value, STTRateLimited)
    assert ei.value.__cause__ is None


def chain_strings(exc: BaseException) -> list[str]:
    out, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        out += [str(exc), repr(exc)]
        exc = exc.__cause__ or exc.__context__
    return out


def echo_key(req):
    return httpx.Response(401, json={"error": f"Invalid API key {SENTINEL}"})


def connect_error(req):
    raise httpx.ConnectError("boom", request=req)


@pytest.mark.parametrize(
    "handler",
    [
        lambda req: httpx.Response(429, json={"error": f"limit for {SENTINEL}"}),
        lambda req: httpx.Response(500, json={"error": f"oops {SENTINEL}"}),
        echo_key,
        lambda req: httpx.Response(403, text=SENTINEL),
        lambda req: httpx.Response(200, text=f"garbage {SENTINEL}"),
        connect_error,
    ],
    ids=["429", "500", "401", "403", "200-garbage", "connect-error"],
)
async def test_key_does_not_leak(stt_settings, handler):
    with pytest.raises(STTError) as ei:
        await run(handler, stt_settings)
    for s in chain_strings(ei.value):
        assert SENTINEL not in s
    assert ei.value.__cause__ is None


async def test_key_scrubbed_from_network_error_text(stt_settings):
    def handler(req):
        raise httpx.ConnectError(f"boom {SENTINEL}", request=req)

    with pytest.raises(STTError) as ei:
        await run(handler, stt_settings)
    # What gets logged is the STTError itself; the original error stays only as suppressed context.
    assert SENTINEL not in str(ei.value) and SENTINEL not in repr(ei.value)
    assert "***" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
