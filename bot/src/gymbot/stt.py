"""Speech-to-text over an OpenAI-compatible /audio/transcriptions endpoint (Groq by default).

The API key goes only into the Authorization header: it never appears in errors or logs.
"""

from __future__ import annotations

import httpx

from gymbot.config import Settings

TIMEOUT = 30.0

# Vocabulary hint for Whisper (`prompt`, it reads ~224 tokens): words it otherwise mishears,
# e.g. "касушку" (a bowl-sized portion) came out as "косушку", "лепёшка" as "лепёка".
STT_HINT = (
    "Дневник питания и тренировок: плов, каса, касушка, лепёшка, самса, лагман, шурпа, манты, чучвара, "
    "творог, гречка, грудка; жим лёжа, присед, тяга, подходы, повторения, дропсет, килограмм."
)


class STTError(RuntimeError):
    """Recognition failed. The message is for logs: status and a short body snippet, never the key."""


class STTRateLimited(STTError):
    """429: the free tier limit is used up for now."""


def _snippet(resp: httpx.Response, key: str) -> str:
    if resp.status_code in (401, 403):  # auth errors may echo part of the key back
        return ""
    body = resp.text[:200]
    return body.replace(key, "***") if key else body


async def transcribe(
    audio: bytes, filename: str, settings: Settings, http: httpx.AsyncClient | None = None
) -> str:
    """Recognize Russian speech in `audio`. `filename` tells the server the format (voice.ogg, voice.wav)."""
    if http is None:
        async with httpx.AsyncClient(timeout=TIMEOUT) as own:
            return await transcribe(audio, filename, settings, own)
    key = settings.stt_api_key
    try:
        resp = await http.post(
            f"{settings.stt_base_url.rstrip('/')}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data={"model": settings.stt_model, "language": "ru", "response_format": "json", "prompt": STT_HINT},
            files={"file": (filename, audio)},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as e:
        # The key is only in a header; scrub it anyway in case an error message ever quotes one.
        detail = str(e).replace(key, "***") if key else str(e)
        raise STTError(f"STT request failed: {type(e).__name__}: {detail[:200]}") from None
    if resp.status_code == 429:
        raise STTRateLimited(f"STT rate limited (429): {_snippet(resp, key)}")
    if resp.status_code != 200:
        raise STTError(f"STT HTTP {resp.status_code}: {_snippet(resp, key)}")
    try:
        text = resp.json()["text"]
    except (ValueError, KeyError, TypeError):
        raise STTError(f"STT unexpected response: {_snippet(resp, key)}") from None
    if not isinstance(text, str):
        raise STTError("STT unexpected response: text is not a string")
    return text.strip()
