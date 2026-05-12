"""Tests for forge.observability.worker.worker."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import forge.observability.worker.worker as worker_mod
from forge.observability.worker.worker import (
    _get_available_sources,
    _resolve_destination,
    _run_dbt,
    _run_dlt,
    _run_pipeline_loop,
    _sleep_or_shutdown,
    _table_exists,
    run_pipelines,
)


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
    from forge.observability.config import ClickHouseConfig

    s = MagicMock()
    s.langfuse_enabled = True
    s.langfuse_interval_seconds = 60
    s.langfuse_url = "http://localhost:3000"
    s.langfuse_public_key = "pk-test"
    s.langfuse_secret_key.get_secret_value.return_value = "sk-test"
    s.backend_config = ClickHouseConfig()
    return s


# ── _resolve_destination ─────────────────────────────────────────────────────


def test_resolve_destination_clickhouse_returns_destination():
    from forge.observability.config import ClickHouseConfig

    cfg = ClickHouseConfig()
    with (
        patch("dlt.destinations.impl.clickhouse.configuration.ClickHouseCredentials"),
        patch("dlt.destinations.clickhouse") as mock_dest,
    ):
        _resolve_destination(cfg)
    mock_dest.assert_called_once()


def test_resolve_destination_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported backend config"):
        _resolve_destination(object())


# ── _table_exists ─────────────────────────────────────────────────────────────


def test_table_exists_clickhouse_true():
    from forge.observability.config import ClickHouseConfig

    cfg = ClickHouseConfig()
    mock_client = MagicMock()
    with patch("clickhouse_connect.get_client", return_value=mock_client):
        assert _table_exists(cfg, "bronze___llm_traces") is True
    mock_client.query.assert_called_once()


def test_table_exists_clickhouse_false():
    from forge.observability.config import ClickHouseConfig

    cfg = ClickHouseConfig()
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("table not found")
    with patch("clickhouse_connect.get_client", return_value=mock_client):
        assert _table_exists(cfg, "missing_table") is False


def test_table_exists_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported backend config"):
        _table_exists(object(), "some_table")


# ── _get_available_sources ────────────────────────────────────────────────────


def test_get_available_sources_includes_existing_tables(mock_settings):
    first_source = next(iter(worker_mod._BRONZE_TABLES))
    # Return True only for the first table, False for the rest.
    returns = [True] + [False] * (len(worker_mod._BRONZE_TABLES) - 1)

    with (
        patch("forge.observability.worker.worker.get_settings", return_value=mock_settings),
        patch("forge.observability.worker.worker._table_exists", side_effect=returns),
    ):
        result = _get_available_sources()

    assert result == [first_source]


def test_get_available_sources_empty_when_no_tables(mock_settings):
    with (
        patch("forge.observability.worker.worker.get_settings", return_value=mock_settings),
        patch("forge.observability.worker.worker._table_exists", return_value=False),
    ):
        result = _get_available_sources()

    assert result == []


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


async def test_run_pipelines_once_graceful_shutdown_on_signal(mock_settings, caplog):
    """SIGINT during --once mode logs the shutdown message and lets work finish."""
    import signal as _signal

    # Capture the _request_shutdown callback when run_pipelines registers it.
    registered: dict = {}
    loop = asyncio.get_running_loop()

    async def pipeline_calls_shutdown(_name, _factory):
        registered[_signal.SIGINT]()  # simulate the signal arriving mid-pipeline

    with (
        caplog.at_level(logging.INFO, logger="forge.observability.worker.worker"),
        patch("forge.observability.worker.worker.get_settings", return_value=mock_settings),
        patch(
            "forge.observability.worker.worker._run_pipeline",
            side_effect=pipeline_calls_shutdown,
        ),
        patch("forge.observability.worker.worker._run_dbt"),
        patch.object(
            loop, "add_signal_handler", side_effect=lambda sig, h: registered.update({sig: h})
        ),
    ):
        await run_pipelines(once=True)

    assert "Shutdown requested" in caplog.text
    assert "All pipelines stopped" in caplog.text


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


# ── _run_pipeline_loop graceful shutdown ─────────────────────────────────────


async def test_pipeline_loop_exits_after_pipeline_when_shutdown_set():
    """Loop exits without running dbt when shutdown_event is set during the pipeline phase."""
    shutdown_event = asyncio.Event()

    async def pipeline_and_signal(_name, _factory):
        shutdown_event.set()

    with (
        patch("forge.observability.worker.worker._run_pipeline", side_effect=pipeline_and_signal),
        patch("forge.observability.worker.worker._run_dbt") as mock_dbt,
    ):
        await _run_pipeline_loop(
            "langfuse", MagicMock(), 60, skip_dbt=False, shutdown_event=shutdown_event
        )

    mock_dbt.assert_not_called()


async def test_pipeline_loop_exits_after_dbt_when_shutdown_set():
    """Loop exits without sleeping when shutdown_event is set during the dbt phase."""
    shutdown_event = asyncio.Event()

    def dbt_and_signal():
        shutdown_event.set()

    sleep_mock = AsyncMock(return_value=False)
    with (
        patch("forge.observability.worker.worker._run_pipeline", new_callable=AsyncMock),
        patch("forge.observability.worker.worker._run_dbt", side_effect=dbt_and_signal),
        patch("forge.observability.worker.worker._sleep_or_shutdown", sleep_mock),
    ):
        await _run_pipeline_loop(
            "langfuse", MagicMock(), 60, skip_dbt=False, shutdown_event=shutdown_event
        )

    sleep_mock.assert_not_called()


async def test_pipeline_loop_exits_during_sleep_when_shutdown_set():
    """Loop does not start a second iteration when _sleep_or_shutdown signals shutdown."""
    pipeline_calls = 0

    async def mock_run_pipeline(_name, _factory):
        nonlocal pipeline_calls
        pipeline_calls += 1

    with (
        patch("forge.observability.worker.worker._run_pipeline", side_effect=mock_run_pipeline),
        patch("forge.observability.worker.worker._run_dbt"),
        patch(
            "forge.observability.worker.worker._sleep_or_shutdown",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await _run_pipeline_loop(
            "langfuse", MagicMock(), 60, skip_dbt=False, shutdown_event=asyncio.Event()
        )

    assert pipeline_calls == 1


# ── _sleep_or_shutdown ────────────────────────────────────────────────────────


async def test_sleep_or_shutdown_returns_false_when_event_fires():
    """Returns False (shutdown) when the event is set before the timeout elapses."""
    event = asyncio.Event()
    asyncio.get_event_loop().call_soon(event.set)
    assert await _sleep_or_shutdown(60, event) is False


async def test_sleep_or_shutdown_returns_true_when_timeout_elapses():
    """Returns True (keep running) when the timeout elapses before the event fires."""
    assert await _sleep_or_shutdown(0, asyncio.Event()) is True


async def test_sleep_or_shutdown_returns_true_with_no_event():
    """Returns True when no shutdown_event is provided (plain sleep path)."""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        assert await _sleep_or_shutdown(60, None) is True


# ── _run_dlt (SIGINT shielding) ───────────────────────────────────────────────


def test_run_dlt_blocks_sigint_before_load():
    """pthread_sigmask blocks SIGINT before pipeline.run executes, then unblocks it after."""
    import signal

    mask_calls: list[tuple] = []

    def capture(how, sigs):
        mask_calls.append((how, list(sigs)))

    with patch("signal.pthread_sigmask", side_effect=capture):
        _run_dlt(MagicMock(), MagicMock())

    assert mask_calls[0] == (signal.SIG_BLOCK, [signal.SIGINT])
    assert mask_calls[1] == (signal.SIG_UNBLOCK, [signal.SIGINT])


def test_run_dlt_unblocks_sigint_even_on_exception():
    """pthread_sigmask unblocks SIGINT in the finally block even when pipeline.run raises."""
    import signal

    unblock_calls: list = []

    def capture(how, sigs):
        if how == signal.SIG_UNBLOCK:
            unblock_calls.append(list(sigs))

    with (
        patch("signal.pthread_sigmask", side_effect=capture),
        pytest.raises(RuntimeError),
    ):
        _run_dlt(
            MagicMock(run=MagicMock(side_effect=RuntimeError("pipeline crashed"))),
            MagicMock(),
        )

    assert unblock_calls == [[signal.SIGINT]]


# ── _run_dbt (SIGINT shielding) ───────────────────────────────────────────────


def test_run_dbt_blocks_sigint_before_subprocess(mock_settings):
    """pthread_sigmask blocks SIGINT before dbt runs, then unblocks it after."""
    import signal

    mask_calls: list[tuple] = []

    def capture(how, sigs):
        mask_calls.append((how, list(sigs)))

    mock_dbt_pkg = MagicMock()
    mock_dbt_pkg.run_all.return_value = []
    with (
        patch("signal.pthread_sigmask", side_effect=capture),
        patch("forge.observability.worker.worker.get_settings", return_value=mock_settings),
        patch("forge.observability.worker.worker._get_available_sources", return_value=[]),
        patch("forge.observability.worker.worker._build_pipeline"),
        patch("dlt.dbt.package", return_value=mock_dbt_pkg),
    ):
        _run_dbt()

    assert mask_calls[0] == (signal.SIG_BLOCK, [signal.SIGINT])
    assert mask_calls[1] == (signal.SIG_UNBLOCK, [signal.SIGINT])


def test_run_dbt_unblocks_sigint_even_on_exception(mock_settings):
    """pthread_sigmask unblocks SIGINT in the finally block even when dbt raises."""
    import signal

    unblock_calls: list = []

    def capture(how, sigs):
        if how == signal.SIG_UNBLOCK:
            unblock_calls.append(list(sigs))

    with (
        patch("signal.pthread_sigmask", side_effect=capture),
        patch("forge.observability.worker.worker.get_settings", return_value=mock_settings),
        patch("forge.observability.worker.worker._get_available_sources", return_value=[]),
        patch("forge.observability.worker.worker._build_pipeline"),
        patch("dlt.dbt.package", side_effect=RuntimeError("dbt crashed")),
        pytest.raises(RuntimeError),
    ):
        _run_dbt()

    assert unblock_calls == [[signal.SIGINT]]
