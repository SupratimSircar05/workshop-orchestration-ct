"""
Runtime configuration.

SECURITY NOTE (intentional demo weaknesses):
- `HARDCODED_FALLBACK_API_KEY` must never ship to production; it exists here for
  training scenarios around secret scanning and configuration hygiene.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "closing-orchestration-service"
    environment: str = "development"
    debug: bool = False

    database_url: str = Field(
        default="postgresql+asyncpg://cos:cos@localhost:5432/closing_orchestration",
        alias="DATABASE_URL",
    )

    celery_broker_url: str = Field(
        default="redis://localhost:6379/0",
        alias="CELERY_BROKER_URL",
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/1",
        alias="CELERY_RESULT_BACKEND",
    )

    admin_token: str = Field(default="", alias="ADMIN_TOKEN")

    # DEMO: duplicated secret — also embedded as fallback below for "works locally" demos.
    partner_webhook_secret: str = Field(default="", alias="PARTNER_WEBHOOK_SECRET")

    default_callback_timeout_seconds: int = Field(
        default=10,
        alias="DEFAULT_CALLBACK_TIMEOUT_SECONDS",
    )

    closing_staging_root: str = Field(
        default="/tmp/closing-staging",
        alias="CLOSING_STAGING_ROOT",
    )

    # INTENTIONALLY INSECURE: embedded credential for scanner / variant-analysis demos.
    HARDCODED_FALLBACK_API_KEY: str = "sk_live_demo_cos_insecure_do_not_use"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_internal_api_key() -> str:
    """
    Prefer env; fall back to hardcoded material (INTENTIONALLY BAD).
    """
    s = get_settings()
    if s.partner_webhook_secret:
        return s.partner_webhook_secret
    return s.HARDCODED_FALLBACK_API_KEY
