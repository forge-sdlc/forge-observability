# forge-observability

Analytics and observability plugin for the [Forge SDLC Orchestrator](https://github.com/your-org/forge).

![Python](https://img.shields.io/badge/python-3.11%2B-blue)

## Overview

forge-observability ingests data from Langfuse, GitHub, JIRA, and Prometheus into ClickHouse, then exposes cross-source analytics through a FastAPI HTTP service. It ships as two independent services: an **API server** and a **worker**.

It runs as the `observability` subcommand under the `forge` CLI.

## Architecture

Data flows through a medallion structure:

```
Langfuse  ──┐
GitHub    ──┤──► Bronze(raw, per-source) ──► Silver/Gold (cross-source joins/KPIs) ──► API
JIRA      ──┤
Prometheus ─┘
```

**Bronze** tables are loaded by dlt into the configured datastore using triple-underscore naming (`bronze___llm_traces`, `bronze___pull_requests`, etc.) within the configured database. **Silver** views are SQL views built by dbt. The API queries silver/gold views for analytics and silver/bronze tables for raw drill-down.

dlt pipelines run concurrently via `asyncio` and handle incremental loading, schema evolution, and datastore destination management. dbt uses an event pattern to rebuild silver/gold views when dlt runs the configured group of pipelines

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
# Edit .env — at minimum set credentials for the sources you want to enable

# Run pipelines once to backfill (also runs dbt to build silver views)
forge observability worker --once

# Start the API
forge observability serve --reload
```

```bash
curl http://localhost:8010/health
# {"status":"ok","store":"sqlalchemy"}
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

### API

| Variable | Default | Description |
|----------|---------|-------------|
| `FORGE_OBSERVABILITY_API_PORT` | `8010` | Listening port |
| `FORGE_OBSERVABILITY_API_LOG_LEVEL` | `INFO` | Uvicorn log level |

### Worker

| Variable | Default | Description |
|----------|---------|-------------|
| `FORGE_OBSERVABILITY_WORKER_LOG_LEVEL` | `INFO` | Log level |
| `FORGE_OBSERVABILITY_WORKER_SKIP_DBT` | `false` | Skip dbt silver/gold rebuilds — useful when iterating on dbt models and invoking dbt directly |

### Langfuse *(disabled if credentials absent)*

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_HOST` | `localhost` | Hostname |
| `LANGFUSE_PORT` | `3000` | Port |
| `LANGFUSE_PUBLIC_KEY` | `` | Public API key |
| `LANGFUSE_SECRET_KEY` | `` | Secret API key |
| `LANGFUSE_INTERVAL_SECONDS` | `60` | Pipeline polling interval |

### Prometheus 

| Variable | Default | Description |
|----------|---------|-------------|
| `PROMETHEUS_HOST` | `localhost` | Hostname |
| `PROMETHEUS_PORT` | `9090` | Port |
| `PROMETHEUS_INTERVAL_SECONDS` | `300` | Pipeline polling interval |

### GitHub *(disabled if token absent)*

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | `` | Personal access token |
| `GITHUB_KNOWN_REPOS` | `` | Comma-separated `owner/repo` list |
| `GITHUB_INTERVAL_SECONDS` | `600` | Pipeline polling interval |

### JIRA *(disabled if credentials absent)*

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_BASE_URL` | `` | Instance URL, e.g. `https://your-org.atlassian.net` |
| `JIRA_USER_EMAIL` | `` | API user email |
| `JIRA_API_TOKEN` | `` | API token |
| `JIRA_INTERVAL_SECONDS` | `600` | Pipeline polling interval |


> When running inside containers, use `host.containers.internal` instead of `localhost` to reach services on the host machine.

## Running with containers

```bash
# External Datastore (run first)
podman compose --env-file .env -f devtools/compose.clickhouse.yml up -d

# API + worker
podman compose --env-file .env -f devtools/compose.dev.yml up -d

# Tail logs
podman compose --env-file .env -f devtools/compose.dev.yml logs -f
```

The API is available at `http://localhost:8010`. dlt pipeline state is persisted in a named volume (`pipeline_state`) mounted at `/home/forge/.dlt`.

Tear down external datastore:

```bash
podman compose -f devtools/compose.clickhouse.yml down -v
```

## CLI reference

Usage: `forge [-v] observability <command> [options]`

`-v / --verbose` enables DEBUG-level logging.

### `serve`

Start the FastAPI HTTP server.

```
forge observability serve [--host HOST] [--port PORT] [--reload]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8010` | Listening port |
| `--reload` | off | Auto-reload on code changes (dev only) |

### `worker`

Run all configured dlt pipelines, then rebuild silver/gold views with dbt.

```
forge observability worker [--once] [--skip-dbt]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--once` | off | Run each pipeline once then exit (useful for backfill) |
| `--skip-dbt` | off | Run source pipelines only — skip dbt silver/gold rebuilds |

Without `--once`, pipelines loop on their configured `*_INTERVAL_SECONDS` and dbt rebuilds silver views after each pipeline round completes.

`--skip-dbt` is intended for local dbt development: keep the worker running so fresh bronze data continues to flow in, then invoke `dbt run` directly as you iterate on models. The flag takes precedence over the `FORGE_OBSERVABILITY_WORKER_SKIP_DBT` environment variable.

## API reference

Base URL: `http://localhost:8010` (or `$FORGE_OBSERVABILITY_API_PORT`)

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check; verifies datastore connectivity |

### Bronze — raw drill-down

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/traces` | `ticket_key`, `limit` (1–500, default 50) | Recent LLM traces from `bronze___llm_traces` |
| `GET` | `/traces/summary` | `days` (1–90, default 7) | Avg latency and cost per trace name |
| `GET` | `/ci-checks` | `repo` (`owner/repo`), `conclusion` (`success`/`failure`), `limit` (1–1000, default 100) | Recent CI check runs |

### Silver — cross-source joins

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/tickets/{ticket_key}/summary` | — | Full ticket summary: LLM cost + PR metrics + interactions |
| `GET` | `/insights/workflows` | `ticket_type`, `status`, `min_llm_cost`, `limit` (1–500, default 50) | All tickets sorted by LLM cost |
| `GET` | `/insights/stage-performance` | — | Per-stage LLM cost, avg latency, and approval rate |
| `GET` | `/insights/prs` | `merged_only` (bool, default false), `limit` (1–500, default 50) | PR metrics correlated with LLM cost |
| `GET` | `/insights/cost-by-model` | `days` (1–90, default 30) | LLM cost and trace volume grouped by trace name |

Errors: `503` on datastore failure, `404` when no data is found for single-row endpoints.

## Data sources

| Source | Bronze tables | Always enabled? |
|--------|--------------|-----------------|
| **Langfuse** | `llm_traces` | No — requires `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` |
| **GitHub** | `pull_requests`, `ci_checks` | No — requires `GITHUB_TOKEN` |
| **JIRA** | `jira_tickets`, `human_interactions` | No — requires `JIRA_BASE_URL` + `JIRA_USER_EMAIL` + `JIRA_API_TOKEN` |
| **Prometheus** | `app_metrics` | Yes |

The JIRA pipeline only ingests tickets labelled `forge:managed`. Silver/gold views built by dbt automatically adapt to whichever sources are available — joins for missing sources are omitted until those bronze tables exist.

## Development

```bash
# Run tests
uv run pytest
uv run pytest tests/test_api.py       # single file
uv run pytest -k "test_health"        # single test by name

# Lint and format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

Tests use a mocked `Repository` and FastAPI's `TestClient` — no live datastore needed.

### dbt

The dbt project lives at `src/forge/observability/pipelines/dbt/`. It builds the silver and gold views in the target datastore from the bronze tables loaded by dlt.

**Profile setup** — `dbt` reads credentials from `.dbt/profiles.yml` at the workspace root. This file is committed and pre-configured for the example ClickHouse dev container (`localhost:8123`, user `forge`, password `forge`). Compiled artifacts and logs are also written to `.dbt/`.

**Environment variables** — `profiles.yml` reads ClickHouse credentials via `env_var()`, which pulls from the shell environment. Unlike `forge observability worker`, the `dbt` CLI does not load `.env` automatically. Source it before running any `dbt` command locally:

```bash
set -a && source .env && set +a
```

VSCode users: the dbt Power User extension picks up the profile and project paths automatically via `.vscode/settings.json`.

```bash
# First-time setup: install declared packages into .dbt/dbt_packages/
dbt deps

# Verify profile and datastore connectivity
dbt debug

# Compile models without executing (useful for syntax checking)
dbt compile

# Build all silver and gold views
dbt run

# Build a single layer
dbt run --select silver
dbt run --select gold

# Run schema and data tests
dbt test

# Browse auto-generated docs
dbt docs generate && dbt docs serve
```

The worker (`forge observability worker`) runs `dbt run` automatically after each pipeline round. To iterate on dbt models while keeping bronze data flowing, start the worker with `--skip-dbt` (or set `FORGE_OBSERVABILITY_WORKER_SKIP_DBT=true`) and invoke `dbt run` directly.
