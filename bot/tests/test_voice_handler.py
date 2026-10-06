"""Voice handler: guards, transcript hand-off to process_text, user-facing error messages."""

import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gymbot.handlers import voice
from gymbot.stt import STTError, STTRateLimited


def make_message(duration: int = 5, audio: bytes = b"OGG"):
    return SimpleNamespace(
        voice=SimpleNamespace(duration=duration, file_id="f"),
        chat=SimpleNamespace(id=42),
        from_user=SimpleNamespace(id=42, full_name="Amir"),
        date=datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        answer=AsyncMock(),
        bot=SimpleNamespace(
            send_chat_action=AsyncMock(), download=AsyncMock(return_value=io.BytesIO(audio))
        ),
    )


@pytest.fixture
def stt_settings(settings):
    return settings.model_copy(update={"stt_api_key": "key"})


@pytest.fixture
def process(monkeypatch):
    fake = AsyncMock()
    monkeypatch.setattr(voice, "process_text", fake)
    return fake


def patch_transcribe(monkeypatch, result=None, exc=None):
    fake = AsyncMock(return_value=result, side_effect=exc)
    monkeypatch.setattr(voice, "transcribe", fake)
    return fake


async def call(message, settings):
    sm, llm = object(), object()
    await voice.log_voice(message, settings, sm, llm)
    return sm, llm


async def test_no_key(settings, monkeypatch, process):
    assert settings.stt_api_key == ""
    tr = patch_transcribe(monkeypatch, "x")
    msg = make_message()

    await call(msg, settings)

    msg.answer.assert_awaited_once_with(voice.NO_KEY)
    tr.assert_not_awaited()
    msg.bot.download.assert_not_awaited()
    process.assert_not_awaited()


async def test_too_long(stt_settings, monkeypatch, process):
    assert stt_settings.stt_max_seconds == 120
    tr = patch_transcribe(monkeypatch, "x")
    msg = make_message(duration=121)

    await call(msg, stt_settings)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Слишком длинное" in text
    assert "2 минут" in text
    tr.assert_not_awaited()
    msg.bot.download.assert_not_awaited()
    process.assert_not_awaited()


async def test_exactly_max_duration_is_accepted(stt_settings, monkeypatch, process):
    patch_transcribe(monkeypatch, "жим")
    msg = make_message(duration=120)

    await call(msg, stt_settings)

    process.assert_awaited_once()
    msg.answer.assert_not_awaited()


async def test_happy_path(stt_settings, monkeypatch, process):
    tr = patch_transcribe(monkeypatch, " жим лёжа три по десять ")
    msg = make_message()

    sm, llm = await call(msg, stt_settings)

    tr.assert_awaited_once_with(b"OGG", "voice.ogg", stt_settings)
    msg.bot.send_chat_action.assert_awaited_once_with(42, "typing")
    process.assert_awaited_once_with(
        msg,
        "жим лёжа три по десять",
        stt_settings,
        sm,
        llm,
        raw_text="[voice] жим лёжа три по десять",
        prefix="Распознал: «жим лёжа три по десять»\n\n",
    )
    msg.answer.assert_not_awaited()


@pytest.mark.parametrize("transcript", ["", "   ", " \n\t "])
async def test_empty_transcript(stt_settings, monkeypatch, process, transcript):
    patch_transcribe(monkeypatch, transcript)
    msg = make_message()

    await call(msg, stt_settings)

    msg.answer.assert_awaited_once_with(voice.EMPTY)
    process.assert_not_awaited()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (STTRateLimited("429"), voice.RATE_LIMITED),
        (STTError("HTTP 500"), voice.STT_FAILED),
    ],
    ids=["rate-limited", "failed"],
)
async def test_stt_errors(stt_settings, monkeypatch, process, exc, expected):
    patch_transcribe(monkeypatch, exc=exc)
    msg = make_message()

    await call(msg, stt_settings)

    msg.answer.assert_awaited_once_with(expected)
    process.assert_not_awaited()


async def test_download_returns_none(stt_settings, monkeypatch, process):
    tr = patch_transcribe(monkeypatch, "x")
    msg = make_message()
    msg.bot.download = AsyncMock(return_value=None)

    await call(msg, stt_settings)

    msg.answer.assert_awaited_once_with(voice.STT_FAILED)
    tr.assert_not_awaited()
    process.assert_not_awaited()
