# forge-observability

Analytics and observability worker for the [Forge SDLC Orchestrator](https://github.com/your-org/forge).

![Python](https://img.shields.io/badge/python-3.11%2B-blue)

## Overview

forge-observability ingests LLM observability data from Langfuse into a target datastore, then builds analytical views using dbt. It runs as a single background worker that handles data ingestion (via dlt) and view creation (via dbt) on a continuous schedule.

## Architecture

Data flows through a medallion structure:

```
Langfuse ──► Bronze (raw) ──► Silver (analytical tables) ──► (aggregations)
```

**Bronze** tables are loaded by **dlt** into the configured datastore using triple-underscore naming (`bronze___llm_traces`). **Staging** views are thin dbt SQL views that are used to prepare silver views. **Silver** views are analytical tableds built by dbt. dbt models enable themselves automatically once the corresponding bronze table exists in the datastore, using `adapter.get_relation()` to check at compile time.

dlt runs the Langfuse pipeline on a configurable interval. After each successful run, dbt rebuilds the views.

## Prerequisites

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/)
- Docker or Podman

## Quick Start

```bash
# Install
uv sync

# Start external datastore
# e.g., Clickhouse
podman compose --env-file .env -f devtools/compose.clickhouse.yml up -d

# Configure
cp .env.example .env
# Edit .env — set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY at minimum

# Run pipelines once to backfill (also runs dbt to build views)
forge-observability worker --once

# Start the worker on a loop, fetching new data after a specified interval
forge-observability worker
```

## Configuration

All settings are read from environment variables or a `.env` file. See `.env.example` for a ready-to-copy template.

### ClickHouse

| Variable | Default | Description |
|----------|---------|-------------|
| `CLICKHOUSE_HOST` | `localhost` | Server hostname |
| `CLICKHOUSE_PORT` | `9000` | Native protocol port |
| `CLICKHOUSE_HTTP_PORT` | `8123` | HTTP interface port |
| `CLICKHOUSE_DATABASE` | `default` | Database name |
| `CLICKHOUSE_USER` | `forge` | Username |
| `CLICKHOUSE_PASSWORD` | `forge` | Password |

### Worker

| Variable | Default | Description |
|----------|---------|-------------|
| `FORGE_OBSERVABILITY_WORKER_LOG_LEVEL` | `INFO` | Log level |
| `FORGE_OBSERVABILITY_WORKER_SKIP_DBT` | `false` | Skip dbt view rebuilds — useful when iterating on dbt models and invoking dbt directly via its CLI |

### Langfuse *(disabled if credentials absent)*

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_HOST` | `localhost` | Hostname |
| `LANGFUSE_PORT` | `3000` | Port |
| `LANGFUSE_PUBLIC_KEY` | `` | Public API key |
| `LANGFUSE_SECRET_KEY` | `` | Secret API key |
| `LANGFUSE_INTERVAL_SECONDS` | `60` | Pipeline polling interval |

> When running inside containers, use `host.containers.internal` instead of `localhost` to reach services on the host machine.

## Running with containers

```bash
# External datastore (run first)
podman compose --env-file .env -f devtools/compose.clickhouse.yml up -d

# Worker
podman compose --env-file .env -f devtools/compose.dev.yml up -d

# Tail logs
podman compose --env-file .env -f devtools/compose.dev.yml logs -f
```

dlt and dbt state are persisted in a named volume (`forge_observability_worker_state`) mounted at `/app/state` inside the container.

Tear down:

```bash
podman compose -f devtools/compose.clickhouse.yml down -v
podman compose -f devtools/compose.dev.yml down -v
```

## CLI reference

Usage: `forge-observability <command> [options]`

`-v / --verbose` enables DEBUG-level logging.

### `worker`

Run all configured dlt pipelines, then rebuild views with dbt after each successful run.

```
forge-observability worker [--once] [--skip-dbt]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--once` | off | Run the pipeline once then exit (useful for backfill) |
| `--skip-dbt` | off | Run source pipeline only — skip dbt silver rebuilds |

Without `--once`, the pipeline loops on the configured `LANGFUSE_INTERVAL_SECONDS`.

`--skip-dbt` is intended for local dbt development: keep the worker running so fresh bronze data continues to flow in, then invoke `dbt run` directly as you iterate on models.

## Data sources

| Source | Bronze tables | Level 
|--------|--------------|------------|
| **Langfuse** | `bronze___llm_traces` | Bronze |
| **Langfuse** | `bronze___llm_observations` | Bronze |
| **Langfuse** | `bronze___llm_scores` | Bronze |

## Development

```bash
# Run tests
uv run pytest

# Lint and format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

### dbt

The dbt project lives at `src/forge/observability/worker/pipelines/dbt/`. It builds staging and views from the bronze tables loaded by dlt. Artifacts are written to `state/.dbt/`.

**DBT CLI** The dbt CLI does not load `.env` automatically. When using the it, source your `.env` file abd point dbt to the correct paths:

```bash
set -a && source .env && set +a
export DBT_PROFILES_DIR=state/.dbt
export DBT_PROJECT_DIR=src/forge/observability/worker/pipelines/dbt
```

```bash
dbt deps      # install packages → state/.dbt/dbt_packages/
dbt debug     # verify connectivity
dbt run       # build all views
dbt test      # run schema tests
```

The worker runs `dbt run` automatically after each successful pipeline execution. To iterate on dbt models while keeping bronze data flowing, start the worker with `--skip-dbt` and invoke `dbt run` directly.
