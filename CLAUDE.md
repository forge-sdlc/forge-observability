# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest
uv run pytest tests/devtools/langfuse/test_seed.py       # single file
uv run pytest -k "test_make_ticket_key"                  # single test by name

# Lint / format
uv run ruff check devtools/ tests/
uv run ruff format devtools/ tests/

# Seed the local observability stack (run in order)
uv run python -m devtools.langfuse.seed      # → writes devtools/seed_output.json
uv run python -m devtools.redis.seed         # → reads seed_output.json, writes to Redis
uv run python -m devtools.prometheus.seed    # → reads seed_output.json, writes to Prometheus

# Start the local Grafana dev instance
podman compose --env-file .env -f devtools/grafana/compose.grafana.yml up -d

# Tear down Grafana (wipes state)
podman compose -f devtools/grafana/compose.grafana.yml down -v
```

## Project structure

```
forge-observability/
├── devtools/
│   ├── langfuse/seed.py          # Langfuse trace seeder (1902 lines)
│   ├── redis/seed.py             # Redis alert seeder + statistics
│   ├── prometheus/seed.py        # Prometheus metric seeder (remote write)
│   ├── seed_output.json          # Sidecar file (generated, not committed)
│   └── grafana/
│       ├── compose.grafana.yml   # Ephemeral Grafana container
│       ├── dashboards/           # Provisioned dashboard JSON files
│       └── provisioning/         # Datasource + dashboard provisioning configs
├── tests/
│   └── devtools/
│       ├── langfuse/test_seed.py
│       ├── redis/test_seed.py
│       └── prometheus/test_seed.py
├── docs/
│   ├── specs/                    # Consolidated design specification
│   └── plans/                    # Consolidated implementation plan
└── proposals/                    # Feature proposal templates
```

## Architecture

**forge-observability** provides synthetic observability data and Grafana dashboards for the Forge SDLC orchestrator. For now, is a devtools project — no production runtime.

### Three seeders, run in order

1. **Langfuse seeder** — Seeds 150 tickets (50 features, 100 bugs) across 2 JIRA projects (`OSASINFRA`, `OSPA`) as Langfuse traces spanning 730 days. Each trace mirrors the LangGraph observation hierarchy that Forge emits. Writes `seed_output.json` as a sidecar for downstream seeders.

2. **Redis seeder** — Reads `seed_output.json`, identifies outlier issues, and writes alert data as Redis-native structures (Hashes + TimeSeries). No JSON blobs — structures are directly queryable by the Grafana Redis datasource.

3. **Prometheus seeder** — Reads `seed_output.json` and writes 1 day of `forge_*` metrics to Prometheus via remote write API (protobuf + snappy) at 15-second intervals. All metrics carry a `project_id` label for cross-datasource correlation.

### Three Grafana dashboards

| Dashboard | UID | Purpose |
|-----------|-----|---------|
| Engineering | `forge-engineering` | Tabbed: Alerts, System Health, Performance |
| Business | `forge-business` | Cost trends, feature vs bug economics, forecasting |
| Issue Detail | `forge-issue-detail` | Per-issue drill-down with workflow waterfall |

Dashboards are provisioned from `devtools/grafana/dashboards/` on container startup.

### Data sources

| Datasource | Type | UID | Purpose |
|------------|------|-----|---------|
| ClickHouse | `grafana-clickhouse-datasource` | `langfuse-clickhouse` | Langfuse traces, observations, and scores |
| Redis | `redis-datasource` | `forge-redis` | Alert findings, scorecards and statistics |
| Prometheus | `prometheus` | `forge-prometheus` | Forge system metrics |

### Dashboard variables

All dashboards share: `project_id` → `jira_issue`. The `project_id` variable cascades into `jira_issue` via ClickHouse `hasAny(tags, [$project_id])`.

## Testing

95 tests, all unit tests using `MagicMock` — no live services required.

```bash
uv run pytest                    # run all 95 tests
uv run pytest -v                 # verbose output
uv run pytest --tb=short         # short tracebacks
```

Test files mirror the devtools structure under `tests/devtools/`.

## Grafana dashboard development

Use the Grafana MCP server for dashboard changes. See `devtools/grafana/README.md` for setup. When developing dashboards, make changes using the Grafana MCP first. If the MCP doesn't provide
the right tool for a specific task or operation, use the Grafana API directly. Once the changes
to the dashboard are okayed by the developer, save the new json representation of the dashboard
to the local project using the v2 schema.

**Workflow Details:**

1. Make changes in Grafana via MCP tools or directly via the grafana API
2. Fetch the updated dashboard JSON from Grafana with `get_dashboard_by_uid`
3. Write it back to the local provisioned JSON file on disk
4. Do NOT manually edit dashboard JSON files separately from Grafana

## Environment variables

See `.env.example` for the full list. Key groups:

- **Langfuse**: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, `LANGFUSE_PORT`
- **ClickHouse**: `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_HTTP_PORT`, `CLICKHOUSE_DATABASE`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`
- **Prometheus**: `PROMETHEUS_HOST`, `PROMETHEUS_PORT`
- **Redis**: `REDIS_HOST`, `REDIS_PORT`
- **Grafana**: `GRAFANA_PORT`
