"""Settings loaded from environment / .env. Never hardcode secrets."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]  # repo root: bot/src/gymbot/config.py -> ../../..


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT / ".env", ".env"), extra="ignore")

    bot_token: str
    # Telegram user ids allowed to use the bot (personal app). Empty = allow everyone.
    allowed_user_ids: list[int] = []

    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'data' / 'gym.db'}"

    # Chat LLM: Groq first (free tier, fast, separate limits per model), OpenRouter free models as the
    # last fallback. Routes are tried in this order: GROQ_MODELS, then OPENROUTER_MODEL and its fallbacks.
    # Empty GROQ_API_KEY = the STT key when STT goes to Groq (same account); no key at all = no Groq routes.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Checked 2026-10-07: 1000 requests/day and 8000 tokens/min per model. qwen first: gpt-oss-120b
    # misread the first message as a correction of a few-shot example.
    groq_models: list[str] = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]

    openrouter_api_key: str = ""
    # Free models rotate on OpenRouter; pick a current one at https://openrouter.ai/models?max_price=0
    # Checked 2026-10-06: the free tier is small and changes often. If parsing starts failing with
    # "all models failed", pick a current free model there and set OPENROUTER_MODEL in .env.
    openrouter_model: str = "inclusionai/ling-3.1-flash"
    openrouter_fallback_models: list[str] = ["inclusionai/ling-3.0-flash-sante:free", "apodex/apodex-1.1-mini:free"]
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Speech-to-text for voice messages: any OpenAI-compatible /audio/transcriptions endpoint.
    # Default is Groq's free tier (key at https://console.groq.com/keys). Empty key = voice is off.
    stt_api_key: str = ""
    stt_base_url: str = "https://api.groq.com/openai/v1"
    stt_model: str = "whisper-large-v3-turbo"
    stt_max_seconds: int = 120

    miniapp_url: str = ""  # public HTTPS URL of the Mini App (Telegram requires HTTPS)
    timezone: str = "Europe/Moscow"

    # HTTP server: the Mini App API and the built Mini App itself (miniapp/dist) on one port,
    # so a single HTTPS tunnel is enough for Telegram.
    api_host: str = "127.0.0.1"  # the tunnel connects locally; no need to listen on the LAN
    api_port: int = 8000
    miniapp_dist: Path = ROOT / "miniapp" / "dist"
    programs_dir: Path = ROOT / "data" / "programs"
    # Local browser testing only: requests without Telegram initData act as this Telegram user.
    # Leave empty in any internet-facing setup.
    dev_user_id: int | None = None
    # false = only the HTTP server (Mini App + API), handy for UI work without Telegram access.
    run_bot: bool = True


    @property
    def groq_key(self) -> str:
        """GROQ_API_KEY, else STT_API_KEY if speech-to-text goes to Groq too (never another provider's key)."""
        if self.groq_api_key:
            return self.groq_api_key
        return self.stt_api_key if "groq.com" in self.stt_base_url else ""


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
