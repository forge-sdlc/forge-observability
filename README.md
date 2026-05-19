# forge-observability

Observability tooling for the [Forge SDLC orchestrator](https://github.com/forge-sdlc/forge). Provides synthetic trace data, alert analytics, system metrics, and a Grafana dashboard suite for developing Forge's observability capabilities.

## Overview

Forge is an AI-powered SDLC orchestrator that processes JIRA tickets through a LangGraph workflow — from PRD generation through code review. Every workflow step produces LLM traces in Langfuse. This project:

1. **Seeds realistic data** — 150 tickets across 2 JIRA projects, producing ~1,400 Langfuse traces with a realistic LangGraph observation hierarchy, multi-model assignment (Claude Opus/Sonnet/Haiku + Gemini 2.5 Pro), and log-normal token/latency distributions to simulate anomalies. The seeder creates `seed_output.json` which is used to generate correlated data
in both redis and prometheus.

2. **Generates correlated alerts** — Outlier tickets get alert findings written to Redis as native Hashes and TimeSeries, queryable directly by Grafana.

3. **Produces system metrics** — 1 day of Prometheus metrics (`forge_*` counters, histograms, gauges) at 15-second intervals with `project_id` and `workflow_step` labels for cross-datasource correlation.

4. **Provides a dashboard suite** — Three interlinked Grafana dashboards (Engineering, Business, JIRA Issue Detail) that demonstrate forge observability end-to-end.

## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- [Podman](https://podman.io/) (or Docker) for the Grafana container
- Running Forge infrastructure: Langfuse, Redis (redis-stack-server), Prometheus

## Quickstart

```bash
# Clone and install
git clone https://github.com/forge-sdlc/forge-observability
cd forge-observability
cp .env.example .env
# Fill in LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY from your Langfuse project
uv sync

# Seed the observability stack
# Langfuse seed must run first; the other seeders can run in any order
# Seeders clean up previously written data before rewriting new seeds
# If Langfuse is seeded a second time, the other seeders need to be run again
uv run python -m devtools.langfuse.seed
uv run python -m devtools.redis.seed
uv run python -m devtools.prometheus.seed

# Start Grafana
podman compose --env-file .env -f devtools/grafana/compose.grafana.yml up -d
# Open http://localhost:3010 (admin / grafana)
```

## Architecture

```
    ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
    │  Langfuse   │ │    Redis    │ │ Prometheus  │
    │  ~1,400     │ │  Alerts +   │ │  forge_*    │
    │  traces     │ │  Statistics │ │  metrics    │
    └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
           │               │               │
           └───────────────┼───────────────┘
                           ▼
                  ┌─────────────────┐
                  │     Grafana     │
                  ├─────────────────┤
                  │  Engineering    │◄─── Alerts, system health,
                  │  Business       │     performance, models
                  │  Issue Detail   │◄─── Per-issue drill-down
                  └────────┬────────┘
                           │
                           ▼
                     Langfuse UI
                   (trace deep links)
```

### Dashboard suite

| Dashboard | Audience | Key panels |
|-----------|----------|------------|
| **Engineering** | Technical Engineers | Alert summary, system health + Prometheus correlation, cost/latency outlier detection, model efficiency, issues table |
| **Business** | Business users | Total LLM cost, feature vs bug economics, cost trends + forecasting |
| **Issue Detail** | Both | Per-issue KPIs, workflow step timeline, cost/token breakdown, trace table with Langfuse deep links |

### Cross-datasource correlation

Dashboards correlate data across ClickHouse (Langfuse), Redis, and Prometheus using shared dimensions:

- **`project_id`** — First-class label on all data sources, cascading dashboard variable
- **`workflow_step`** / `phase` — Maps Langfuse trace tags to Prometheus phase labels
- **`ticket_type`** — `feature` or `bug`, present in all three stores
- **`jira_issue`** / `session_id` — Per-issue join key between ClickHouse and Redis

## Development

```bash
# Run tests (95 unit tests, no live services needed)
uv sync --dev
uv run pytest

# Lint and format
uv run ruff check devtools/ tests/
uv run ruff format devtools/ tests/
```

See [`devtools/README.md`](devtools/README.md) for more seeder details and [`devtools/grafana/README.md`](devtools/grafana/README.md) for Grafana development workflow.

## Project documentation

- [`docs/specs/`](docs/specs/) — Design specifications
- [`docs/plans/`](docs/plans/) — Implementation plans
- [`devtools/README.md`](devtools/README.md) — Seeder usage and configuration
- [`devtools/grafana/README.md`](devtools/grafana/README.md) — Grafana setup and MCP integration
