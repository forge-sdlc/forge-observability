"""Tests for forge.observability.config."""

from pydantic import SecretStr

from forge.observability.config import ClickHouseConfig, Settings, get_settings


def test_langfuse_url():
    s = Settings(langfuse_host="langfuse.internal", langfuse_port=4000)
    assert s.langfuse_url == "http://langfuse.internal:4000"


def test_langfuse_url_default_port():
    s = Settings(langfuse_host="localhost", langfuse_port=3000)
    assert s.langfuse_url == "http://localhost:3000"


def test_langfuse_enabled_with_both_keys():
    s = Settings(langfuse_public_key="pk-test", langfuse_secret_key=SecretStr("sk-test"))
    assert s.langfuse_enabled is True


def test_langfuse_enabled_missing_secret():
    s = Settings(langfuse_public_key="pk-test", langfuse_secret_key=SecretStr(""))
    assert s.langfuse_enabled is False


def test_langfuse_enabled_missing_public():
    s = Settings(langfuse_public_key="", langfuse_secret_key=SecretStr("sk-test"))
    assert s.langfuse_enabled is False


def test_langfuse_enabled_no_credentials():
    s = Settings(langfuse_public_key="", langfuse_secret_key=SecretStr(""))
    assert s.langfuse_enabled is False


def test_clickhouse_password_is_secret():
    cfg = ClickHouseConfig(password=SecretStr("hunter2"))
    assert cfg.password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(cfg)


def test_env_var_overrides_clickhouse_host(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch.internal")
    cfg = ClickHouseConfig()
    assert cfg.host == "ch.internal"


def test_env_var_overrides_langfuse_port(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PORT", "4000")
    s = Settings()
    assert s.langfuse_port == 4000


def test_env_var_skip_dbt(monkeypatch):
    monkeypatch.setenv("FORGE_OBSERVABILITY_WORKER_SKIP_DBT", "true")
    s = Settings()
    assert s.forge_observability_worker_skip_dbt is True


def test_backend_config_defaults_to_clickhouse():
    s = Settings()
    assert isinstance(s.backend_config, ClickHouseConfig)


def test_get_settings_returns_same_instance():
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()


def test_langfuse_collection_defaults():
    s = Settings()
    assert s.langfuse_page_size == 50
    assert s.langfuse_collection_lag_seconds == 1800


def test_langfuse_collection_env_override(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PAGE_SIZE", "100")
    monkeypatch.setenv("LANGFUSE_COLLECTION_LAG_SECONDS", "300")
    s = Settings(_env_file=None)
    assert s.langfuse_page_size == 100
    assert s.langfuse_collection_lag_seconds == 300
