"""
Centralized application settings loaded dynamically from environment variables and .env.
Ensures zero hardcoded runtime configurations in business logic.
"""
from typing import Union
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -----------------------------------------------------------------------
    # LLM & AI Model Settings
    # -----------------------------------------------------------------------
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    gemini_model: str = Field(default="gemini-2.5-flash", validation_alias=AliasChoices("GEMINI_MODEL", "LLM_MODEL"))
    llm_temperature: float = Field(default=0.2, validation_alias=AliasChoices("LLM_TEMPERATURE", "TEMPERATURE"))

    # -----------------------------------------------------------------------
    # Live Email Transport Settings (SMTP / IMAP)
    # -----------------------------------------------------------------------
    smtp_server: str = Field(default="smtp.gmail.com", validation_alias=AliasChoices("SMTP_SERVER", "EMAIL_SMTP_SERVER"))
    smtp_port: int = Field(default=587, validation_alias=AliasChoices("SMTP_PORT", "EMAIL_SMTP_PORT"))
    imap_server: str = Field(default="imap.gmail.com", validation_alias=AliasChoices("IMAP_SERVER", "EMAIL_IMAP_SERVER"))
    email_user: str = Field(default="", validation_alias=AliasChoices("EMAIL_USER", "GMAIL_USER", "SMTP_USER"))
    email_pass: str = Field(default="", validation_alias=AliasChoices("EMAIL_PASS", "GMAIL_PASS", "SMTP_PASS", "EMAIL_PASSWORD"))
    my_test_email: str = Field(default="", validation_alias=AliasChoices("MY_TEST_EMAIL", "TEST_BUYER_EMAIL", "RECIPIENT_EMAIL"))

    # -----------------------------------------------------------------------
    # Server & Networking
    # -----------------------------------------------------------------------
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "DEFAULT_APP_PORT", "SERVER_PORT"))
    host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("HOST", "SERVER_HOST"))
    cors_origins: Union[list[str], str] = Field(
        default=["*"],
        validation_alias=AliasChoices("CORS_ORIGINS", "ALLOWED_ORIGINS"),
    )

    # -----------------------------------------------------------------------
    # Live Market Web Scraper & Cache Configuration
    # -----------------------------------------------------------------------
    market_source_url: str = Field(
        default="https://query1.finance.yahoo.com/v8/finance/chart/ZR=F?interval=1d&range=5d",
        validation_alias=AliasChoices("MARKET_SOURCE_URL", "YAHOO_RICE_URL", "THAI_RICE_URL"),
    )
    market_cache_file: str = Field(
        default="market_cache.json",
        validation_alias=AliasChoices("MARKET_CACHE_FILE", "CACHE_FILE_PATH"),
    )
    market_cache_ttl_seconds: int = Field(
        default=21600,  # 6 hours
        validation_alias=AliasChoices("MARKET_DATA_CACHE_TTL_SECONDS", "MARKET_CACHE_TTL"),
    )
    market_fetch_timeout_seconds: float = Field(
        default=3.5,
        validation_alias=AliasChoices("MARKET_FETCH_TIMEOUT_SECONDS", "MARKET_TIMEOUT"),
    )
    default_benchmark_rate: float = Field(
        default=900.0,
        validation_alias=AliasChoices("DEFAULT_BENCHMARK_RATE", "FALLBACK_BENCHMARK"),
    )
    default_freight_rate: float = Field(
        default=50.0,
        validation_alias=AliasChoices("DEFAULT_FREIGHT_RATE", "FALLBACK_FREIGHT"),
    )
    default_fuel_surcharge_pct: float = Field(
        default=0.0,
        validation_alias=AliasChoices("DEFAULT_FUEL_SURCHARGE_PCT", "FREIGHT_BAF_PCT"),
    )

    # -----------------------------------------------------------------------
    # Default Campaign Arbitrage Parameters
    # -----------------------------------------------------------------------
    default_commodity: str = Field(
        default="Basmati 1121",
        validation_alias=AliasChoices("DEFAULT_COMMODITY", "COMMODITY"),
    )
    default_target_volume_mt: float = Field(
        default=500.0,
        validation_alias=AliasChoices("DEFAULT_TARGET_VOLUME_MT", "TARGET_VOLUME_MT"),
    )
    default_target_margin_pct: float = Field(
        default=10.0,
        validation_alias=AliasChoices("DEFAULT_TARGET_MARGIN_PCT", "TARGET_MARGIN_PCT"),
    )
    default_max_variance_pct: float = Field(
        default=5.0,
        validation_alias=AliasChoices("DEFAULT_MAX_VARIANCE_PCT", "MAX_VARIANCE_PCT"),
    )
    default_buffer_usd_per_mt: float = Field(
        default=20.0,
        validation_alias=AliasChoices("DEFAULT_BUFFER_USD_PER_MT", "BUFFER_USD_PER_MT"),
    )
    default_destination_port: str = Field(
        default="Jebel Ali",
        validation_alias=AliasChoices("DEFAULT_DESTINATION_PORT", "DESTINATION_PORT"),
    )
    default_origin_port: str = Field(
        default="Karachi",
        validation_alias=AliasChoices("DEFAULT_ORIGIN_PORT", "ORIGIN_PORT"),
    )
    default_payment_terms: str = Field(
        default="100% LC at sight",
        validation_alias=AliasChoices("DEFAULT_PAYMENT_TERMS", "PAYMENT_TERMS"),
    )
    default_broken_percentage: float = Field(
        default=5.0,
        validation_alias=AliasChoices("DEFAULT_BROKEN_PERCENTAGE", "BROKEN_PERCENTAGE"),
    )

    # -----------------------------------------------------------------------
    # Trading Desk Identity & Sign-Off
    # -----------------------------------------------------------------------
    desk_name: str = Field(
        default="Global Agro Arbitrage Desk",
        validation_alias=AliasChoices("DESK_NAME", "TRADING_DESK_NAME"),
    )
    desk_email: str = Field(
        default="trading@arbitrage-desk.com",
        validation_alias=AliasChoices("DESK_EMAIL", "TRADING_DESK_EMAIL"),
    )


# Singleton configuration instance
settings = Settings()
