# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync
# Or to include dev only dependencies
uv sync --extra dev

# Run tests
uv run pytest
uv run pytest tests/test_api.py          # single file
uv run pytest -k "test_health"           # single test by name

# Lint / format
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Run the API server (port 8010) 
uv run forge observability serve
uv run forge observability serve --host "0.0.0.0" --port 8010

# Run all dlt/dbt pipelines (continuous or one-shot)
uv run forge observability worker
uv run forge observability worker --once

# Start dev datastore (e.g., ClickHouse)
podman compose -f devtools/compose.clickhouse.yml up -d

# Start Forge Observability Stack (API + worker)
podman compose -f devtools/compose.dev.yml up -d

```

## Architecture

**forge-observability** is a data ingestion and analytics plugin for the Forge SDLC Orchestrator. It pulls data from four external sources (Langfuse, GitHub, JIRA, Prometheus) into a configurable datastore. Using a medallion architecture, metrics are exposed via the Forge Observability API.

### Data layers

| Layer | Location | What it is |
|---|---|---|
| Bronze | Datastore tables | Raw, denormalized tables per source loaded by dlt |
| Silver | Datastore views | Cross-source joins, built by dbt |
| Gold | Datastore views | Pre-aggregated KPIs, built by dbt |

**Table naming convention**: dlt loads bronze tables into the `default` database (no schema separation) using triple-underscore as a schema prefix separator — e.g. `bronze___llm_traces`, `silver___ticket_full_view`. SQLAlchemy queries reference these flat names directly.

### Pipeline worker (`pipelines/worker.py`)

Four dlt source pipelines run concurrently via `asyncio.gather` on configurable intervals. After each round — once every configured pipeline has reported at least one completion (success or failure) — a dbt task rebuilds silver and gold views via `dlt.dbt.package()`. Worker entry point: `forge observability worker`.

**dlt pipelines** (each `*_pipeline.py` is an isolated dlt source):
- `langfuse_pipeline` → `bronze___llm_traces`
- `github_pipeline` → `bronze___pull_requests`, `bronze___ci_checks`
- `jira_pipeline` → `bronze___jira_tickets`, `bronze___human_interactions`
- `prometheus_pipeline` → `bronze___app_metrics`

Pipelines are only registered if their credentials are present (`*_enabled` properties on `Settings`).

**dbt run** (in `pipelines/dbt/`): After bronze pipelines finish, `_run_dbt()` in `worker.py` calls `dlt.dbt.package()` to execute the dbt project. Before each run it calls `_write_sources_yml()` which regenerates `models/sources.yml` from `_SOURCE_TABLE_MAP` (avoiding dlt housekeeping tables). Each dbt model uses the `is_source_available()` macro to conditionally include joins only for sources whose bronze tables actually exist — silver and gold views degrade gracefully when a source hasn't been loaded yet.

### API service (`api/app.py`)

Standalone FastAPI app — routes query datastore directly via `Repository`. Endpoint groups:

- **Bronze drill-downs**: `/traces`, `/traces/summary`, `/ci-checks`
- **Silver aggregations**: `/tickets/{ticket_key}/summary`, `/insights/workflows`, `/insights/stage-performance`, `/insights/prs`, `/insights/cost-by-model`
- **Health**: `/health`

`Repository` (`repository/repository.py`) uses SQLAlchemy Core with `create_engine(settings.datastore_dsn)`. It is injected as a FastAPI dependency and mocked in tests. Queries are built with SQLAlchemy `select()` / `sa_table()` referencing the flat `bronze___*` / `silver___*` table names.

### Configuration (`config.py`)

All config comes from environment variables (or `.env`) via Pydantic settings — `get_settings()` returns a cached singleton. Key groups:

- ClickHouse: `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_HTTP_PORT`, `CLICKHOUSE_DATABASE`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`
- Source credentials: `LANGFUSE_*`, `GITHUB_TOKEN`, `GITHUB_KNOWN_REPOS`, `JIRA_*`, `PROMETHEUS_HOST/PORT`
- Pipeline intervals: `LANGFUSE_INTERVAL_SECONDS`, `PROMETHEUS_INTERVAL_SECONDS`, `GITHUB_INTERVAL_SECONDS`, `JIRA_INTERVAL_SECONDS`
- API: `FORGE_OBSERVABILITY_API_PORT`, `FORGE_OBSERVABILITY_API_LOG_LEVEL`

`datastore_dsn` (computed) builds the SQLAlchemy connection string; `langfuse_url` / `prometheus_url` compose host+port into full URLs.

### Entry points

`forge` → `forge.observability.cli:forge_main`

Registers `observability` as a subcommand of the Forge CLI namespace. When installed alongside other Forge plugins, each plugin contributes its own subcommand under `forge`.

### Containers

`containers/` holds two Dockerfiles:
- `forge-observability-api` — FastAPI service
- `forge-observability-worker` — background dlt + dbt worker

These are composed with ClickHouse in `devtools/`.

## Tests

Tests use `pytest-asyncio`, FastAPI `TestClient`, and mock `Repository` to avoid needing a real datastore.

- `tests/test_api.py` — API endpoint responses (mocked repository)
- `tests/test_config.py` — settings loading and computed properties
- `tests/test_pipelines.py` — pipeline helper logic (ticket extraction, interaction classification)

Ruff is configured with rules E, F, I, UP, B, SIM, ARG and a 100-character line limit.
