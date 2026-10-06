"""Settings loaded from environment / .env. Never hardcode secrets."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    bot_token: str
    # Telegram user ids allowed to use the bot (personal app). Empty = allow everyone.
    allowed_user_ids: list[int] = []

    database_url: str = "sqlite+aiosqlite:///./data/gym.db"

    openrouter_api_key: str = ""
    # Free models rotate on OpenRouter; pick a current one at https://openrouter.ai/models?max_price=0
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_fallback_models: list[str] = []
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    miniapp_url: str = ""  # public HTTPS URL of the Mini App (Telegram requires HTTPS)
    timezone: str = "Europe/Moscow"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
