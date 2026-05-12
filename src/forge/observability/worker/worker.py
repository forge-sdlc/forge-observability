"""Analytics pipeline runner — executes dlt pipelines on a schedule."""

import asyncio
import logging
import signal
from collections.abc import Callable
from functools import singledispatch
from pathlib import Path

import dlt
import yaml

from forge.observability.config import ClickHouseConfig, get_settings

logger = logging.getLogger(__name__)

_DBT_PROJECT = Path(__file__).parent / "pipelines" / "dbt"

# Repo root — four levels above src/forge/observability/worker/
_REPO_ROOT = Path(__file__).parents[4]
_DLT_PIPELINES_DIR = _REPO_ROOT / "state" / ".dlt" / "pipelines"


def _load_bronze_tables() -> dict[str, str]:
    """Build source-name → table-identifier mapping from the dbt sources.yml."""
    spec = yaml.safe_load((_DBT_PROJECT / "models" / "sources.yml").read_text())
    return {
        table["name"]: table["identifier"]
        for source in spec.get("sources", [])
        if source["name"] == "bronze"
        for table in source.get("tables", [])
    }


# Maps dbt source name → bronze table identifier, derived from sources.yml.
_BRONZE_TABLES: dict[str, str] = _load_bronze_tables()

# dlt uses CREATE TABLE (not IF NOT EXISTS) for the bronze dataset sentinel table.
# On a fresh datastore, all pipelines race to create it simultaneously. This lock
# serializes the first pipeline run so only one initializes the dataset.
_dataset_initialized = False
_dataset_init_lock: asyncio.Lock | None = None


# ── Backend dispatch ─────────────────────────────────────────────────────────
# To add a new backend: register _resolve_destination and _table_exists for its config type.


@singledispatch
def _resolve_destination(cfg):
    raise ValueError(f"Unsupported backend config: {type(cfg).__name__}")


@_resolve_destination.register(ClickHouseConfig)
def _resolve_destination_clickhouse(cfg: ClickHouseConfig):
    # Build credentials manually — dlt's auto-detection doesn't pick up Pydantic settings.
    from dlt.destinations.impl.clickhouse.configuration import ClickHouseCredentials

    creds = ClickHouseCredentials()
    creds.host = cfg.host
    creds.port = cfg.port
    creds.http_port = cfg.http_port
    creds.database = cfg.database
    creds.username = cfg.user
    creds.password = cfg.password.get_secret_value()
    creds.secure = 0
    return dlt.destinations.clickhouse(credentials=creds)


@singledispatch
def _table_exists(cfg, table_name: str) -> bool:
    raise ValueError(f"Unsupported backend config: {type(cfg).__name__} (table: {table_name})")


@_table_exists.register(ClickHouseConfig)
def _table_exists_clickhouse(cfg: ClickHouseConfig, table_name: str) -> bool:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.http_port,
        username=cfg.user,
        password=cfg.password.get_secret_value(),
        database=cfg.database,
    )
    try:
        client.query(f"SELECT 1 FROM `{table_name}` LIMIT 1")
        return True
    except Exception:
        return False


def _build_pipeline(name: str, dataset_name: str = "bronze") -> dlt.Pipeline:
    """Create a dlt pipeline targeting the configured datastore."""
    cfg = get_settings().backend_config
    return dlt.pipeline(
        pipeline_name=name,
        destination=_resolve_destination(cfg),
        dataset_name=dataset_name,
        pipelines_dir=_DLT_PIPELINES_DIR,
    )


# ── Source factories ────────────────────────────────────────────────────────


# TODO: Configure for different LLM observability tools
def _make_langfuse_source():
    """Build a Langfuse dlt source from the active settings."""
    # Deferred import: avoids loading langfuse deps when the source isn't configured.
    from forge.observability.worker.pipelines.langfuse_pipeline import langfuse_source

    s = get_settings()
    return langfuse_source(
        host=s.langfuse_url,
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key.get_secret_value(),
        lag_seconds=s.langfuse_collection_lag_seconds,
        page_size=s.langfuse_page_size,
    )


# ── Execution ───────────────────────────────────────────────────────────────


def _run_dlt(pipeline, source):
    """Synchronous thread target: call pipeline.run(source) with SIGINT blocked.

    Mirrors _run_dbt — both are the sync, SIGINT-shielded functions handed to
    asyncio.to_thread so that Ctrl+C can't interrupt an in-flight dlt load.
    Signal masks are inherited across fork/exec, so any subprocess spawned by
    the pipeline also has SIGINT blocked.
    """
    import signal as _signal

    _signal.pthread_sigmask(_signal.SIG_BLOCK, [_signal.SIGINT])
    try:
        return pipeline.run(source)
    finally:
        _signal.pthread_sigmask(_signal.SIG_UNBLOCK, [_signal.SIGINT])


async def _run_pipeline(name: str, source_factory: Callable) -> None:
    """Async coordinator: build the pipeline, manage the dataset-init mutex, then
    dispatch the actual dlt load to a worker thread via _run_dlt.

    The dataset-init lock exists because dlt uses CREATE TABLE (not IF NOT EXISTS)
    for its sentinel table — without serialization, concurrent pipelines on a fresh
    datastore race to create it and one will fail.
    """
    global _dataset_initialized, _dataset_init_lock
    pipeline = _build_pipeline(name)
    source = source_factory()
    logger.info(f"Running pipeline: {name}")

    if not _dataset_initialized:
        # Lazy-init: Lock must be created inside a running event loop.
        if _dataset_init_lock is None:
            _dataset_init_lock = asyncio.Lock()
        async with _dataset_init_lock:
            if not _dataset_initialized:
                # This pipeline is first — it initializes the shared dlt dataset.
                load_info = await asyncio.to_thread(_run_dlt, pipeline, source)
                _dataset_initialized = True
                logger.info(f"Pipeline {name} complete: {load_info}")
                return
        # Another pipeline already initialized the dataset; fall through to a
        # normal run which will find the sentinel table already present.

    load_info = await asyncio.to_thread(_run_dlt, pipeline, source)
    logger.info(f"Pipeline {name} complete: {load_info}")


async def _sleep_or_shutdown(seconds: int, shutdown_event: asyncio.Event | None) -> bool:
    """Sleep for `seconds`. Returns True if the interval elapsed, False if shutdown was requested."""
    if shutdown_event is None:
        await asyncio.sleep(seconds)
        return True

    try:
        # Race the event against the timeout: returns normally if shutdown fires first,
        # raises TimeoutError if the full interval elapses without a shutdown signal.
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return False
    except TimeoutError:
        return True


async def _run_pipeline_loop(
    name: str,
    source_factory: Callable,
    interval: int,
    skip_dbt: bool = False,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run a pipeline on a repeating interval, with graceful shutdown support.

    Each iteration has three phases:
      1. dlt pipeline run — extract + load bronze tables.
      2. dbt run (optional) — rebuild silver/gold views from bronze.
      3. Inter-run sleep — wait `interval` seconds before the next iteration.

    After each phase completes, the shutdown event is checked. If set, the loop
    returns without starting the next phase, allowing any in-flight work to finish
    before the process exits.
    """
    while True:
        # Phase 1: run the dlt pipeline. On failure, sleep then retry.
        try:
            await _run_pipeline(name, source_factory)
        except Exception:
            logger.exception(f"Pipeline {name} failed — retrying after {interval}s")
            if not await _sleep_or_shutdown(interval, shutdown_event):
                return
            continue

        # Exit before dbt if shutdown was requested while the pipeline was running.
        if shutdown_event and shutdown_event.is_set():
            return

        # Phase 2: rebuild dbt views. Errors are logged but don't stop the loop.
        if not skip_dbt:
            try:
                await asyncio.to_thread(_run_dbt)
            except Exception:
                logger.exception("dbt run failed")

        # Exit before sleeping if shutdown was requested while dbt was running.
        if shutdown_event and shutdown_event.is_set():
            return

        # Phase 3: sleep until the next run, waking early if shutdown is requested.
        if not await _sleep_or_shutdown(interval, shutdown_event):
            return


def _get_available_sources() -> list[str]:
    """Return source names whose bronze tables currently exist in the datastore."""
    cfg = get_settings().backend_config
    return [name for name, table in _BRONZE_TABLES.items() if _table_exists(cfg, table)]


def _run_dbt() -> None:
    """Rebuild silver/gold views via dbt after a successful pipeline run."""
    import signal as _signal

    # Block SIGINT in this thread before dlt spawns the dbt subprocess.
    # Signal masks are inherited across fork/exec, so the dbt subprocess also
    # has SIGINT blocked — Ctrl+C from the terminal won't kill it mid-run.
    _signal.pthread_sigmask(_signal.SIG_BLOCK, [_signal.SIGINT])
    try:
        s = get_settings()
        available = _get_available_sources()
        logger.info(f"Running dbt — available sources: {available}")
        dbt_pipeline = _build_pipeline("dbt", dataset_name=s.backend_config.database)
        dbt = dlt.dbt.package(dbt_pipeline, str(_DBT_PROJECT))
        models = dbt.run_all(additional_vars={"available_sources": available})
        for m in models:
            logger.info(f"dbt {m.model_name}: {m.status} ({m.time})")
    finally:
        _signal.pthread_sigmask(_signal.SIG_UNBLOCK, [_signal.SIGINT])


async def run_pipelines(once: bool = False, skip_dbt: bool = False) -> None:
    """Run all configured analytics pipelines concurrently, then rebuild views with dbt.

    Args:
        once: Run each pipeline once and exit (useful for backfill / testing).
        skip_dbt: Skip dbt silver/gold rebuilds (useful for iterating on dbt models locally).
    """
    s = get_settings()
    pipelines: list[tuple[str, Callable, int]] = []

    # Register each enabled source as a (name, factory, interval) tuple.
    if s.langfuse_enabled:
        pipelines.append(("langfuse", _make_langfuse_source, s.langfuse_interval_seconds))
    else:
        logger.warning("Langfuse not configured — skipping langfuse pipeline")

    if not pipelines:
        logger.error("No pipelines configured — nothing to run")
        return

    # Graceful shutdown for both once and continuous modes: replace asyncio's
    # default SIGINT/SIGTERM handler (which cancels tasks immediately) with a
    # cooperative flag that lets in-flight work finish before exiting.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        # Idempotent: only log and set the event on the first signal.
        if not shutdown_event.is_set():
            logger.info("Shutdown requested — waiting for active pipelines to finish")
            shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, _request_shutdown)
    loop.add_signal_handler(signal.SIGTERM, _request_shutdown)

    try:
        if once:
            # One-shot mode: run all pipelines concurrently, rebuild views, then exit.
            await asyncio.gather(*[_run_pipeline(name, factory) for name, factory, _ in pipelines])
            # Don't start dbt if shutdown was requested while pipelines were running.
            if not shutdown_event.is_set():
                if not skip_dbt:
                    await asyncio.to_thread(_run_dbt)
                else:
                    logger.info("Skipping dbt run (--skip-dbt)")
        else:
            if skip_dbt:
                logger.info("Skipping dbt runs (--skip-dbt)")
            tasks = [
                _run_pipeline_loop(
                    name, factory, interval, skip_dbt=skip_dbt, shutdown_event=shutdown_event
                )
                for name, factory, interval in pipelines
            ]
            # gather runs all pipeline loops concurrently; returns when every loop exits.
            await asyncio.gather(*tasks)
    finally:
        # Restore default signal handling regardless of how the gather exits.
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)

    logger.info("All pipelines stopped — worker exiting")
