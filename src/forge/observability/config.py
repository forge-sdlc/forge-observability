"""Standalone configuration for forge-observability.

All settings are read from environment variables (or a .env file).
No dependency on the forge package.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── ClickHouse analytical store ───────────────────────────────────────
    clickhouse_host: str = Field(default="localhost")
    clickhouse_port: int = Field(default=9000, description="Native protocol port")
    clickhouse_http_port: int = Field(default=8123, description="HTTP interface port")
    clickhouse_database: str = Field(default="default")
    clickhouse_user: str = Field(default="forge")
    clickhouse_password: SecretStr = Field(default=SecretStr("forge"))

    # ── Forge Observability Worker ────────────────────────────────────────
    forge_observability_worker_log_level: str = Field(default="INFO")
    forge_observability_worker_skip_dbt: bool = Field(default=False)

    # ── Langfuse ──────────────────────────────────────────────────────────
    langfuse_host: str = Field(default="localhost")
    langfuse_port: int = Field(default=3000)
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: SecretStr = Field(default=SecretStr(""))
    langfuse_page_size: int = Field(default=50)
    langfuse_collection_lag_seconds: int = Field(default=1800)

    # ── Pipeline intervals ────────────────────────────────────────────────
    langfuse_interval_seconds: int = Field(
        default=60, description="How often dlt loads bronze LLM observability tables"
    )

    # ── Computed helpers ──────────────────────────────────────────────────

    @property
    def langfuse_url(self) -> str:
        """Full Langfuse base URL."""
        # TODO: TLS Support
        return f"http://{self.langfuse_host}:{self.langfuse_port}"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
