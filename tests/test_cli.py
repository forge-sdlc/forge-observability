"""Tests for forge.observability.cli."""

from unittest.mock import AsyncMock, MagicMock, patch

from forge.observability.cli import main


def _mock_settings(skip_dbt: bool = False) -> MagicMock:
    s = MagicMock()
    s.forge_observability_worker_skip_dbt = skip_dbt
    return s


def test_no_command_prints_help_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["forge-observability"])
    result = main()
    assert result == 0
    assert "usage:" in capsys.readouterr().out.lower()


def test_worker_default_flags(monkeypatch):
    monkeypatch.setattr("sys.argv", ["forge-observability", "worker"])
    with (
        patch("asyncio.run") as mock_asyncio_run,
        patch("forge.observability.worker.run_pipelines", new_callable=AsyncMock) as mock_run,
        patch("forge.observability.config.get_settings", return_value=_mock_settings()),
    ):
        result = main()

    assert result == 0
    mock_run.assert_called_once_with(once=False, skip_dbt=False)
    mock_asyncio_run.assert_called_once()


def test_worker_once_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["forge-observability", "worker", "--once"])
    with (
        patch("asyncio.run"),
        patch("forge.observability.worker.run_pipelines", new_callable=AsyncMock) as mock_run,
        patch("forge.observability.config.get_settings", return_value=_mock_settings()),
    ):
        result = main()

    assert result == 0
    mock_run.assert_called_once_with(once=True, skip_dbt=False)


def test_worker_skip_dbt_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["forge-observability", "worker", "--skip-dbt"])
    with (
        patch("asyncio.run"),
        patch("forge.observability.worker.run_pipelines", new_callable=AsyncMock) as mock_run,
        patch("forge.observability.config.get_settings", return_value=_mock_settings()),
    ):
        result = main()

    assert result == 0
    mock_run.assert_called_once_with(once=False, skip_dbt=True)


def test_worker_skip_dbt_from_settings(monkeypatch):
    monkeypatch.setattr("sys.argv", ["forge-observability", "worker"])
    with (
        patch("asyncio.run"),
        patch("forge.observability.worker.run_pipelines", new_callable=AsyncMock) as mock_run,
        patch(
            "forge.observability.config.get_settings", return_value=_mock_settings(skip_dbt=True)
        ),
    ):
        result = main()

    assert result == 0
    mock_run.assert_called_once_with(once=False, skip_dbt=True)


def test_worker_once_and_skip_dbt(monkeypatch):
    monkeypatch.setattr("sys.argv", ["forge-observability", "worker", "--once", "--skip-dbt"])
    with (
        patch("asyncio.run"),
        patch("forge.observability.worker.run_pipelines", new_callable=AsyncMock) as mock_run,
        patch("forge.observability.config.get_settings", return_value=_mock_settings()),
    ):
        result = main()

    assert result == 0
    mock_run.assert_called_once_with(once=True, skip_dbt=True)
