# Devtools

Seed scripts and local development tools for the Forge observability stack.

---

## Seeding overview

Three seeders populate the observability stack with realistic synthetic data.
**Langfuse seeder must be run first** — every other seeder reads the output of the langfuse seeder.

```
devtools/langfuse/seed.py    →  devtools/seed_output.json
devtools/redis/seed.py       →  forge:alerts:* Hashes and forge:stats:alerts:* in Redis
devtools/prometheus/seed.py  →  forge_* metrics in Prometheus
```

### 1. Langfuse seed

```bash
uv run python -m devtools.langfuse.seed
```

Seeds 150 tickets (50 features, 100 bugs) as Langfuse traces spanning 730 days.
Writes `devtools/seed_output.json` — consumed by the Redis and Prometheus seeders.

**Requires:** Langfuse running (`LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` in `.env`).

### 2. Redis alert seeder

```bash
uv run python -m devtools.redis.seed
```

Reads `devtools/seed_output.json`, identifies outlier issues, and writes all alert data
directly as Redis-native structures (no JSON strings):

- `forge:alerts:{issue_id}:{alert_type}` — Hash per individual alert
- `forge:stats:alerts:summary` — Hash with total, critical, warning, cost_outlier, latency_outlier counts
- `forge:stats:alerts:by_type` — Hash with per-alert-type counts
- `forge:stats:alerts:ts:cost_outlier` / `forge:stats:alerts:ts:latency_outlier` — TimeSeries
- `forge:alerts:idx` — RediSearch index over alert Hashes (enables `FT.SEARCH` table queries in Grafana)
- `forge:alerts:stream` — Redis Stream with all alert entries

**Requires:**

- Forge Redis running (`REDIS_PORT=6380` in `.env` is the default).
- Redis Stack server (`redis/redis-stack-server`) — needed for the TimeSeries module and Redis Streams.
- `devtools/seed_output.json` to exist (run the Langfuse seeder first).

### 3. Prometheus seeder

```bash
uv run python -m devtools.prometheus.seed
```

Reads `devtools/seed_output.json` for ticket volumes, then writes 1 day of backdated
Forge metric time-series to Prometheus via the remote write API (protobuf + snappy),
at a 15-second scrape interval (~5,760 samples per metric).

**Requires:**

- `python-snappy` installed — `uv add python-snappy`
- Forge Prometheus running (`PROMETHEUS_PORT=9092` in `.env` is the default)
- Two startup flags must be added to the `prometheus` service in `forge/docker-compose.yml`:

  ```yaml
  command:
    - '--config.file=/etc/prometheus/prometheus.yml'
    - '--storage.tsdb.path=/prometheus'
    - '--storage.tsdb.retention.time=15d'
    - '--web.enable-lifecycle'
    - '--web.enable-remote-write-receiver'   # required: enables /api/v1/write
    - '--web.enable-admin-api'               # required: enables delete_series for cleanup
  ```

  Without `--web.enable-remote-write-receiver` the seeder fails with HTTP 404.
  Without `--web.enable-admin-api` the cleanup step is skipped and old data accumulates on re-runs.

  Restart after changes: `podman compose -f forge/docker-compose.yml up -d prometheus`

- Out-of-order ingestion must be enabled in prometheus.yml (in the forge repo):

  ```yaml
  storage:
    tsdb:
      out_of_order_time_window: 15d
  ```

- `devtools/seed_output.json` to exist (run the Langfuse seeder first).

---

## Environment variables

| Variable | Used by |
|---|---|
| `CLICKHOUSE_HOST` | Grafana ClickHouse datasource |
| `CLICKHOUSE_PORT` | Grafana ClickHouse datasource |
| `CLICKHOUSE_HTTP_PORT` | Grafana ClickHouse datasource |
| `CLICKHOUSE_DATABASE` | Grafana ClickHouse datasource |
| `CLICKHOUSE_USER` | Grafana ClickHouse datasource |
| `CLICKHOUSE_PASSWORD` | Grafana ClickHouse datasource |
| `REDIS_HOST` | Redis seeders + Grafana Redis datasource |
| `REDIS_PORT` | Redis seeders + Grafana Redis datasource |
| `PROMETHEUS_HOST` | Prometheus seeder + Grafana Prometheus datasource |
| `PROMETHEUS_PORT` | Prometheus seeder + Grafana Prometheus datasource |

See `.env.example` for the full list.

---

## Grafana

See [`grafana/README.md`](grafana/README.md) for starting the local Grafana instance
and configuring the MCP server for Claude Code dashboard development.
