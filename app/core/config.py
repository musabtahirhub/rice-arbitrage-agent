"""
Application runtime settings powered by pydantic-settings.
"""

from __future__ import annotations

import os
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Centralized, validated environment configuration.
    Values are automatically loaded from .env and system environment.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini LLM Authentication
    gemini_api_key: str = Field(
        default="",
        alias="GEMINI_API_KEY",
        description="Google Gemini Flash API key for extraction and drafting",
    )

    # Email Service Configuration
    email_provider: Literal["mock", "smtp", "resend"] = Field(
        default="mock",
        alias="EMAIL_PROVIDER",
        description="Active email provider interface",
    )
    resend_api_key: str = Field(
        default="",
        alias="RESEND_API_KEY",
        description="API key for Resend email delivery",
    )
    smtp_host: str = Field(
        default="smtp.gmail.com",
        alias="SMTP_HOST",
        description="SMTP server host",
    )
    smtp_port: int = Field(
        default=587,
        alias="SMTP_PORT",
        description="SMTP server port (usually 587 for TLS)",
    )
    smtp_user: str = Field(
        default="",
        alias="SMTP_USER",
        description="SMTP username / email address",
    )
    smtp_password: str = Field(
        default="",
        alias="SMTP_PASSWORD",
        description="SMTP account or app-specific password",
    )

    # Operator Alerts & Webhooks
    operator_notification_webhook: str = Field(
        default="",
        alias="OPERATOR_NOTIFICATION_WEBHOOK",
        description="Webhook URL to notify trading desk operators of approved deals",
    )

    # Market Data Caching
    market_data_cache_ttl_seconds: int = Field(
        default=21600,  # 6 hours
        alias="MARKET_DATA_CACHE_TTL_SECONDS",
        description="TTL duration in seconds before market rates re-fetch",
    )

    # Runtime Server Settings
    default_app_port: int = Field(
        default=8000,
        alias="DEFAULT_APP_PORT",
        description="Port for Uvicorn web server",
    )
    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
        description="Standard logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:8000", "http://127.0.0.1:8000"],
        alias="CORS_ORIGINS",
        description="Authorized origins for CORS headers",
    )


# Singleton instance
settings = Settings()
