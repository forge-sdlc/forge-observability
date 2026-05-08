"""Analytics pipeline runner — executes dlt pipelines on a schedule."""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import dlt
import yaml

from forge.observability.config import get_settings

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


# Maps dbt source name → ClickHouse bronze table name, derived from sources.yml.
_BRONZE_TABLES: dict[str, str] = _load_bronze_tables()

# dlt uses CREATE TABLE (not IF NOT EXISTS) for the bronze dataset sentinel table.
# On a fresh datastore, all pipelines race to create it simultaneously. This lock
# serializes the first pipeline run so only one initializes the dataset.
_dataset_initialized = False
_dataset_init_lock: asyncio.Lock | None = None


def _build_pipeline(name: str, dataset_name: str = "bronze") -> dlt.Pipeline:
    """Create a dlt pipeline targeting the configured datastore."""
    # TODO: Configure for different backends
    from dlt.destinations.impl.clickhouse.configuration import ClickHouseCredentials

    s = get_settings()

    creds = ClickHouseCredentials()
    creds.host = s.clickhouse_host
    creds.port = s.clickhouse_port
    creds.http_port = s.clickhouse_http_port
    creds.database = s.clickhouse_database
    creds.username = s.clickhouse_user
    creds.password = s.clickhouse_password.get_secret_value()
    creds.secure = 0

    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.clickhouse(credentials=creds),
        dataset_name=dataset_name,
        pipelines_dir=_DLT_PIPELINES_DIR,
    )


# ── Source factories ────────────────────────────────────────────────────────


# TODO: Configure for different LLM observability tools
def _make_langfuse_source():
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


async def _run_pipeline(name: str, source_factory: Callable) -> None:
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
                load_info = await asyncio.to_thread(pipeline.run, source)
                _dataset_initialized = True
                logger.info(f"Pipeline {name} complete: {load_info}")
                return
        # Another pipeline already initialized the dataset; fall through to a
        # normal run which will find the sentinel table already present.

    load_info = await asyncio.to_thread(pipeline.run, source)
    logger.info(f"Pipeline {name} complete: {load_info}")


async def _run_pipeline_loop(
    name: str, source_factory: Callable, interval: int, skip_dbt: bool = False
) -> None:
    while True:
        try:
            await _run_pipeline(name, source_factory)
        except Exception:
            logger.exception(f"Pipeline {name} failed — retrying after {interval}s")
            await asyncio.sleep(interval)
            continue

        if not skip_dbt:
            try:
                await asyncio.to_thread(_run_dbt)
            except Exception:
                logger.exception("dbt run failed")

        await asyncio.sleep(interval)


def _get_available_sources() -> list[str]:
    """Return source names whose bronze tables currently exist in the datastore."""
    import clickhouse_connect

    s = get_settings()
    client = clickhouse_connect.get_client(
        host=s.clickhouse_host,
        port=s.clickhouse_http_port,
        username=s.clickhouse_user,
        password=s.clickhouse_password.get_secret_value(),
        database=s.clickhouse_database,
    )
    available = []
    for source_name, table_name in _BRONZE_TABLES.items():
        try:
            client.query(f"SELECT 1 FROM `{table_name}` LIMIT 1")
            available.append(source_name)
        except Exception:
            pass
    return available


def _run_dbt() -> None:
    """Rebuild views via dbt after a successful pipeline run."""
    # TODO: Configure different backends
    s = get_settings()
    available = _get_available_sources()
    logger.info(f"Running dbt — available sources: {available}")
    dbt_pipeline = _build_pipeline("dbt", dataset_name=s.clickhouse_database)
    dbt = dlt.dbt.package(dbt_pipeline, str(_DBT_PROJECT))
    models = dbt.run_all(additional_vars={"available_sources": available})
    for m in models:
        logger.info(f"dbt {m.model_name}: {m.status} ({m.time})")


async def run_pipelines(once: bool = False, skip_dbt: bool = False) -> None:
    """Run all configured analytics pipelines concurrently, then rebuild views with dbt.

    Args:
        once: Run each pipeline once and exit (useful for backfill / testing).
        skip_dbt: Skip dbt silver/gold rebuilds (useful for iterating on dbt models locally).
    """
    s = get_settings()
    pipelines: list[tuple[str, Callable, int]] = []

    if s.langfuse_enabled:
        pipelines.append(("langfuse", _make_langfuse_source, s.langfuse_interval_seconds))
    else:
        logger.warning("Langfuse not configured — skipping langfuse pipeline")

    if not pipelines:
        logger.error("No pipelines configured — nothing to run")
        return

    if once:
        await asyncio.gather(*[_run_pipeline(name, factory) for name, factory, _ in pipelines])
        if not skip_dbt:
            await asyncio.to_thread(_run_dbt)
        else:
            logger.info("Skipping dbt run (--skip-dbt)")
    else:
        if skip_dbt:
            logger.info("Skipping dbt runs (--skip-dbt)")
        tasks = [
            _run_pipeline_loop(name, factory, interval, skip_dbt=skip_dbt)
            for name, factory, interval in pipelines
        ]
        await asyncio.gather(*tasks)
