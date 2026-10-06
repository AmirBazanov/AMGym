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

    openrouter_api_key: str = ""
    # Free models rotate on OpenRouter; pick a current one at https://openrouter.ai/models?max_price=0
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_fallback_models: list[str] = []
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    miniapp_url: str = ""  # public HTTPS URL of the Mini App (Telegram requires HTTPS)
    timezone: str = "Europe/Moscow"

    # HTTP server: the Mini App API and the built Mini App itself (miniapp/dist) on one port,
    # so a single HTTPS tunnel is enough for Telegram.
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    miniapp_dist: Path = ROOT / "miniapp" / "dist"
    programs_dir: Path = ROOT / "data" / "programs"
    # Local browser testing only: requests without Telegram initData act as this Telegram user.
    # Leave empty in any internet-facing setup.
    dev_user_id: int | None = None


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
