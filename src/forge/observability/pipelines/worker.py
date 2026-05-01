"""Analytics pipeline runner — executes dlt pipelines on a schedule."""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import dlt
from sqlalchemy import text

from forge.observability.config import get_settings

logger = logging.getLogger(__name__)

_DBT_PROJECT = Path(__file__).parent / "dbt"

# Repo root — two levels above src/forge/observability/pipelines/
_REPO_ROOT = Path(__file__).parents[4]
_DBT_TARGET = _REPO_ROOT / ".dbt" / "target"
_DBT_LOGS = _REPO_ROOT / ".dbt" / "logs"

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
    )


def _read_source_tables() -> dict[str, str]:
    """Return logical→physical table mapping from sources.yml."""
    import yaml

    sources_yml = _DBT_PROJECT / "models" / "sources.yml"
    doc = yaml.safe_load(sources_yml.read_text())
    result = {}
    for source in doc.get("sources", []):
        for table in source.get("tables", []):
            result[table["name"]] = table.get("identifier", table["name"])
    return result


def _get_available_sources() -> list[str]:
    """Return source names whose bronze tables actually exist in datastore."""
    from forge.observability.repository.repository import _get_engine

    engine = _get_engine()
    available = []
    for source, table in _read_source_tables().items():
        try:
            with engine.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
            available.append(source)
        except Exception:
            pass
    return available


def _load_dbt(available_sources: list[str]) -> None:
    """Rebuild silver views via dbt with current source availability."""
    import os

    _dbt_packages = _REPO_ROOT / ".dbt" / "packages"
    _DBT_TARGET.mkdir(parents=True, exist_ok=True)
    _DBT_LOGS.mkdir(parents=True, exist_ok=True)
    _dbt_packages.mkdir(parents=True, exist_ok=True)
    os.environ["DBT_TARGET_PATH"] = str(_DBT_TARGET)
    os.environ["DBT_LOG_PATH"] = str(_DBT_LOGS)
    os.environ["DBT_PACKAGES_INSTALL_PATH"] = str(_dbt_packages)

    # TODO: Configure different backends
    s = get_settings()
    dbt_pipeline = _build_pipeline("forge_observability_dbt", dataset_name=s.clickhouse_database)
    dbt = dlt.dbt.package(dbt_pipeline, str(_DBT_PROJECT))
    models = dbt.run_all(
        additional_vars={
            "available_sources": available_sources,
        }
    )
    for m in models:
        logger.info(f"dbt {m.model_name}: {m.status} ({m.time})")


# ── Source factories ────────────────────────────────────────────────────────


# TODO: Configure for different LLM observability tools
def _make_langfuse_source():
    from forge.observability.pipelines.langfuse_pipeline import langfuse_source

    s = get_settings()
    return langfuse_source(
        host=s.langfuse_url,
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key.get_secret_value(),
    )


# TODO: Add prometheus authentication
def _make_prometheus_source():
    from forge.observability.pipelines.prometheus_pipeline import prometheus_source

    s = get_settings()
    return prometheus_source(prometheus_url=s.prometheus_url)


def _make_github_source():
    from forge.observability.pipelines.github_pipeline import github_source

    s = get_settings()
    return github_source(
        token=s.github_token.get_secret_value(),
        repos=s.known_repos,
    )


def _make_jira_source():
    from forge.observability.pipelines.jira_pipeline import jira_source

    s = get_settings()
    return jira_source(
        base_url=s.jira_base_url,
        user_email=s.jira_user_email,
        api_token=s.jira_api_token.get_secret_value(),
    )


# ── Execution ───────────────────────────────────────────────────────────────


async def _load_pipeline(name: str, source_factory: Callable) -> None:
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
    name: str, source_factory: Callable, interval: int, done_queue: asyncio.Queue
) -> None:
    while True:
        try:
            await _load_pipeline(name, source_factory)
        except Exception:
            logger.exception(f"Pipeline {name} failed — retrying after {interval}s")
        finally:
            await done_queue.put(name)
        await asyncio.sleep(interval)


async def _run_dbt(pipeline_names: frozenset[str], done_queue: asyncio.Queue) -> None:
    """Run dbt once per complete round of pipeline executions.

    Waits until every configured pipeline has reported at least one completion
    (success or failure) since the last dbt run, then rebuilds silver views.
    Fast pipelines that complete multiple times per round are deduplicated via
    the set — dbt fires at the pace of the slowest configured pipeline.
    """
    while True:
        seen: set[str] = set()
        while seen < pipeline_names:
            seen.add(await done_queue.get())
        try:
            available = await asyncio.to_thread(_get_available_sources)
            logger.info(f"dbt run — available sources: {available}")
            await asyncio.to_thread(_load_dbt, available)
        except Exception:
            logger.exception("dbt run failed")


async def _run_pipelines(once: bool = False, skip_dbt: bool = False) -> None:
    """Run all configured analytics pipelines concurrently, then rebuild silver views with dbt.

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

    if s.prometheus_enabled:
        pipelines.append(("prometheus", _make_prometheus_source, s.prometheus_interval_seconds))
    else:
        logger.warning("Prometheus not configured — skipping prometheus pipeline")

    if s.github_enabled:
        pipelines.append(("github", _make_github_source, s.github_interval_seconds))
    else:
        logger.warning("GitHub not configured — skipping github pipeline")

    if s.jira_enabled:
        pipelines.append(("jira", _make_jira_source, s.jira_interval_seconds))
    else:
        logger.warning("JIRA not configured — skipping jira pipeline")

    if not pipelines:
        logger.error("No pipelines configured — nothing to run")
        return

    if once:
        await asyncio.gather(*[_load_pipeline(name, factory) for name, factory, _ in pipelines])
        if not skip_dbt:
            available = await asyncio.to_thread(_get_available_sources)
            logger.info(f"dbt run — available sources: {available}")
            await asyncio.to_thread(_load_dbt, available)
        else:
            logger.info("Skipping dbt run (--skip-dbt)")
    else:
        done_queue: asyncio.Queue[str] = asyncio.Queue()
        pipeline_names = frozenset(name for name, _, _ in pipelines)
        tasks = [
            _run_pipeline_loop(name, factory, interval, done_queue)
            for name, factory, interval in pipelines
        ]
        if not skip_dbt:
            tasks.append(_run_dbt(pipeline_names, done_queue))
        else:
            logger.info("Skipping dbt runs (--skip-dbt)")
        await asyncio.gather(*tasks)
