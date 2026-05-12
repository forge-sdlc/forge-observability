"""Tests for forge.observability.config."""

from pydantic import SecretStr

from forge.observability.config import Settings, get_settings


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
    s = Settings(clickhouse_password=SecretStr("hunter2"))
    assert s.clickhouse_password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(s)


def test_env_var_overrides_clickhouse_host(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_HOST", "ch.internal")
    s = Settings()
    assert s.clickhouse_host == "ch.internal"


def test_env_var_overrides_langfuse_port(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PORT", "4000")
    s = Settings()
    assert s.langfuse_port == 4000


def test_env_var_skip_dbt(monkeypatch):
    monkeypatch.setenv("FORGE_OBSERVABILITY_WORKER_SKIP_DBT", "true")
    s = Settings()
    assert s.forge_observability_worker_skip_dbt is True


def test_get_settings_returns_same_instance():
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()
