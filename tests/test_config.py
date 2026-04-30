"""Tests for forge_observability.config — settings loading and computed properties."""

from pydantic import SecretStr

from forge.observability.config import Settings


def _make(**kwargs) -> Settings:
    """Build a Settings instance without reading any .env file."""
    return Settings(_env_file=None, **kwargs)


class TestClickHouseDefaults:
    def test_defaults(self):
        s = _make()
        assert s.clickhouse_host == "localhost"
        assert s.clickhouse_port == 9000
        assert s.clickhouse_http_port == 8123
        assert s.clickhouse_database == "default"
        assert s.clickhouse_user == "forge"

    def test_dsn_format(self):
        s = _make(
            clickhouse_user="u",
            clickhouse_password=SecretStr("p"),
            clickhouse_host="ch-host",
            clickhouse_http_port=8124,
            clickhouse_database="mydb",
        )
        assert s.datastore_dsn == "clickhousedb://u:p@ch-host:8124/mydb"


class TestApiSettings:
    def test_defaults(self):
        s = _make()
        assert s.forge_observability_api_port == 8010
        assert s.forge_observability_api_log_level == "INFO"
        assert s.forge_observability_worker_log_level == "INFO"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FORGE_OBSERVABILITY_API_PORT", "9999")
        monkeypatch.setenv("FORGE_OBSERVABILITY_API_LOG_LEVEL", "DEBUG")
        s = Settings(_env_file=None)
        assert s.forge_observability_api_port == 9999
        assert s.forge_observability_api_log_level == "DEBUG"


class TestLangfuseUrl:
    def test_bare_hostname_constructs_url(self):
        s = _make(langfuse_host="langfuse-svc", langfuse_port=3000)
        assert s.langfuse_url == "http://langfuse-svc:3000"

    def test_langfuse_enabled_requires_both_keys(self):
        s = _make(langfuse_public_key="pk", langfuse_secret_key=SecretStr("sk"))
        assert s.langfuse_enabled is True

    def test_langfuse_disabled_when_keys_missing(self):
        s = _make(langfuse_public_key="", langfuse_secret_key=SecretStr(""))
        assert s.langfuse_enabled is False


class TestPrometheusUrl:
    def test_default(self):
        s = _make()
        assert s.prometheus_url == "http://localhost:9090"

    def test_bare_host_and_port(self):
        s = _make(prometheus_host="prom-svc", prometheus_port=9091)
        assert s.prometheus_url == "http://prom-svc:9091"


class TestGitHubHelpers:
    def test_known_repos_parses_csv(self):
        s = _make(github_known_repos="org/a, org/b , org/c")
        assert s.known_repos == ["org/a", "org/b", "org/c"]

    def test_known_repos_empty(self):
        s = _make(github_known_repos="")
        assert s.known_repos == []
