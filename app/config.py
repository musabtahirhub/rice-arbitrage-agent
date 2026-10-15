from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
import json
from typing import Any


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    llm_temperature: float = 0.2

    # Langfuse Observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    imap_server: str = "imap.gmail.com"
    email_user: str = ""
    email_pass: str = ""
    my_test_email: str = ""
    # Test inbox that plays the supplier. When set, all supplier mail goes here instead of directory addresses.
    my_test_supplier_email: str = ""
    email_poll_interval_seconds: int = 15
    email_timeout_seconds: float = 30.0
    imap_mailbox_folder: str = "INBOX"

    google_pubsub_topic: str = ""
    google_credentials_json_path: str = "credentials.json"
    google_token_json_path: str = "token.json"
    # Headless Gmail OAuth (preferred): the server refreshes access tokens from these and never opens a
    # browser. Generate them once with `python scripts/gmail_auth.py`. If client id/secret are empty they
    # are read from credentials.json.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    use_push_webhooks: bool = True

    port: int = 8000
    host: str = "127.0.0.1"
    cors_origins: list[str] | str = ["*"]
    database_url: str = "postgresql://postgres:postgres@localhost:5432/arbitrage_desk"
    checkpointer_backend: str = "memory"
    checkpointer_allow_volatile_fallback: bool = False
    log_level: str = "INFO"
    log_file: str = ""  # empty = logs/arbitrage_desk.log

    market_source_url: str = "https://query1.finance.yahoo.com/v8/finance/chart/ZR=F?interval=1d&range=5d"
    market_cache_file: str = "market_cache.json"
    market_cache_ttl_seconds: int = 21600
    market_fetch_timeout_seconds: float = 3.5
    default_benchmark_rate: float = 900.0
    default_freight_rate: float = 50.0
    default_fuel_surcharge_pct: float = 0.0
    base_rough_rice_cwt_usd: float = 16.0
    broken_penalty_rate: float = 0.005
    broken_penalty_max_cap: float = 0.20

    default_commodity: str = "Basmati 1121"
    default_target_volume_mt: float = 500.0
    default_target_margin_pct: float = 10.0
    default_max_variance_pct: float = 5.0
    default_buffer_usd_per_mt: float = 20.0
    default_destination_port: str = "Jebel Ali"
    default_origin_port: str = "Karachi"
    default_payment_terms: str = "100% LC at sight"
    default_broken_percentage: float = 5.0

    default_min_profit_per_mt_hard: float = 50.0
    default_min_profit_per_mt_soft: float = 120.0
    default_max_negotiation_rounds: int = 3
    sco_anchor_premium_usd_per_mt: float = 30.0
    buyer_counter_step_usd_per_mt: float = 25.0
    supplier_counter_step_usd_per_mt: float = 20.0
    quantity_tolerance_pct: float = 2.0
    # Demo only: fabricate a supplier quote at benchmark FOB when a live buyer email arrives before any real
    # supplier quote. Off by default — a real supplier quote is required before a deal can be approved.
    simulate_supplier: bool = False

    desk_name: str = "Global Agro Arbitrage Desk"
    desk_email: str = "desk@arbitrage.com"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Any) -> list[str]:
        # 1. If unset (None), fallback to default wildcard
        if v is None:
            return ["*"]

        # 2. String input handling
        if isinstance(v, str):
            v_trimmed = v.strip()
            # Explicitly empty string -> allow no origins
            if not v_trimmed:
                return []

            # JSON array format: '["http://localhost:3000"]'
            if v_trimmed.startswith("[") and v_trimmed.endswith("]"):
                try:
                    parsed = json.loads(v_trimmed)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass

            # Comma-separated format: "http://localhost:3000,https://app.com"
            if "," in v_trimmed:
                return [s.strip() for s in v_trimmed.split(",") if s.strip()]

            # Single origin format: "http://localhost:3000" or "*"
            return [v_trimmed]

        # 3. List input handling (e.g. from Python code/tests)
        elif isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]

        return []

    def __getattr__(self, name: str):
        lower_name = name.lower()
        try:
            return object.__getattribute__(self, lower_name)
        except AttributeError:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")


settings = Settings()
