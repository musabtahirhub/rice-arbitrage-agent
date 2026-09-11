"""
Application configuration loaded from environment variables using pydantic-settings.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini LLM configuration
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Server settings
    port: int = 8000
    host: str = "127.0.0.1"

    # Default campaign arbitrage parameters
    default_target_margin_pct: float = 10.0
    default_max_variance_pct: float = 5.0
    default_buffer_usd_per_mt: float = 20.0


settings = Settings()
