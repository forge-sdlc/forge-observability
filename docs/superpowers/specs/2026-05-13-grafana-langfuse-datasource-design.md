# Grafana Langfuse Data Source Plugin — Design

**Date:** 2026-05-13
**Project:** `~/git/grafana-langfuse-datasource` (new standalone project)

---

## Overview

A Grafana frontend data source plugin that connects Grafana to a self-hosted Langfuse instance via the Langfuse HTTP API. Includes a Podman Compose file for running Grafana locally with the plugin pre-loaded, and two auto-provisioned dashboards covering LLM cost and performance metrics.

No ETL pipeline. No separate ClickHouse instance. Grafana talks to Langfuse directly through the plugin.

---

## Architecture

Three concerns, cleanly separated:

1. **Plugin** — Grafana frontend data source plugin (TypeScript/React). Proxies all API calls through Grafana's built-in backend proxy so credentials never touch the browser and CORS is not an issue.
2. **Provisioning** — Grafana provisioning config (YAML + dashboard JSON) auto-loaded on startup. No manual UI configuration required.
3. **Compose** — `devtools/compose.grafana.yml` starts Grafana with the plugin and provisioning mounted. Reads credentials from `.env`.

---

## Project Layout

```
grafana-langfuse-datasource/
├── src/
│   ├── plugin.json              # plugin metadata and proxy route config
│   ├── module.ts                # plugin entry point
│   ├── datasource.ts            # query logic, pagination, time bucketing
│   ├── types.ts                 # LangfuseQuery, LangfuseOptions types
│   ├── ConfigEditor.tsx         # datasource config UI (URL, credentials)
│   └── QueryEditor.tsx          # query editor UI (queryType picker)
├── provisioning/
│   ├── datasources/
│   │   └── langfuse.yml         # auto-configures datasource on startup
│   └── dashboards/
│       ├── dashboards.yml       # tells Grafana where to find dashboard JSON
│       ├── llm-cost.json        # LLM Cost Overview dashboard
│       └── llm-performance.json # LLM Performance dashboard
├── devtools/
│   └── compose.grafana.yml
├── .env.example
└── package.json
```

---

## Plugin

### Authentication

The `ConfigEditor` uses Grafana's standard URL field plus two custom fields:
- **URL** — the Langfuse base URL, stored in Grafana's standard `url` datasource field (e.g., `http://host.containers.internal:3000`)
- **Public key** — stored as the basic auth username
- **Secret key** — stored as the basic auth password (Grafana secure storage)

Grafana's built-in basic auth support encodes these and the proxy route attaches an `Authorization: Basic <base64>` header to every proxied request. Credentials never reach the browser.

### Proxy Route (`plugin.json`)

```json
{
  "routes": [{
    "path": "langfuse",
    "url": "{{ .URL }}",
    "authType": "basicAuth"
  }]
}
```

`{{ .URL }}` is Grafana's standard template variable for the datasource's configured URL — stored in the top-level `url` field of the datasource, not in `jsonData`. All plugin API calls go to `/api/datasources/proxy/:id/langfuse/api/public/...`, which Grafana forwards to the configured Langfuse URL with the Authorization header attached.

### Query Types (v1)

The plugin exposes a `queryType` field from day one. v1 values:

| queryType | Endpoint | Aggregation |
|---|---|---|
| `trace_cost` | `/api/public/traces` | sum of `totalCost` per bucket |
| `trace_latency` | `/api/public/traces` | average of `latency` per bucket |
| `trace_count` | `/api/public/traces` | count of traces per bucket |
| `observation_tokens` | `/api/public/observations` | sum of `input + output` tokens per bucket |
| `observation_cost` | `/api/public/observations` | sum of `totalCost` per bucket |

The `queryType` field is the single extension point for pivoting to a flexible query builder (Option 2) later — adding new query types requires no structural changes.

### Data Flow

1. Grafana calls `datasource.query()` with `queryType` and dashboard time range (`from`, `to`)
2. Plugin determines time bucket size from the range (e.g., 1h buckets for 7-day range, 10m for 6-hour range)
3. Plugin makes paginated GET requests through Grafana's proxy, passing `fromUpdatedAt`/`fromStartTime` and `toUpdatedAt`/`toStartTime` (endpoint-specific param names), fetching all pages
4. Results are bucketed client-side: each record is placed into the matching time bucket and the relevant field is accumulated
5. Plugin returns a Grafana DataFrame with a `time` column and a value column

**Shared utilities** (to simplify the Option 2 pivot):
- `fetchAllPages(path, params)` — pagination loop, yields all records
- `bucketByTime(records, getTs, getValue, bucketSize)` — generic time bucketing

### Query Editor UI

A simple dropdown in v1:
```
Metric: [trace_cost ▼]
```

Options: Cost (traces), Latency (traces), Volume (traces), Token usage (observations), Cost (observations).

---

## Provisioning

### Datasource (`provisioning/datasources/langfuse.yml`)

```yaml
apiVersion: 1
datasources:
  - name: Langfuse
    type: forge-langfuse-datasource
    uid: langfuse
    url: "http://${LANGFUSE_HOST}:${LANGFUSE_PORT}"
    editable: false
    basicAuth: true
    basicAuthUser: "${LANGFUSE_PUBLIC_KEY}"
    secureJsonData:
      basicAuthPassword: "${LANGFUSE_SECRET_KEY}"
```

### Dashboards

**LLM Cost Overview** (`llm-cost.json`):
- Time series: total trace cost over time
- Stat: total cost for the selected time range
- Time series: observation-level cost over time

**LLM Performance** (`llm-performance.json`):
- Time series: average trace latency over time
- Time series: trace volume (count) over time
- Time series: token usage (input + output) over time

Both dashboards reference the datasource by UID (`langfuse`) and work out of the box with no manual wiring.

---

## Compose

`devtools/compose.grafana.yml`:

```yaml
services:
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3001:3000"   # 3000 reserved for Langfuse
    environment:
      GF_PATHS_PROVISIONING: /etc/grafana/provisioning
      GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS: forge-langfuse-datasource
      LANGFUSE_HOST: ${LANGFUSE_HOST:-host.containers.internal}
      LANGFUSE_PORT: ${LANGFUSE_PORT:-3000}
      LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY}
      LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY}
    volumes:
      - ../dist:/var/lib/grafana/plugins/forge-langfuse-datasource
      - ../provisioning:/etc/grafana/provisioning
```

Run with:
```bash
podman compose --env-file .env -f devtools/compose.grafana.yml up -d
```

The plugin must be built (`npm run build` → `dist/`) before starting Grafana.

---

## Future: Pivot to Option 2

When ready to expand to a flexible query builder:
1. Add new `queryType` values (e.g., `custom_traces`, `custom_observations`)
2. Expand `QueryEditor.tsx` to show resource, field, and filter selectors for those types
3. Add new handlers in `datasource.ts` using the existing `fetchAllPages` utility
4. Existing fixed query types remain unchanged — no breaking change to existing dashboards
