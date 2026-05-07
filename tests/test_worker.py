"""Tests for forge.observability.worker.worker."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import forge.observability.worker.worker as worker_mod
from forge.observability.worker.worker import _run_pipeline_loop, run_pipelines


@pytest.fixture(autouse=True)
def reset_dataset_state():
    """Reset module-level dataset init state between tests."""
    worker_mod._dataset_initialized = False
    worker_mod._dataset_init_lock = None
    yield
    worker_mod._dataset_initialized = False
    worker_mod._dataset_init_lock = None


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.langfuse_enabled = True
    s.langfuse_interval_seconds = 60
    s.langfuse_url = "http://localhost:3000"
    s.langfuse_public_key = "pk-test"
    s.langfuse_secret_key.get_secret_value.return_value = "sk-test"
    s.clickhouse_host = "localhost"
    s.clickhouse_port = 9000
    s.clickhouse_http_port = 8123
    s.clickhouse_database = "default"
    s.clickhouse_user = "forge"
    s.clickhouse_password.get_secret_value.return_value = "forge"
    return s


# ── run_pipelines ────────────────────────────────────────────────────────────


async def test_run_pipelines_no_pipelines_logs_error(mock_settings, caplog):
    mock_settings.langfuse_enabled = False
    with patch("forge.observability.worker.worker.get_settings", return_value=mock_settings):
        await run_pipelines()
    assert "No pipelines configured" in caplog.text


async def test_run_pipelines_langfuse_disabled_logs_warning(mock_settings, caplog):
    mock_settings.langfuse_enabled = False
    with patch("forge.observability.worker.worker.get_settings", return_value=mock_settings):
        await run_pipelines()
    assert "Langfuse not configured" in caplog.text


async def test_run_pipelines_once_calls_run_pipeline_and_dbt(mock_settings):
    with (
        patch("forge.observability.worker.worker.get_settings", return_value=mock_settings),
        patch(
            "forge.observability.worker.worker._run_pipeline", new_callable=AsyncMock
        ) as mock_run,
        patch("forge.observability.worker.worker._run_dbt") as mock_dbt,
    ):
        await run_pipelines(once=True)

    mock_run.assert_called_once_with("langfuse", worker_mod._make_langfuse_source)
    mock_dbt.assert_called_once()


async def test_run_pipelines_once_skip_dbt_skips_dbt(mock_settings):
    with (
        patch("forge.observability.worker.worker.get_settings", return_value=mock_settings),
        patch("forge.observability.worker.worker._run_pipeline", new_callable=AsyncMock),
        patch("forge.observability.worker.worker._run_dbt") as mock_dbt,
    ):
        await run_pipelines(once=True, skip_dbt=True)

    mock_dbt.assert_not_called()


# ── _run_pipeline_loop ───────────────────────────────────────────────────────


async def test_pipeline_loop_calls_dbt_after_success():
    """dbt runs after a successful pipeline execution."""
    call_count = 0

    async def mock_run_pipeline(_name, _factory):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise asyncio.CancelledError

    with (
        patch("forge.observability.worker.worker._run_pipeline", side_effect=mock_run_pipeline),
        patch("forge.observability.worker.worker._run_dbt") as mock_dbt,
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_pipeline_loop("langfuse", MagicMock(), 60, skip_dbt=False)

    mock_dbt.assert_called_once()


async def test_pipeline_loop_skips_dbt_after_failure():
    """dbt does not run when the pipeline raises an exception."""
    call_count = 0

    async def mock_run_pipeline(_name, _factory):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("connection refused")
        raise asyncio.CancelledError

    with (
        patch("forge.observability.worker.worker._run_pipeline", side_effect=mock_run_pipeline),
        patch("forge.observability.worker.worker._run_dbt") as mock_dbt,
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_pipeline_loop("langfuse", MagicMock(), 60, skip_dbt=False)

    mock_dbt.assert_not_called()


async def test_pipeline_loop_skips_dbt_when_flag_set():
    """dbt does not run when skip_dbt=True even after a successful pipeline."""
    call_count = 0

    async def mock_run_pipeline(_name, _factory):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise asyncio.CancelledError

    with (
        patch("forge.observability.worker.worker._run_pipeline", side_effect=mock_run_pipeline),
        patch("forge.observability.worker.worker._run_dbt") as mock_dbt,
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_pipeline_loop("langfuse", MagicMock(), 60, skip_dbt=True)

    mock_dbt.assert_not_called()


async def test_pipeline_loop_continues_after_dbt_failure():
    """A dbt failure does not stop the pipeline loop."""
    pipeline_calls = 0

    async def mock_run_pipeline(_name, _factory):
        nonlocal pipeline_calls
        pipeline_calls += 1
        if pipeline_calls > 2:
            raise asyncio.CancelledError

    with (
        patch("forge.observability.worker.worker._run_pipeline", side_effect=mock_run_pipeline),
        patch("forge.observability.worker.worker._run_dbt", side_effect=RuntimeError("dbt failed")),
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_pipeline_loop("langfuse", MagicMock(), 60, skip_dbt=False)

    assert pipeline_calls > 1
