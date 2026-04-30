"""Standalone configuration for forge-observability.

All settings are read from environment variables (or a .env file).
No dependency on the forge package.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# TODO: Add fine-grained metrics configurations:


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

    # ── Forge Observability API ───────────────────────────────────────────
    forge_observability_api_port: int = Field(default=8010)
    forge_observability_api_log_level: str = Field(default="INFO")

    # ── Forge Observability Worker ────────────────────────────────────────
    forge_observability_worker_log_level: str = Field(default="INFO")

    # ── Langfuse ──────────────────────────────────────────────────────────
    langfuse_host: str = Field(default="localhost")
    langfuse_port: int = Field(default=3000)
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: SecretStr = Field(default=SecretStr(""))

    # ── Prometheus ────────────────────────────────────────────────────────
    # TODO: Prometheus Auth
    prometheus_host: str = Field(default="localhost")
    prometheus_port: int = Field(default=9090)

    # ── GitHub ────────────────────────────────────────────────────────────
    github_token: SecretStr = Field(default=SecretStr(""))
    # Comma-separated list of owner/repo strings, e.g. "myorg/myrepo,myorg/other"
    github_known_repos: str = Field(default="")

    # ── JIRA ──────────────────────────────────────────────────────────────
    jira_base_url: str = Field(default="")
    jira_user_email: str = Field(default="")
    jira_api_token: SecretStr = Field(default=SecretStr(""))

    # ── Pipeline intervals ────────────────────────────────────────────────
    langfuse_interval_seconds: int = Field(
        default=60, description="How often dlt loads bronze LLM observability tables"
    )
    prometheus_interval_seconds: int = Field(
        default=300, description="How often dlt loads bronze prometheus tables"
    )
    github_interval_seconds: int = Field(
        default=600, description="How often dlt loads bronze GitHub tables"
    )
    jira_interval_seconds: int = Field(
        default=600, description="How often dlt loads bronze jira tables"
    )
    dbt_interval_seconds: int = Field(
        default=300, description="How often dbt rebuilds silver and gold views"
    )

    # ── Computed helpers ──────────────────────────────────────────────────

    @property
    def langfuse_url(self) -> str:
        """Full Langfuse base URL."""
        # TODO: TLS Support
        return f"http://{self.langfuse_host}:{self.langfuse_port}"

    @property
    def prometheus_url(self) -> str:
        """Full Prometheus base URL."""
        # TODO: TLS Support
        return f"http://{self.prometheus_host}:{self.prometheus_port}"

    @property
    def datastore_dsn(self) -> str:
        """External datastore native DSN for dlt destination."""
        # TODO: Support Multiple Backends
        pw = self.clickhouse_password.get_secret_value()
        return (
            f"clickhousedb://{self.clickhouse_user}:{pw}"
            f"@{self.clickhouse_host}:{self.clickhouse_http_port}"
            f"/{self.clickhouse_database}"
        )

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key.get_secret_value())

    @property
    def prometheus_enabled(self) -> bool:
        # TODO: check for prometheus credentials to enable the prometheus dlt pipeline
        return True

    @property
    def jira_enabled(self) -> bool:
        return bool(
            self.jira_base_url and self.jira_user_email and self.jira_api_token.get_secret_value()
        )

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token.get_secret_value())

    @property
    def known_repos(self) -> list[str]:
        """Parsed list of GitHub repositories."""
        return [r.strip() for r in self.github_known_repos.split(",") if r.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
