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
uv run pytest tests/test_config.py      # single file
uv run pytest -k "test_langfuse_url"   # single test by name

# Lint / format
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Run all dlt/dbt pipelines (continuous or one-shot)
uv run forge-observability worker
uv run forge-observability worker --once

# Run source pipelines only — skip dbt silver/gold rebuilds.
# Useful when iterating on dbt models and invoking dbt directly.
uv run forge-observability worker --skip-dbt
uv run forge-observability worker --once --skip-dbt
# Equivalent via env var (also respected by compose.dev.yml):
# FORGE_OBSERVABILITY_WORKER_SKIP_DBT=true

# Start dev datastore (e.g., ClickHouse)
podman compose --env-file .env -f devtools/compose.clickhouse.yml up -d

# Start Forge Observability Stack (worker)
podman compose --env-file .env -f devtools/compose.dev.yml up -d

# dbt
# profiles.yml lives at state/.dbt/profiles.yml.
# dbt artifacts (target, logs, packages) write to state/.dbt/.
#
# The dbt CLI does not load .env automatically — source it first:
set -a && source .env && set +a

# Also set the profiles dir (already wired in .vscode/settings.json for terminal):
export DBT_PROFILES_DIR=state/.dbt
export DBT_PROJECT_DIR=src/forge/observability/worker/pipelines/dbt

dbt deps                     # install packages declared in dependencies.yml → state/.dbt/dbt_packages/
dbt debug                    # verify profile + datastore connectivity
dbt compile                  # parse and compile models without executing
dbt run                      # build all silver views in ClickHouse
dbt run --select silver      # build only the silver layer
dbt test                     # run schema + data tests defined in models/
dbt docs generate            # generate docs site
dbt docs serve               # serve docs locally (default port 8080)
```

## Architecture

**forge-observability** is a data ingestion and analytics worker for the Forge SDLC Orchestrator. It pulls LLM observability data from Langfuse into a target datastore and builds analytics views using dbt.

### Data layers

| Layer | Location | What it is |
|---|---|---|
| Bronze | Datastore tables | Raw, denormalized tables loaded by dlt |
| Silver | Datastore views | Analytical tables built by dbt |
| Gold | Datastore views | Pre-aggregated KPIs built by dbt |

**Table naming convention**: dlt loads bronze tables into the `default` database using triple-underscore as a schema prefix separator — e.g. `bronze___llm_traces`.

### Pipeline worker (`worker/worker.py`)

The Langfuse dlt pipeline runs on a configurable interval via `asyncio`. After each successful pipeline run, dbt rebuilds views via `dlt.dbt.package()`. Worker entry point: `forge-observability worker`.

**dlt pipeline**:
- `langfuse_pipeline` → `bronze___llm_traces` + `bronze___llm_observations` + `bronze___llm_scores` 

The pipeline is only registered if Langfuse credentials are present (`langfuse_enabled` on `Settings`).

**dbt run** (in `worker/pipelines/dbt/`): After a successful bronze pipeline run, `_run_dbt()` calls `dlt.dbt.package()`. Each dbt model uses the `is_source_available()` macro — which calls `adapter.get_relation()` to check whether the bronze table actually exists in the datastore — to conditionally enable models. Views enable themselves automatically once the corresponding table appears.

### Module layout

```
src/forge/observability/
├── config.py               # Pydantic settings (env vars / .env)
├── cli.py                  # CLI entry point: forge-observability worker
└── worker/
    ├── __init__.py         # Re-exports run_pipelines
    ├── worker.py           # Pipeline orchestration and dbt runner
    └── pipelines/
        ├── langfuse_pipeline.py
        └── dbt/            # dbt project
            ├── dbt_project.yml
            ├── macros/is_source_available.sql
            └── models/
                ├── sources.yml
                ├── staging/stg_llm_traces.sql
                └── silver/
                    ├── stage_performance.sql
                    └── ticket_llm_summary.sql
```

### Configuration (`config.py`)

All config comes from environment variables (or `.env`) via Pydantic settings — `get_settings()` returns a cached singleton. Key groups:

- ClickHouse: `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_HTTP_PORT`, `CLICKHOUSE_DATABASE`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`
- Worker: `FORGE_OBSERVABILITY_WORKER_LOG_LEVEL`, `FORGE_OBSERVABILITY_WORKER_SKIP_DBT`
- Langfuse: `LANGFUSE_HOST`, `LANGFUSE_PORT`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_INTERVAL_SECONDS`

`langfuse_url` (computed) composes host+port into the full URL.

### Entry points

`forge-observability` → `forge.observability.cli:main`

### Containers

`containers/forge-observability-worker/` holds the worker Containerfile. It is composed with ClickHouse in `devtools/`.

dlt and dbt state are written to `/app/state/` inside the container, backed by a named volume (`forge_observability_worker_state`) mounted at `/app/state`.

## Tests

Tests use `pytest-asyncio` and `unittest.mock` — no live datastore needed.

- `tests/test_config.py` — settings loading and computed properties
- `tests/test_cli.py` — CLI argument parsing and dispatch
- `tests/test_worker.py` — pipeline orchestration and dbt trigger logic

Ruff is configured with rules E, F, I, UP, B, SIM, ARG and a 100-character line limit.
